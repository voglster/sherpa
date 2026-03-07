#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: unsplash_search
description: Search Unsplash for photos and download them
categories: [image, unsplash, photos, search, creative]
secrets:
  - UNSPLASH_ACCESS_KEY
usage: |
  search --query 'mountain sunset' [--count 5] [--orientation landscape]
  download --query 'mountain sunset' [--output photo.jpg] [--size regular]
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
API_BASE = "https://api.unsplash.com"


def _load_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    value = vault.get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _client() -> httpx.Client:
    token = _load_secret("UNSPLASH_ACCESS_KEY")
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Client-ID {token}", "Accept-Version": "v1"},
        timeout=30,
    )


def cmd_search(args: argparse.Namespace) -> None:
    params = {"query": args.query, "per_page": args.count}
    if args.orientation:
        params["orientation"] = args.orientation

    with _client() as client:
        resp = client.get("/search/photos", params=params)
        if resp.status_code != 200:
            print(f"Search failed: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        data = resp.json()

    results = []
    for photo in data.get("results", []):
        results.append({
            "id": photo["id"],
            "description": photo.get("alt_description") or photo.get("description") or "",
            "author": photo.get("user", {}).get("name", ""),
            "urls": {
                "thumb": photo["urls"]["thumb"],
                "small": photo["urls"]["small"],
                "regular": photo["urls"]["regular"],
                "full": photo["urls"]["full"],
            },
            "link": photo["links"]["html"],
        })

    print(json.dumps({"total": data.get("total", 0), "results": results}, indent=2))


def cmd_download(args: argparse.Namespace) -> None:
    with _client() as client:
        resp = client.get("/search/photos", params={"query": args.query, "per_page": 1})
        if resp.status_code != 200:
            print(f"Search failed: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        results = resp.json().get("results", [])
        if not results:
            print(f"No photos found for: {args.query}", file=sys.stderr)
            sys.exit(2)

        photo = results[0]
        url = photo["urls"].get(args.size, photo["urls"]["regular"])
        author = photo.get("user", {}).get("name", "unknown")

        print(f"Downloading from {author}...", file=sys.stderr)
        img_resp = client.get(url)
        if img_resp.status_code != 200:
            print(f"Download failed: {img_resp.status_code}", file=sys.stderr)
            sys.exit(2)

        # Trigger download tracking per Unsplash API guidelines
        download_location = photo.get("links", {}).get("download_location")
        if download_location:
            client.get(download_location)

        Path(args.output).write_bytes(img_resp.content)
        print(f"Saved: {args.output}", file=sys.stderr)

    print(json.dumps({
        "file": args.output,
        "author": author,
        "description": photo.get("alt_description") or "",
        "link": photo["links"]["html"],
    }))


def main():
    parser = argparse.ArgumentParser(description="Search and download Unsplash photos.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("search", help="Search for photos")
    p.add_argument("--query", required=True, help="Search query")
    p.add_argument("--count", type=int, default=5, help="Number of results (default: 5)")
    p.add_argument("--orientation", default=None, choices=["landscape", "portrait", "squarish"], help="Filter by orientation")

    p = sub.add_parser("download", help="Download the top matching photo")
    p.add_argument("--query", required=True, help="Search query")
    p.add_argument("--output", default="photo.jpg", help="Output filename (default: photo.jpg)")
    p.add_argument("--size", default="regular", choices=["thumb", "small", "regular", "full"], help="Image size (default: regular)")

    args = parser.parse_args()

    match args.command:
        case "search":
            cmd_search(args)
        case "download":
            cmd_download(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
