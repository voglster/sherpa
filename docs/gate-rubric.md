# Gate rubric

This is the checklist the local review gate (`no-mistakes` → `pi` → the ollama
model on the RTX 4090) checks a diff against. It is derived from two sources:
the compliance checklist at the end of `docs/SHERPA_STANDARDS.md`, and the
commit-message and comment-discipline rules in `~/.claude/CLAUDE.md`. No item
here should exist outside those two documents.

**A finding must cite the item number below (e.g. "violates item 6").** A
finding that does not cite a number is unparseable and must be rejected
rather than guessed at.

1. Tool output goes through `emit()` (TOON) — never a raw `print(json.dumps(...))` or a direct call to `toon.encode`.
2. A `--json` flag is present and emits the pre-conversion JSON shape.
3. Exit codes follow the contract: `0` for success/no-op, `1` for an uncooperative external world, `2` when the caller must change flags or environment — applied consistently across every subcommand of the tool.
4. Errors are printed to stdout via `fail()`, not via a raw exception traceback or a bare `print()`.
5. Every `fail()` call includes a `help:` line naming a full `sherpa ...` invocation.
6. A missing secret prints `MISSING_SECRET: <KEY>` to stderr, in addition to the stdout error, and exits 2.
7. Any `help` key present in emitted output is a list of strings, never a bare string.
8. List/default output schemas expose at most 4 fields, with `--fields` available to request more.
9. Totals reported are true totals (`count: N of M total`), not just the size of the returned page.
10. Empty results are stated explicitly, with context for the zero, rather than an empty list or blank output.
11. Unknown flags are rejected via `parse_strict()`, exit 2, with the valid flag set listed.
12. Invoking the tool with no arguments shows live content plus `bin:` and `description:` lines — not a usage manual.
13. Contextual hints appear on list and mutation output only, and are omitted on detail-view output; each hint names a full `sherpa` invocation.
14. `axi: true` is present in the tool's YAML docstring.
15. The commit message contains no "🤖 Generated with Claude Code" line and no "Co-Authored-By: Claude" line.
16. A comment is evidence that the code failed to express its intent. Python is expressive enough that most comments are a naming or structuring failure wearing a disguise. For every comment added in the diff, report a violation **only if you can state the replacement** — the specific rename, extracted helper, or restructuring that carries the same intent without the prose. A finding that cannot name its replacement is not a finding; leave the comment alone. Two kinds of comment survive this test: one stating a fact the language cannot express (a non-obvious external-schema constraint a reader would otherwise get wrong), and one explaining *why* deliberately clever code is clever.
17. No narration comments — a comment whose text restates what the next line or block plainly does, including sequence markers like `# Arrange` / `# Act` / `# Assert`, `# Setup` / `# Teardown`, and `# Step 1` / `# First` / `# Then` / `# Finally`.

Items 1–14 come from the "Compliance checklist" in `docs/SHERPA_STANDARDS.md`.
Items 15–17 come from `~/.claude/CLAUDE.md`.

Items 16 and 17 split what was previously one item, along the line the model
benchmark drew: 17 is a string match a deterministic check should own, 16 is the
judgement only a model can make. Item 16's "name the replacement or it is not a
finding" rule exists because its predecessor was unfalsifiable — the same defect
that makes item 13 report violations against clean code.
