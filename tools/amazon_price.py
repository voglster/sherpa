#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==0.1.3", "httpx"]
# ///
"""
name: amazon_price
description: Look up live Amazon prices and stock by ASIN, or find ASINs by search, via Jina Reader (no API key).
categories: [web, shopping, price, amazon, research, read]
axi: true
usage: |
  lookup <ASIN_OR_URL> [<ASIN_OR_URL> ...] [--fields sold_by,url,rating] [--full] [--json]
  search <QUERY> [--limit 10] [--json]
risk: low
outputBudget: 3000
operations:
  lookup:
    tier: read
    argv: ["lookup", "{asins}"]
    optional:
      fields: "--fields"
      full: "--full"
    notes: "Reads the new-condition buybox price; one Jina Reader fetch per ASIN."
  search:
    tier: read
    argv: ["search", "{query}"]
    optional:
      limit: "--limit"
    notes: "Returns ASINs from an Amazon search page, for feeding back into lookup."
"""

import argparse
import concurrent.futures as futures
import json
import re
import sys
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate

JINA = "https://r.jina.ai/"
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
ASIN_IN_URL = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
DP_LINK = re.compile(r"/dp/([A-Z0-9]{10})")

# Amazon ships the buybox as JSON inside the page. The FIRST displayPrice is the
# new-condition offer; later ones are used/refurbished listings, so `min()` over
# them silently reports a used price as if it were the sale price.
BUYBOX = re.compile(r'"displayPrice"\s*:\s*"\$?([\d,]+\.\d{2})"')
TITLE = re.compile(r"Title:\s*Amazon\.com:?\s*(.{0,160})")
UNAVAILABLE = re.compile(r"Currently unavailable|out of stock", re.I)
IN_STOCK = re.compile(r"(In Stock|Only \d+ left in stock)", re.I)
SOLD_BY = re.compile(r"Sold by:\s*\\?n?\s*([A-Za-z0-9 .,&'-]{2,40})")
RATING = re.compile(r"([\d.]+) out of 5 stars")
REVIEWS = re.compile(r"([\d,]+) ratings")

BASE_FIELDS = ("asin", "price", "stock", "title")
EXTRA_FIELDS = ("sold_by", "url", "rating", "reviews", "currency")


def to_asin(token: str) -> str | None:
    token = token.strip()
    if ASIN_RE.match(token):
        return token
    found = ASIN_IN_URL.search(token)
    return found.group(1) if found else None


def read(url: str, timeout: float = 90.0) -> str:
    resp = httpx.get(
        JINA + url,
        timeout=timeout,
        follow_redirects=True,
        headers={"Accept": "text/markdown"},
    )
    resp.raise_for_status()
    return resp.text


def scrape(asin: str) -> dict:
    url = f"https://www.amazon.com/dp/{asin}"
    try:
        body = read(url)
    except Exception as exc:
        return {"asin": asin, "url": url, "error": type(exc).__name__}

    prices = BUYBOX.findall(body)
    title = TITLE.search(body)
    stock = IN_STOCK.search(body)
    sold_by = SOLD_BY.search(body)
    rating = RATING.search(body)
    reviews = REVIEWS.search(body)

    return {
        "asin": asin,
        "price": float(prices[0].replace(",", "")) if prices else None,
        "currency": "USD",
        "stock": "unavailable" if UNAVAILABLE.search(body)
                 else (stock.group(1).lower() if stock else "unknown"),
        "title": re.sub(r"\s+", " ", title.group(1)).strip(" :-") if title else "unknown",
        "sold_by": sold_by.group(1).strip() if sold_by else "unknown",
        "rating": float(rating.group(1)) if rating else None,
        "reviews": int(reviews.group(1).replace(",", "")) if reviews else None,
        "url": url,
    }


