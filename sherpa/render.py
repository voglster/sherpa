"""AXI-conformant output boundary shared by sherpa tools.

Tools run standalone under `uv run --script`, so they reach this module by
inserting the repo root on sys.path (see TOOL_PREAMBLE) and declaring
python-toon in their own PEP 723 dependency block.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

import toon

# python-toon 0.1.3 under-quotes two cases the TOON spec requires quoted
# (verified against the official fixture suite): a leading `#` reads back as
# a full-line comment to a spec-compliant decoder, silently deleting the row
# and desyncing the array's declared count from its contents; raw control
# characters violate the \uXXXX escaping the spec requires. Quoting contains
# both — it does not add the \u escaping a fully conformant encoder would.
#
# Installation must survive re-execution of this module (importlib.reload, or
# a test loading it a second time via spec_from_file_location): re-executing
# would otherwise wrap the already-installed wrapper, and a module-global
# reference to the pristine function would rebind to that wrapper and recurse
# until the stack blows. The pristine function is therefore held in a closure,
# the wrapper is marked, and a marked function is left alone. `__wrapped__`
# lets a caller that needs the pristine encoder (see the conformance suite)
# recover it.
def _install_stricter_quoting() -> None:
    pristine = toon.primitives.is_safe_unquoted
    if getattr(pristine, "_sherpa_stricter_quoting", False):
        return

    def stricter_quoting(value: str, delimiter: str = ",") -> bool:
        if value.startswith("#"):
            return False
        if any(ord(character) < 0x20 for character in value):
            return False
        return pristine(value, delimiter)

    stricter_quoting._sherpa_stricter_quoting = True
    stricter_quoting.__wrapped__ = pristine
    toon.primitives.is_safe_unquoted = stricter_quoting


_install_stricter_quoting()

TOOL_PREAMBLE = """sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate"""


def emit(payload: dict[str, Any], *, as_json: bool = False) -> None:
    print(json.dumps(payload, indent=2) if as_json else toon.encode(payload))


def fail(message: str, *, help: str | None = None, usage: bool = False) -> NoReturn:
    print(f"error: {message}")
    if help:
        print(f"help: {help}")
    sys.exit(2 if usage else 1)


def truncate(text: str, limit: int = 1000) -> tuple[str, str | None]:
    if len(text) <= limit:
        return text, None
    return text[:limit], f"... (truncated, {len(text)} chars total)"


def bin_line(executable: str | Path) -> str:
    path = Path(executable)
    try:
        return f"bin: ~/{path.relative_to(Path.home())}"
    except ValueError:
        return f"bin: {path}"


def parse_strict(
    parser: argparse.ArgumentParser,
    subparsers: dict[str, argparse.ArgumentParser] | None = None,
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """Parse argv, rejecting unrecognized flags by name instead of ignoring them.

    argparse reports extras against the top-level parser, which would list the
    wrong flag set, so extras are attributed back to the chosen subcommand.
    argparse's default abbreviation matching (e.g. `--stat` silently resolving
    to `--state`) would also let unknown flags slip past as "known", so it is
    disabled on every parser involved before parsing.
    """
    parser.allow_abbrev = False
    for subparser in (subparsers or {}).values():
        subparser.allow_abbrev = False
    args, extras = parser.parse_known_args(argv)
    if not extras:
        return args

    command = getattr(args, "command", None)
    target = (subparsers or {}).get(command, parser)
    valid = sorted(
        option for action in target._actions for option in action.option_strings
    )
    scope = f" for `{command}`" if command else ""
    fail(
        f"unknown flag {extras[0]}{scope}",
        help=f"valid flags{scope}: {', '.join(valid)}",
        usage=True,
    )
