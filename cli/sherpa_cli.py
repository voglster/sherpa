"""Sherpa CLI — dispatches `sherpa <tool> [args...]` to tools in $SHERPA_HOME/tools/.

Each tool is a PEP 723 script invoked via `uv run --script`, so per-tool
dependencies stay isolated from the CLI's own environment.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

CONFIG_PATH = Path.home() / ".config" / "sherpa" / "config.toml"

RESERVED = {"list", "search", "help", "where", "set-home", "-h", "--help"}


# ---------------------------------------------------------------------------
# Home resolution
# ---------------------------------------------------------------------------

def _find_home() -> Path:
    """Resolve $SHERPA_HOME from env var, config file, or fail with guidance."""
    env = os.environ.get("SHERPA_HOME")
    if env:
        p = Path(env).expanduser()
        if (p / "tools").is_dir():
            return p
        sys.exit(f"SHERPA_HOME={env} does not contain a tools/ dir")

    if CONFIG_PATH.exists():
        cfg = tomllib.loads(CONFIG_PATH.read_text())
        home = cfg.get("home")
        if home:
            p = Path(home).expanduser()
            if (p / "tools").is_dir():
                return p
            sys.exit(f"Config home={home} does not contain a tools/ dir. Fix {CONFIG_PATH}.")

    sys.exit(
        "Sherpa home not configured.\n"
        "Run: sherpa set-home <path-to-jim_tools-clone>\n"
        "Or set the SHERPA_HOME environment variable."
    )


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_DOCSTRING_RE = re.compile(r'^"""(.*?)"""', re.DOTALL | re.MULTILINE)


def _parse_metadata(script: Path) -> dict:
    try:
        text = script.read_text()
    except OSError:
        return {"name": script.stem}
    m = _DOCSTRING_RE.search(text)
    if not m:
        return {"name": script.stem}
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {"name": script.stem}
    meta.setdefault("name", script.stem)
    return meta


def _list_tools(home: Path) -> list[tuple[Path, dict]]:
    out = []
    for script in sorted((home / "tools").glob("*.py")):
        if script.name.startswith("_"):
            continue
        out.append((script, _parse_metadata(script)))
    return out


def _resolve_tool(home: Path, name: str) -> Path:
    script = home / "tools" / f"{name}.py"
    if script.exists():
        return script
    sys.exit(f"Tool not found: {name}\nRun 'sherpa list' to see available tools.")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _print_top_help() -> None:
    print(
        "sherpa — CLI dispatcher for Sherpa tools\n"
        "\n"
        "Usage:\n"
        "  sherpa <tool> [args...]    Run a tool with the given args\n"
        "  sherpa list                List all available tools\n"
        "  sherpa search <query>      Search tools by name/description/category\n"
        "  sherpa help <tool>         Show usage for a specific tool\n"
        "  sherpa where               Print the resolved $SHERPA_HOME\n"
        "  sherpa set-home <path>     Persist a default Sherpa home in ~/.config/sherpa/config.toml\n"
    )


def cmd_list(_argv: list[str]) -> int:
    home = _find_home()
    width = 24
    for _, meta in _list_tools(home):
        print(f"  {meta.get('name', ''):<{width}} {meta.get('description', '')}")
    return 0


def cmd_search(argv: list[str]) -> int:
    if not argv:
        sys.exit("Usage: sherpa search <query>")
    home = _find_home()
    query = " ".join(argv).lower()
    width = 24
    matches = 0
    for _, meta in _list_tools(home):
        blob = " ".join([
            meta.get("name", ""),
            meta.get("description", ""),
            " ".join(meta.get("categories", []) or []),
            meta.get("usage", "") or "",
        ]).lower()
        if query in blob:
            print(f"  {meta.get('name', ''):<{width}} {meta.get('description', '')}")
            matches += 1
    if matches == 0:
        print(f"No tools matched: {query}", file=sys.stderr)
    return 0


def cmd_help(argv: list[str]) -> int:
    if not argv:
        sys.exit("Usage: sherpa help <tool>")
    home = _find_home()
    script = _resolve_tool(home, argv[0])
    meta = _parse_metadata(script)
    print(f"{meta.get('name')} — {meta.get('description', '')}")
    if meta.get("categories"):
        print(f"categories: {', '.join(meta['categories'])}")
    if meta.get("secrets"):
        print(f"secrets: {', '.join(meta['secrets'])}")
    if meta.get("usage"):
        print("\nUsage:")
        print(meta["usage"].rstrip())
    print("\n--- tool --help ---")
    result = subprocess.run(
        ["uv", "run", "--script", str(script), "--help"],
        capture_output=True,
        text=True,
    )
    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def cmd_where(_argv: list[str]) -> int:
    print(_find_home())
    return 0


def cmd_set_home(argv: list[str]) -> int:
    if not argv:
        sys.exit("Usage: sherpa set-home <path>")
    path = Path(argv[0]).expanduser().resolve()
    if not (path / "tools").is_dir():
        sys.exit(f"{path} does not contain a tools/ dir")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(f'home = "{path}"\n')
    print(f"Sherpa home set to {path}")
    return 0


def cmd_run(name: str, rest: list[str]) -> int:
    home = _find_home()
    script = _resolve_tool(home, name)
    return subprocess.call(["uv", "run", "--script", str(script), *rest])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        _print_top_help()
        sys.exit(0)

    cmd, rest = argv[0], argv[1:]
    dispatch = {
        "list": cmd_list,
        "search": cmd_search,
        "help": cmd_help,
        "where": cmd_where,
        "set-home": cmd_set_home,
    }
    if cmd in dispatch:
        sys.exit(dispatch[cmd](rest))
    sys.exit(cmd_run(cmd, rest))


if __name__ == "__main__":
    main()