def cmd_lookup(args: argparse.Namespace) -> None:
    asins, bad = [], []
    for token in args.asins:
        asin = to_asin(token)
        (asins if asin else bad).append(asin or token)
    if bad:
        fail(
            f"not an ASIN or Amazon product URL: {bad[0]}",
            help="sherpa amazon_price lookup B07VP37KMB",
            usage=True,
        )

    seen: list[str] = []
    for asin in asins:
        if asin not in seen:
            seen.append(asin)

    with futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(scrape, seen))

    chosen = list(BASE_FIELDS)
    if args.full:
        chosen += list(EXTRA_FIELDS)
    elif args.fields:
        for name in (f.strip() for f in args.fields.split(",")):
            if name not in BASE_FIELDS + EXTRA_FIELDS:
                fail(
                    f"unknown field {name}",
                    help=f"valid fields: {', '.join(BASE_FIELDS + EXTRA_FIELDS)}",
                    usage=True,
                )
            if name not in chosen:
                chosen.append(name)

    items, failed = [], 0
    for row in rows:
        if row.get("error"):
            failed += 1
            items.append({"asin": row["asin"], "price": None, "stock": "fetch_failed",
                          "title": f"error: {row['error']}"})
            continue
        items.append({k: row.get(k) for k in chosen})

    priced = [r["price"] for r in rows if r.get("price")]
    payload = {
        "count": f"{len(items)} of {len(seen)} requested",
        "subtotal": round(sum(priced), 2) if priced else 0,
        "priced": f"{len(priced)} of {len(items)}",
        "items": items,
    }
    if failed:
        payload["fetch_failures"] = failed
    if not items:
        payload["items"] = []
        payload["note"] = "no ASINs given"
    payload["help"] = [
        "sherpa amazon_price lookup <ASIN> --full",
        "sherpa amazon_price search '<QUERY>'",
    ]
    emit(payload, as_json=args.json)


def cmd_search(args: argparse.Namespace) -> None:
    query = urllib.parse.quote_plus(args.query)
    try:
        body = read(f"https://www.amazon.com/s?k={query}")
    except Exception as exc:
        fail(f"search fetch failed: {type(exc).__name__}",
             help="sherpa amazon_price search '<QUERY>'")

    found: list[str] = []
    for asin in DP_LINK.findall(body):
        if asin not in found:
            found.append(asin)

    hits = found[: args.limit]
    payload = {
        "query": args.query,
        "count": f"{len(hits)} of {len(found)} found",
        "asins": hits or [],
    }
    if not found:
        payload["note"] = f"0 ASINs on the search page for {args.query!r}"
    payload["help"] = [
        f"sherpa amazon_price lookup {' '.join(hits[:3]) if hits else '<ASIN>'}",
    ]
    emit(payload, as_json=args.json)


def home() -> None:
    print(bin_line(Path(__file__).resolve()))
    print("description: Live Amazon prices by ASIN via Jina Reader (no API key)")
    print("hint: sherpa amazon_price lookup B07VP37KMB")
    print("hint: sherpa amazon_price search 'hdmi splitter 4k'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Amazon prices via Jina Reader.")
    sub = parser.add_subparsers(dest="command")
    registry: dict[str, argparse.ArgumentParser] = {}

    p = sub.add_parser("lookup", help="Price one or more ASINs / product URLs")
    p.add_argument("asins", nargs="+", help="ASIN(s) or Amazon product URL(s)")
    p.add_argument("--fields", help=f"extra fields: {','.join(EXTRA_FIELDS)}")
    p.add_argument("--full", action="store_true", help="include every field")
    p.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    registry["lookup"] = p

    p = sub.add_parser("search", help="Find ASINs by search query")
    p.add_argument("query", help="search terms")
    p.add_argument("--limit", type=int, default=10, help="max ASINs (default 10)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    registry["search"] = p

    args = parse_strict(parser, registry)

    match args.command:
        case "lookup":
            cmd_lookup(args)
        case "search":
            cmd_search(args)
        case _:
            home()


if __name__ == "__main__":
    main()
