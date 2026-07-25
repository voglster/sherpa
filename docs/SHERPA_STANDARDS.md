# Sherpa Tool Standards

Every tool in `tools/` must follow this contract.

## File Structure

Sherpa follows the [AXI standard](https://axi.md/) for agent-ergonomic CLI design: TOON output instead of raw JSON, strict flag parsing, and an output contract designed to be read by an agent rather than a human at a terminal.

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==0.1.3"]
# ///
"""
name: my_tool_name
description: One-line summary of what this tool does.
categories: [category1, category2]
axi: true
usage: |
  subcommand <POSITIONAL_ARG> [--flag VALUE]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate
```

`python-toon==0.1.3` is the pinned dependency for every AXI-conformant tool; it installs as the `toon` module, but tools never import `toon` directly (see "The render API" below). Add whatever other deps the tool needs alongside it in the `dependencies` list.

## Docstring Format

The module docstring is YAML with these fields:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Snake-case identifier. **Must match filename** without `.py`. |
| `description` | Yes | One-line human-readable summary. Searched by `tool_search`. |
| `categories` | Yes | List of lowercase tags for search grouping. |
| `secrets` | No | List of vault key names the tool requires. |
| `usage` | No | Multi-line CLI usage examples showing subcommands and args. Returned in `tool_search` results. |
| `axi` | No | `true` once the tool has been converted to the AXI contract below. See "Migration state" under New Tool Checklist. |

## The render API

`sherpa/render.py` is the shared output boundary. Every AXI-conformant tool reaches it the same way, via the two-line preamble shown in the template above:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate
```

It exposes:

- `emit(payload: dict, *, as_json: bool = False) -> None` — encodes `payload` as TOON to stdout, or as indented JSON when `as_json=True` (wire this to the tool's `--json` flag). **Always go through `emit()`, never call `toon.encode` directly** — `render.py` patches a python-toon 0.1.3 spec bug (it under-quotes `#`-leading strings and raw control characters per TOON SPEC §15 and §7.1) by overriding `toon.primitives.is_safe_unquoted` at import time. Calling `toon.encode` yourself silently loses that fix.
- `fail(message: str, *, help: str | None = None, usage: bool = False) -> NoReturn` — prints `error: <message>` (and `help: <help>` if given) to stdout and exits: `2` if `usage=True`, otherwise `1`. Pass `usage=True` whenever the caller must change something before retrying is worth attempting — a flag *or* the environment (see "Exit codes" below).
- `truncate(text: str, limit: int = 1000) -> tuple[str, str | None]` — returns `(preview, notice)`; `notice` is `None` if `text` was short enough to pass through untouched, otherwise a string reporting the total size.
- `bin_line(executable: str | Path) -> str` — formats a `bin: ~/path/to/tool` line (relative to `$HOME` when possible) for no-args output.
- `parse_strict(parser, subparsers: dict[str, ArgumentParser] | None = None, argv=None) -> Namespace` — parses `argv` with `allow_abbrev=False` set on every parser involved, and exits (via `fail`, code 2) naming the first unrecognized flag and listing the valid set for the chosen subcommand. **Sherpa tools do not accept abbreviated flags**: `--stat` is rejected rather than silently resolved to `--state`. This is deliberate and matches AXI §6's canonical example.
- `TOOL_PREAMBLE` — the two-line snippet above, as a string, for tools that want to assemble it programmatically.

## Conventions

- **Shebang:** `#!/usr/bin/env python3`
- **PEP 723:** Every tool must have an inline script metadata block (`# /// script` ... `# ///`), pinning `python-toon==0.1.3`.
- **Args:** Use `argparse`, parsed via `parse_strict()` — never call `parser.parse_args()` directly. `--help` is automatic.
- **stdout is TOON**, produced only through `emit()`. Every tool accepts `--json` for the previous shape.
- **Exit codes:**
  - `0` — success, including idempotent no-ops.
  - `1` — the operation failed and retrying the same invocation is the reasonable next step, or nothing the caller controls would change the outcome: network errors, Jira 5xx, a video with no captions, a wrapped tool that crashed.
  - `2` — **the caller must change something before retrying: flags *or* environment.** Unknown or missing flags, malformed values, a missing vault secret, a missing dependency binary, and rejected/unauthorized credentials (Jira 400/401/403) are all `2`. The test is "would re-running this identically ever work?" — if not, it is `2`. This is deliberately wider than "usage error": an agent reads `2` as *fix the invocation or the setup*, and `1` as *the world was uncooperative*.
  - **This inverts sherpa's old exit-code meanings** (previously `1` was usage error and `2` was runtime error) — do not carry the old mapping into a converted tool.
  - Within one tool, the same condition must always yield the same code, including from subcommands that are not yet converted.
- **A missing secret MUST report on both channels** and exit `2`: `MISSING_SECRET: <KEY>` on stderr **and** the structured `error:`/`help:` pair on stdout via `fail()`. This does not contradict "stdout is TOON, stderr is diagnostics" — stdout carries the structured error the agent parses, stderr carries the diagnostic marker. That marker is load-bearing: it is the documented signal a calling agent recovers from (prompt the user for the value, `sherpa vault_manager set <KEY> <VALUE>`, retry), so a conversion that emits only the stdout error breaks that workflow silently. See the `Secrets:` snippet below for the exact shape.
- **Errors go to stdout** via `fail()`, with an actionable `help:` line naming the full invocation the caller should run, `sherpa` prefix included (`sherpa youtube version`, not `youtube version`) — that is the string an agent has to execute. Translate dependency errors; never leak tracebacks or the wrapped tool's name.
- **stderr is diagnostics only.** Never mix progress into stdout — an agent reads "Fetching..." as data.
- **Minimal default schemas**: 3-4 fields in lists. Long-form content lives in detail views. Offer `--fields` for more.
- **Truncate, never omit**: use `truncate()`, include the total size, and suggest `--full` only when truncation happened.
- **Report true totals**: `count: 30 of 847 total`, so the agent does not paginate to find out.
- **Definitive empty states**: state the zero with context.
- **Reject unknown flags** by name via `parse_strict()` before any dependency call, exit 2, listing the valid set.
- **No interactive prompts.** Every operation completable by flags alone; suppress prompts from wrapped tools.
- **No-args prints live content** plus `bin:` and `description:` lines — not a usage manual.
- **Contextual hints** on list and mutation output, omitted on detail views; dynamic values as `<id>` placeholders, never guessed.
- **`help` is always a list of strings**, even when there is exactly one hint — `"help": ["sherpa jira_issues get <ISSUE_KEY>"]`, never a bare string. TOON encodes a string and a one-element list differently, so a tool that varies the shape forces the agent to type-check the key before reading it. Each hint names a full invocation with the `sherpa` prefix, same rule as `fail()`'s `help:` line.
- **No-args home view is plain lines, not a TOON document** — `bin:`/`description:`/`hint:` prefixed lines printed directly, deliberately outside `emit()`. It is orientation for an agent that invoked the tool blind, not a payload to parse.
- **Secrets:** Read `~/.sherpa/vault.json` directly:
  ```python
  vault_path = Path.home() / ".sherpa" / "vault.json"
  vault = json.loads(vault_path.read_text()) if vault_path.exists() else {}
  token = vault.get("MY_SECRET")
  if not token:
      print("MISSING_SECRET: MY_SECRET", file=sys.stderr)
      fail("missing secret MY_SECRET", help="sherpa vault_manager set MY_SECRET <value>", usage=True)
  ```

## New Tool Checklist

1. Create `tools/<name>.py` following the template above.
2. Ensure `name` in YAML docstring matches the filename (without `.py`).
3. Add only the deps the tool needs to the PEP 723 block, alongside the pinned `python-toon==0.1.3`.
4. Test: `tool_run("<name>", "--help")`
5. Test: run with valid args via `tool_run("<name>", "<args>")`, verify TOON output (and `--json` output matches the pre-conversion shape).
6. The tool will be auto-indexed on the next `tool_search` call.

### Migration state

`axi: true` in the docstring marks a tool as converted to the contract above; its absence means the tool is unconverted and still follows the old JSON/argparse conventions. Sherpa is migrated tool-by-tool, so at any point some tools will have the marker and most will not — that is expected, not a bug. Check `sherpa list` to see which tools are currently conformant.

### Compliance checklist

A reviewer can run this against any tool to confirm it meets the contract:

- [ ] Output goes through `emit()` (TOON), never raw `print(json.dumps(...))` or `toon.encode` directly
- [ ] `--json` flag present, emitting the pre-conversion JSON shape
- [ ] Exit codes correct: `0` success/no-op, `1` uncooperative world, `2` caller must change flags or environment — and consistent across every subcommand of the tool
- [ ] Errors printed to stdout via `fail()`, with a `help:` line naming a full `sherpa ...` invocation
- [ ] Any `help` key in emitted output is a list of strings, never a bare string
- [ ] A missing secret emits `MISSING_SECRET: <KEY>` on stderr as well as the stdout error, and exits 2
- [ ] List/default schemas are ≤4 fields, with `--fields` for more
- [ ] True totals reported (`count: N of M total`), not just the page size
- [ ] Empty states are explicit and state the zero with context
- [ ] Unknown flags rejected via `parse_strict()`, exit 2, valid set listed
- [ ] No-args invocation shows live content plus `bin:` and `description:` lines
- [ ] Contextual hints appear on list/mutation output only, never on detail views
- [ ] `axi: true` present in the docstring

---

## Workflows

Workflows are YAML files in `workflows/` that describe multi-step processes chaining multiple tools together. They are **informational** — Claude reads the steps and executes them; there is no automated runner. Workflows are indexed alongside tools and appear in `tool_search` results.

### YAML Schema

```yaml
name: my_workflow_name
description: One-line summary of what this workflow accomplishes.
categories: [category1, category2]
steps:
  - tool: tool_name
    action: "What to do with this tool"
    args: "--flag <placeholder>"
  - tool: null
    action: "A reasoning or decision step (no tool invoked)"
```

### Field Reference

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Snake-case identifier. **Must match filename** without `.yaml`. |
| `description` | Yes | One-line human-readable summary. Searched by `tool_search`. |
| `categories` | Yes | List of lowercase tags for search grouping. |
| `steps` | Yes | Ordered list of step objects. |

### Step Fields

| Field | Required | Description |
|-------|----------|-------------|
| `tool` | Yes | Tool name (string) or `null` for reasoning/decision steps. |
| `action` | Yes | Human-readable description of what to do in this step. |
| `args` | No | CLI args template. Use `<placeholder>` for values to be filled in at runtime. |

### Conventions

- **Filename must match name:** `workflows/my_workflow.yaml` → `name: my_workflow`
- **`tool: null`** for reasoning steps: analysis, decisions, branching logic
- **Keep steps atomic:** each step should do one thing
- **Placeholders:** use `<angle_brackets>` for values that vary per invocation

### New Workflow Checklist

1. Create `workflows/<name>.yaml` following the schema above.
2. Ensure `name` matches the filename (without `.yaml`).
3. Include at least `name`, `description`, `categories`, and `steps`.
4. Verify each `tool` reference matches an existing tool name (or use `null`).
5. Test: reindex and confirm the workflow appears.
6. Test: search for a relevant keyword via `tool_search` and verify the workflow is returned.
