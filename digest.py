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
def scrub(text):
    """Keys end up in request URLs and therefore in error messages; never write them to the digest."""
    for var in ("REDDIT_RSS_KEY", "SERPAPI_KEY"):
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


# ---------- scoring ----------
def verdict(item, price, hist, days):
    """Return (tag, [phrases]) comparing price to usual price, 6-month low, and tracked history."""
    parts, tag = [], "ok"
    ref = item.get("usual") or (statistics.median(hist) if hist else None)