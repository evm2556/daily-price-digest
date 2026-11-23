#!/usr/bin/env python3
"""Daily deal digest: Slickdeals + Reddit (+ optional Google Shopping) judged against your price history.
An LLM (GitHub Models) expands vague wishes into searches/subreddits and rejects bad matches.

Run locally:  python digest.py --open        (writes digest.html)
Scheduled:    .github/workflows/daily.yml
"""
import argparse, datetime as dt, hashlib, html, json, os, re, shutil, smtplib, sqlite3, statistics
import subprocess, time, urllib.parse, webbrowser
from email.mime.text import MIMEText
from pathlib import Path

import feedparser, requests, yaml

ROOT = Path(__file__).parent
DB, OUT, CACHE = ROOT / "prices.db", ROOT / "digest.html", ROOT / "expansions.json"
UA = "deal-digest/1.0 (personal use)"
WINDOW_DAYS = 180
PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{2})?")
SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{3,21}$")
DEFAULT_MODEL = "openai/gpt-4.1"


# ---------- collectors: each returns [{source, title, url, price}] ----------
def scrub(text):
    """Keys end up in request URLs and therefore in error messages; never write them to the digest."""
    for var in ("REDDIT_RSS_KEY", "SERPAPI_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.getenv(var):
            text = text.replace(os.getenv(var), "***")
    return text


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
    params = {"q": q, "restrict_sr": "on", "sort": "new", "t": "week"}
    key = os.getenv("REDDIT_RSS_KEY")
    if key:  # keyed RSS proxy; reddit.com itself blocks most cloud IPs
        params["key"] = key
        base = f"https://api.redlisten.com/r/{subs}/search.rss"
    else:
        base = f"https://www.reddit.com/r/{subs}/search.rss"
    return feed_hits("Reddit", f"{base}?{urllib.parse.urlencode(params)}")


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


# ---------- LLM (GitHub Models) ----------
def llm_available():
    return bool(shutil.which("gh") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"))

def llm(instruction, data, model):
    """`gh models run MODEL "instruction"` with data on stdin; falls back to the REST API."""
    time.sleep(1)  # be gentle with per-minute rate limits
    cli_err = "gh not installed"
    if shutil.which("gh"):
        p = subprocess.run(["gh", "models", "run", model, instruction], input=data,
                           capture_output=True, text=True, timeout=120)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout
        cli_err = f"exit {p.returncode}: {(p.stderr or p.stdout).strip()[:200]!r}"
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        raise RuntimeError(f"gh models failed ({cli_err}) and no token for REST fallback")
    r = requests.post("https://models.github.ai/inference/chat/completions", timeout=120,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"model": model, "messages": [{"role": "user", "content": f"{instruction}\n\n{data}"}]})
    try:
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"CLI: {cli_err} | REST {r.status_code}: {r.text[:200]!r}") from None


def llm_json(instruction, data, model):
    out = llm(instruction + " Reply with a single JSON object and nothing else.", data, model)
    try:
        return json.loads(out[out.index("{"): out.rindex("}") + 1])  # tolerate ```json fences / chatter
    except ValueError:
        raise ValueError(f"model reply was not JSON: {out[:200]!r}") from None

def uniq(xs):
    return list({x.lower(): x for x in xs if x}.values())


def plan(item, cfg, cache, model):
    """Search queries + subreddits for an item. A free-text `description` is expanded by the LLM once
    and cached in expansions.json (edit that file to correct it)."""
    queries = [item.get("query", item["name"])]
    subs = [s for s in cfg.get("subreddits", "deals").split("+") if s]
    desc = item.get("description")
    if desc:
        key = hashlib.sha1(desc.encode()).hexdigest()[:10]
        ent = cache.get(item["name"])
        if not ent or ent.get("hash") != key:
            j = llm_json('A shopper described something they want to buy (text below). Return {"queries": '
                         '[up to 2 short search queries for deal sites], "subreddits": [up to 4 subreddit '
                         'names without r/ where deals for this kind of product are posted]}.', desc, model)
            clean = lambda s: s.strip().removeprefix("r/") if isinstance(s, str) else ""
            ent = {"hash": key,
                   "queries": [q.strip() for q in j.get("queries", []) if isinstance(q, str) and q.strip()][:2],
                   "subreddits": [s for s in map(clean, j.get("subreddits", [])) if SUBREDDIT.match(s)][:4]}
            cache[item["name"]] = ent
        queries += ent["queries"]
        subs += ent["subreddits"]
    return uniq(queries), uniq(subs)


