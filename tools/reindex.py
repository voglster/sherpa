#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
name: reindex
description: Force a rebuild of the Sherpa tool metadata index.
categories: [sherpa, indexing, maintenance]
"""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Force a rebuild of the Sherpa tool metadata index.")
    parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["uv", "run", "--directory", str(project_root), "python", "-m", "sherpa.indexer"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(2)

    # Forward the JSON output
    print(result.stdout, end="")


if __name__ == "__main__":
    main()
