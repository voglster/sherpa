#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==0.1.3"]
# ///
"""
name: sherpa_admin
description: Introspect Sherpa itself and control which tools show up in discovery (progressive disclosure).
categories: [sherpa, admin, discovery, maintenance]
axi: true
usage: |
  status [--json]
  list [--all] [--json]
  hide <TOOL> [TOOL...] [--json]
  show <TOOL> [TOOL...] [--json]
operations:
  status:
    tier: read
    argv: ["status"]
  list:
    tier: read
    argv: ["list"]
    optional:
      all: "--all"
  hide:
    tier: write
    argv: ["hide", "{tool}"]
    notes: "Withholds a tool from discovery. It still runs; `show` puts it back."
  show:
    tier: write
    argv: ["show", "{tool}"]
notes: |
  Hidden tools disappear from `sherpa list`, `sherpa search` and the agent-facing
  tool_search. They still run: `sherpa <tool>`, `sherpa help <tool>` and tool_run
  are unaffected. Hiding shrinks the surface a newcomer has to read, it does not
  revoke access. State lives in ~/.config/sherpa/config.toml.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa import config as sherpa_config
from sherpa.render import bin_line, emit, fail, parse_strict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"


def _tool_names() -> list[str]:
    return sorted(p.stem for p in TOOLS_DIR.glob("*.py") if not p.name.startswith("_"))


def _describe(name: str) -> str:
    """First `description:` line from the tool's YAML docstring, without importing it."""
    try:
        for line in (TOOLS_DIR / f"{name}.py").read_text().splitlines()[:20]:
            if line.startswith("description:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _reject_unknown(names: list[str]) -> None:
    unknown = [n for n in names if n not in _tool_names()]
    if unknown:
        fail(
            f"no such tool: {', '.join(unknown)}",
            help="sherpa sherpa_admin list --all",
            usage=True,
        )


def _cmd_status(args: argparse.Namespace) -> None:
    names = _tool_names()
    hidden = sherpa_config.hidden_tools() & set(names)
    emit(
        {
            "home": str(PROJECT_ROOT),
            "config": str(sherpa_config.CONFIG_PATH),
            "tools": len(names),
            "active": len(names) - len(hidden),
            "hidden": len(hidden),
            "hidden_tools": sorted(hidden),
            "help": ["sherpa sherpa_admin list --all", "sherpa sherpa_admin hide <TOOL>"],
        },
        as_json=args.json,
    )


def _cmd_list(args: argparse.Namespace) -> None:
    names = _tool_names()
    hidden = sherpa_config.hidden_tools()
    rows = [
        {"name": n, "hidden": n in hidden, "description": _describe(n)}
        for n in names
        if args.all or n not in hidden
    ]
    emit(
        {
            "count": f"{len(rows)} of {len(names)} total",
            "tools": rows,
            "help": ["sherpa sherpa_admin hide <TOOL>", "sherpa sherpa_admin show <TOOL>"],
        },
        as_json=args.json,
    )


def _cmd_hide(args: argparse.Namespace) -> None:
    _reject_unknown(args.tools)
    hidden = sherpa_config.hidden_tools()
    sherpa_config.set_hidden(sorted(hidden | set(args.tools)))
    _report_change("hidden", args)


def _cmd_show(args: argparse.Namespace) -> None:
    _reject_unknown(args.tools)
    hidden = sherpa_config.hidden_tools()
    sherpa_config.set_hidden(sorted(hidden - set(args.tools)))
    _report_change("shown", args)


def _report_change(action: str, args: argparse.Namespace) -> None:
    hidden = sorted(sherpa_config.hidden_tools())
    emit(
        {
            action: sorted(args.tools),
            "hidden_tools": hidden,
            "count": f"{len(hidden)} of {len(_tool_names())} total",
            "help": ["sherpa sherpa_admin list --all"],
        },
        as_json=args.json,
    )


def _home() -> None:
    names = _tool_names()
    hidden = sherpa_config.hidden_tools() & set(names)
    print(bin_line(__file__))
    print("description: introspect Sherpa and control which tools appear in discovery")
    print(f"home: {PROJECT_ROOT}")
    print(f"tools: {len(names)} ({len(hidden)} hidden)")
    print("hint: sherpa sherpa_admin status")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="sherpa_admin", description="Introspect Sherpa and control tool visibility."
    )
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="Home, config path, and tool counts")
    listing = sub.add_parser("list", help="Tools, active ones by default")
    listing.add_argument("--all", action="store_true", help="Include hidden tools")
    hide = sub.add_parser("hide", help="Withhold tools from discovery")
    hide.add_argument("tools", nargs="+", metavar="TOOL")
    show = sub.add_parser("show", help="Return tools to discovery")
    show.add_argument("tools", nargs="+", metavar="TOOL")

    subparsers = {"status": status, "list": listing, "hide": hide, "show": show}
    for sp in subparsers.values():
        sp.add_argument("--json", action="store_true", help="Emit JSON instead of TOON")

    if not (argv if argv is not None else sys.argv[1:]):
        _home()
        return

    args = parse_strict(parser, subparsers, argv)
    match args.command:
        case "status":
            _cmd_status(args)
        case "list":
            _cmd_list(args)
        case "hide":
            _cmd_hide(args)
        case "show":
            _cmd_show(args)
        case _:
            _home()


if __name__ == "__main__":
    main()
