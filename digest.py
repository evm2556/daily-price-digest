#!/usr/bin/env python3
"""Daily deal digest: Slickdeals + Reddit (+ optional Google Shopping) judged against your price history.

Run locally:  python digest.py --open        (writes digest.html)
Scheduled:    .github/workflows/daily.yml
"""
import argparse, datetime as dt, html, os, re, smtplib, sqlite3, statistics, urllib.parse, webbrowser
from email.mime.text import MIMEText
from pathlib import Path

import feedparser, requests, yaml

ROOT = Path(__file__).parent
DB, OUT = ROOT / "prices.db", ROOT / "digest.html"
UA = "deal-digest/1.0 (personal use)"
WINDOW_DAYS = 180
PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{2})?")


# ---------- collectors: each returns [{source, title, url, price}] ----------
def price_in(text):
    m = PRICE.search(text or "")
    return float(m.group(1).replace(",", "") + (m.group(2) or "")) if m else None


def feed_hits(source, url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return [{"source": source, "title": e.get("title", ""), "url": e.get("link", ""),
             "price": price_in(e.get("title", ""))} for e in feedparser.parse(r.content).entries]


def slickdeals(q, cfg):
    qs = urllib.parse.urlencode({"q": q, "searchin": "first", "rss": 1})
    return feed_hits("Slickdeals", f"https://slickdeals.net/newsearch.php?{qs}")


def reddit(q, cfg):
    subs = cfg.get("subreddits", "deals+buildapcsales+GameDeals")
    qs = urllib.parse.urlencode({"q": q, "restrict_sr": "on", "sort": "new", "t": "week"})
    return feed_hits("Reddit", f"https://www.reddit.com/r/{subs}/search.rss?{qs}")


def google(q, cfg):
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return []
    r = requests.get("https://serpapi.com/search.json", timeout=30,
                     params={"engine": "google_shopping", "q": q, "api_key": key, "gl": "us"})
    r.raise_for_status()
    return [{"source": f"Google ({s.get('source', '')})", "title": s.get("title", ""),
             "url": s.get("product_link") or s.get("link", ""), "price": s.get("extracted_price")}
            for s in r.json().get("shopping_results", [])[:10]]


# ---------- scoring ----------
def verdict(item, price, hist, days):
    """Return (tag, [phrases]) comparing price to usual price, 6-month low, and tracked history."""
    parts, tag = [], "ok"
    ref = item.get("usual") or (statistics.median(hist) if hist else None)
    if ref:
        pct = (1 - price / ref) * 100
        what = "usual" if item.get("usual") else "median tracked"
        parts.append(f"{abs(pct):.0f}% {'below' if pct >= 0 else 'above'} {what} ${ref:,.0f}")
    cands = []
    if item.get("low_6m"):
        cands.append((item["low_6m"], "6-month low"))
    if hist:
        cands.append((min(hist), f"lowest tracked ({days}d)"))
    if cands:
        low, label = min(cands)
        if price <= low:
            tag = "best"
            parts.append(f"at or below {label} ${low:,.0f}")
        else:
            parts.append(f"{(price / low - 1) * 100:.0f}% above {label} ${low:,.0f}")
    if item.get("target") and price <= item["target"]:
        tag = "best"
        parts.append(f"under your target ${item['target']:,.0f}")
    if not parts:
        parts.append("no history yet; add usual/low_6m in items.yaml for context")
    return tag, parts


def run(cfg, today):
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS observations(
        item TEXT, source TEXT, title TEXT, url TEXT, price REAL, seen_at TEXT,
        UNIQUE(item, url, price))""")
    cutoff = (today - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    results, errors = [], []
    for item in cfg["items"]:
        name = item["name"]
        terms = [t.lower() for t in (item.get("terms") or name.split())]
        q = item.get("query", name)
        collectors = [slickdeals, reddit] + ([google] if item.get("google") else [])
        found = []
        for fn in collectors:
            try:
                found += fn(q, cfg)
            except Exception as ex:  # one dead source must not kill the digest
                errors.append(f"{fn.__name__} / {name}: {ex}")
        found = [h for h in found if all(any(a in h["title"].lower() for a in t.split("|")) for t in terms)]
        found.sort(key=lambda h: (h["price"] is None, h["price"] or 0))

        rows = con.execute("SELECT price, substr(seen_at,1,10) FROM observations "
                           "WHERE item=? AND substr(seen_at,1,10)>=? AND substr(seen_at,1,10)<?",
                           (name, cutoff, today.isoformat())).fetchall()
        hist, days = [r[0] for r in rows], len({r[1] for r in rows})

        for h in found:
            if h["price"] is not None:
                con.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",
                            (name, h["source"], h["title"], h["url"], h["price"], today.isoformat()))
        best = next((h for h in found if h["price"] is not None), None)
        tag, parts = verdict(item, best["price"], hist, days) if best else ("none", [])
        results.append({"name": name, "best": best, "others": [h for h in found if h is not best],
                        "tag": tag, "parts": parts, "days": days})
    con.commit()
    con.close()
    return results, errors


# ---------- output ----------
CSS = """:root{color-scheme:light dark;--bg:#fafafa;--fg:#1a1a1a;--mut:#666;--card:#fff;--bd:#ddd;--ok:#1a7f37}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#999;--card:#212121;--bd:#383838;--ok:#4ac26b}}
body{font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:720px;margin:2rem auto;padding:0 1rem}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:.8rem 1rem;margin:.7rem 0}
.card.best{border-color:var(--ok);border-width:2px}.price{font-size:1.4rem;font-weight:600}
.mut{color:var(--mut);font-size:.9rem}ul{margin:.4rem 0 0;padding-left:1.1rem}a{color:inherit}"""


def money(x):
    return f"${x:,.2f}".removesuffix(".00")


def render(results, errors, today):
    e = lambda s: html.escape(str(s), quote=True)
    link = lambda h: f'<a href="{e(h["url"])}">{e(h["source"])}</a>'
    hits = sorted((r for r in results if r["best"]), key=lambda r: r["tag"] != "best")
    out = [f"<!doctype html><meta charset=utf-8><title>Deal digest {today}</title><style>{CSS}</style>",
           f"<h1>Deal digest <span class=mut>{today:%a %b %d}</span></h1>"]
    for r in hits:
        b = r["best"]
        others = "".join(f"<li>{link(o)} {money(o['price']) if o['price'] else '(no price)'} "
                         f"<span class=mut>{e(o['title'][:90])}</span></li>" for o in r["others"][:4])
        out.append(f'<div class="card {r["tag"]}"><b>{e(r["name"])}</b>'
                   f'<div class=price>{money(b["price"])} <span class=mut>via {link(b)}</span></div>'
                   f'<div>{e("; ".join(r["parts"]))}</div>'
                   f'<div class=mut>{e(b["title"][:120])} &middot; {r["days"]} days of tracked history</div>'
                   f'{"<ul>" + others + "</ul>" if others else ""}</div>')
    quiet = [r["name"] for r in results if not r["best"]]
    if quiet:
        out.append(f'<p class=mut>No priced deals today: {e(", ".join(quiet))}</p>')
    if errors:
        out.append(f'<details class=mut><summary>{len(errors)} source error(s)</summary>'
                   f'{"<br>".join(e(x) for x in errors)}</details>')
    return "\n".join(out)


def send_email(page, subject):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("MAIL_TO")
    if not (user and pw and to):
        return
    msg = MIMEText(page, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="open digest.html in the browser")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "items.yaml").read_text(encoding="utf-8"))
    today = dt.date.today()
    if cfg.get("until") and today > dt.date.fromisoformat(str(cfg["until"])):
        print("Past the 'until' date in items.yaml; nothing to do.")
        return
    results, errors = run(cfg, today)
    page = render(results, errors, today)
    OUT.write_text(page, encoding="utf-8")  # always the latest
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")  # runner clock is UTC on GitHub
    d = ROOT / "docs"
    d.mkdir(exist_ok=True)
    (d / f"digest-{stamp}.html").write_text(page, encoding="utf-8")
    links = "".join(f'<li><a href="{f.name}">{f.stem}</a></li>'
                    for f in sorted(d.glob("digest-*.html"), reverse=True))
    (d / "index.html").write_text(page + f"<h3>Earlier digests</h3><ul>{links}</ul>", encoding="utf-8")
    n = sum(r["tag"] == "best" for r in results)
    send_email(page, f"Deal digest {today}: {n} at/under your benchmark")
    print(f"{len(results)} items, {n} strong hits, {len(errors)} source errors -> {OUT.name}")
    if args.open:
        webbrowser.open(OUT.as_uri())


if __name__ == "__main__":
    main()