def verify(item, hits, model):
    """Ask the LLM which listings are really what the shopper wants. Returns (kept, rejected_count)."""
    lines = "\n".join(f'{i}. [{h["source"]}] {h["title"]} | {money(h["price"]) if h["price"] else "no price"}'
                      for i, h in enumerate(hits))
    want = item.get("description") or item["name"]
    j = llm_json(f'A shopper wants: "{want}". The numbered deal listings below came from search. Return '
                 '{"keep": [indexes]} containing only listings that are genuinely that kind of product: '
                 'right category, not an accessory, part, refill or different item, and a plausible '
                 'new-item deal.', lines, model)
    keep = {i for i in j.get("keep", []) if isinstance(i, int) and 0 <= i < len(hits)}
    return [h for i, h in enumerate(hits) if i in keep], len(hits) - len(keep)


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
    model = cfg.get("llm_model", DEFAULT_MODEL)
    use_llm = bool(cfg.get("llm", True))
    if use_llm and not llm_available():
        use_llm = False
        errors.append("LLM skipped: neither the gh CLI nor GITHUB_TOKEN/GH_TOKEN is available")
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    before = json.dumps(cache, sort_keys=True)

    for item in cfg["items"]:
        name = item["name"]
        if item.get("terms"):
            terms = [t.lower() for t in item["terms"]]
        else:  # with the LLM judging matches, don't pre-filter on the name's words
            terms = [] if use_llm else [w.lower() for w in name.split()]
        queries, icfg = [item.get("query", name)], cfg
        if use_llm:
            try:
                queries, subs = plan(item, cfg, cache, model)
                icfg = {**cfg, "subreddits": "+".join(subs)}
            except Exception as ex:
                errors.append(scrub(f"llm expand / {name}: {ex}"))
        collectors = [slickdeals, reddit] + ([google] if item.get("google") else [])
        found = []
        for q in queries:
            for fn in collectors:
                try:
                    found += fn(q, icfg)
                except Exception as ex:  # one dead source must not kill the digest
                    errors.append(scrub(f"{fn.__name__} / {name} / '{q}': {ex}"))
        seen = set()
        found = [h for h in found if not (h["url"] in seen or seen.add(h["url"]))]
        found = [h for h in found if all(any(a in h["title"].lower() for a in t.split("|")) for t in terms)]
        found.sort(key=lambda h: (h["price"] is None, h["price"] or 0))

        rejected = 0
        if use_llm and found:
            try:
                found, rejected = verify(item, found[:20], model)
            except Exception as ex:  # fail open: keep everything rather than hide deals
                errors.append(scrub(f"llm verify / {name}: {ex}"))

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
                        "tag": tag, "parts": parts, "days": days, "rejected": rejected})
    con.commit()
    con.close()
    if json.dumps(cache, sort_keys=True) != before:
        CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
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
        rej = f" &middot; {r['rejected']} rejected by LLM" if r.get("rejected") else ""
        out.append(f'<div class="card {r["tag"]}"><b>{e(r["name"])}</b>'
                   f'<div class=price>{money(b["price"])} <span class=mut>via {link(b)}</span></div>'
                   f'<div>{e("; ".join(r["parts"]))}</div>'
                   f'<div class=mut>{e(b["title"][:120])} &middot; {r["days"]} days of tracked history{rej}</div>'
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