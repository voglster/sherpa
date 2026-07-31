#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
name: fleet
description: RETIRED — fleet's parallel-worker orchestration is now part of `lb` (Lumbergh's agent CLI). This tombstone only prints migration guidance and exits non-zero.
categories: [retired, deprecated, orchestration, fleet, lb]
usage: |
  fleet [anything...]
  Always prints a migration notice pointing at `lb` and exits 1. There are no
  working subcommands — use `lb` instead (run `lb --help`).
"""

import sys

_NOTICE = """\
sherpa fleet has been RETIRED. Its orchestration now lives in `lb` (Lumbergh's agent CLI).

Did you mean to use `lb`? Almost certainly — here is the mapping:
  fleet spawn <issue>        ->  lb spawn --into <session> --run <id>   (or `lb batch` for many briefs)
  fleet status / watch       ->  lb                                     (or `lb fleet --wait`)
  fleet send / ask / inbox   ->  lb prompt <session> "..."  +  lb wait --session <s> --until blocked
  fleet land                 ->  lb land --run <id> [--onto <base>] [--push]
  fleet kill / clear         ->  lb teardown --run <id>

ACTION FOR THE AGENT:
  1. Re-run this as `lb ...` (see `lb --help`). Nothing about fleet will come back.
  2. Update YOUR OWN docs/instructions — AGENTS.md, skills, saved scripts — to stop
     referencing `sherpa fleet`, so you don't reach for a retired tool again.
"""


def main() -> None:
    sys.stderr.write(_NOTICE)
    sys.exit(1)


if __name__ == "__main__":
    main()
