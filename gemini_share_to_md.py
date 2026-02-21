#!/usr/bin/env python3
"""Fetch a Gemini shared conversation and save it as a markdown file."""

import argparse
import re
import sys
import time
from pathlib import Path

from markdownify import markdownify as md
from playwright.sync_api import sync_playwright


def fetch_gemini_share(url: str, timeout_ms: int = 30000, headed: bool = False) -> str:
    """Load a Gemini share URL with a browser and extract the markdown content."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        # Wait for the markdown content to render
        page.wait_for_selector(".markdown", timeout=timeout_ms)
        # Extra settle time for SPA hydration
        time.sleep(3)

        el = page.query_selector(".markdown")
        if not el:
            browser.close()
            raise RuntimeError("No .markdown element found on the page.")

        html = el.inner_html()
        browser.close()

    # Convert HTML to clean markdown
    result = md(html, heading_style="atx", bullets="-", strip=["div"])
    # Clean up excessive blank lines
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def slugify(url: str) -> str:
    """Turn the share URL into a safe filename."""
    share_id = url.rstrip("/").split("/")[-1]
    return f"gemini-share-{share_id}"


def main():
    parser = argparse.ArgumentParser(description="Download a Gemini shared conversation as markdown.")
    parser.add_argument("url", help="Gemini share URL (e.g. https://gemini.google.com/share/abc123)")
    parser.add_argument("-o", "--output", help="Output file path (default: auto-generated from share ID)")
    parser.add_argument("--timeout", type=int, default=30000, help="Page load timeout in ms (default: 30000)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    args = parser.parse_args()

    if "gemini.google.com/share/" not in args.url:
        print("Error: URL doesn't look like a Gemini share link.", file=sys.stderr)
        sys.exit(1)

    output = args.output or f"{slugify(args.url)}.md"

    print(f"Fetching {args.url} ...")
    result = fetch_gemini_share(args.url, timeout_ms=args.timeout, headed=args.headed)

    Path(output).write_text(result, encoding="utf-8")
    print(f"Saved to {output}")


if __name__ == "__main__":
    main()
