# AXI Foundation for Sherpa

Date: 2026-07-25
Status: approved, not yet implemented

## Problem

Sherpa's tool contract (`docs/SHERPA_STANDARDS.md`) was written before agent-ergonomic
CLI design had an articulated standard. AXI ("Agent eXperience Interface",
<https://axi.md/>) is now that standard, and it conflicts with sherpa's contract in ways
that cost tokens on every tool call across 20 tools.

Two of the conflicts are semantic inversions, not additions:

| Concern | Sherpa today | AXI |
| --- | --- | --- |
| stdout format | JSON | TOON, ~40% fewer tokens |
| Error channel | stderr | stdout, structured, so the agent can act on it |
| Exit codes | 1 = usage, 2 = runtime | 1 = error, 2 = usage; 0 for idempotent no-ops |
| No arguments | prints `--help` | prints live content |

Nothing consumes sherpa exit codes today — `cli/sherpa_cli.py:185` dispatches with
`subprocess.call` and returns the child's code unmodified — so correcting the exit-code
semantics breaks no caller.

Beyond the conflicts, AXI specifies rules sherpa's contract is silent on: minimal default
schemas, truncation that reports total size, pre-computed aggregates, definitive empty
states, rejecting unknown flags by name, and contextual next-step hints.

## Goals

1. A TOON encoder sherpa tools share.
2. A rewritten tool contract that is AXI-conformant.
3. Two tools converted as pilots.
4. A measurement of TOON's savings on real sherpa payloads, not the published claim.
5. A triage decision for each existing tool, so fan-out is mechanical.

## Non-goals

- Converting all ~20 tools. That is follow-on work, gated on the pilot measurement.
- AXI §7 (session-lifecycle hooks injecting an ambient dashboard). It assumes one binary
  per domain; sherpa is a multiplexer, and the user already loads a `SessionStart` hook
  for superpowers. Twenty tools of ambient state would cost more tokens than TOON saves.
- Replacing tools that have an existing AXI equivalent. Phase A records the decision;
  acting on it is separate work.

## Design

### 1. `sherpa/toon.py`

A TOON encoder applied only at the output boundary. Internal tool logic keeps using
dicts and lists.

Before writing one: verify whether the `toon` or `toon-format` packages on PyPI are
legitimate TOON implementations. Both names resolve, neither is confirmed. If a
maintained encoder exists and matches the spec, depend on it. Otherwise implement one.

Public surface, if implemented here:

```python
def encode(value, *, name: str | None = None) -> str
```

Handles the two shapes AXI output actually uses:

- **Uniform collections** → tabular form with a length and field header:
  `tasks[2]{id,title,status}:` followed by one indented comma-separated row each.
- **Single objects** → indented `key: value` lines, nesting for sub-objects.

Encoder requirements:

- Quote a field only when its content requires it (contains a comma, quote, newline, or
  leading/trailing whitespace); escape embedded quotes. Unnecessary quoting is the main
  way a TOON encoder silently loses its token advantage.
- Non-uniform lists (rows with differing keys) fall back to per-item objects rather than
  emitting a header that misdescribes the rows.
- `None` renders as an empty field, not the string `None`.
- Reject values it cannot represent loudly, so a tool never emits malformed TOON.

Tested against the examples in the TOON spec (<https://toonformat.dev/reference/spec.html>,
to be read before implementation) plus sherpa-shaped fixtures.

### 2. Rewritten `docs/SHERPA_STANDARDS.md`

The contract becomes:

- **stdout is TOON.** Every tool accepts `--json` to emit the old format, for the rare
  human or script that wants it.
- **Exit codes**: 0 success, including idempotent no-ops; 1 error; 2 usage error.
- **Errors go to stdout**, structured, with an actionable `help:` line naming a sherpa
  command. Dependency output (API errors, tracebacks) is translated, never leaked.
- **stderr is diagnostics only** — progress, debug. Agents do not read it.
- **Minimal default schemas**: 3-4 fields in list output. Long-form content belongs in
  detail views. `--fields` requests more.
- **Truncation**: never omit a large field; include a preview, state the total size, and
  suggest the `--full` escape hatch only when truncation actually happened.
- **Aggregates**: list output reports the true total, not just the page size.
- **Empty states**: state the zero with context, so the agent does not re-run to confirm.
- **Unknown flags are rejected by name** before any dependency call, exit 2, with the
  command's valid flags listed inline so the agent self-corrects in one turn.
- **No interactive prompts.** Every operation completable by flags alone.
- **No-args prints live content** relevant to the working directory, plus a `bin:` line
  with the executable path (home collapsed to `~`) and a one-line description.
- **Contextual hints** on list and mutation output; omitted on detail views, where they
  are noise. Dynamic values appear as placeholders (`<id>`), never guessed.

Ends with a per-tool compliance checklist, so new tools are born conformant and existing
ones can be audited against it.

### 3. Pilots

Two tools, chosen to exercise different halves of the standard:

- **`jira_issues search`** — list-shaped, the largest real payload in the suite. Exercises
  minimal schemas, aggregates, empty states, contextual hints.
- **`youtube info`** — detail-shaped. Exercises truncation with total size (the
  description field), nested arrays (chapters), and the no-args home view. It currently
  violates JSON output, no-args help, errors-to-stderr, and the exit-code convention, so
  it is a fair representative of the existing suite.

Each pilot keeps its current behavior available under `--json`, and gains tests for the
output-shape rules that are cheap to assert (empty state, truncation notice, unknown-flag
rejection).

### 4. Measurement

The 40% claim is published by AXI's author and measured on his tools. Phase A checks it
against sherpa payloads.

Method: capture real output from each pilot as both JSON and TOON, then token-count both
with Anthropic's `count_tokens` endpoint — the tokenizer that actually bills, rather than
a `tiktoken` approximation. Record per-payload savings in the phase's final report.

Three payload sizes per pilot, since savings scale with row count: a single item, a
typical list, and a large list. A result materially below 40% is a finding worth having,
not a failure — it changes whether converting the remaining 18 tools is worth the work.

### 5. Catalog triage

A decision table committed alongside the standard, assigning every existing tool one of:

- **AXI-ify** — genuinely yours, no equivalent exists. Expected: `jira_issues`,
  `jira_admin`, `jira_pulse`, `fleet`, `knowledge`, `notes_search`, `lumbergh`,
  `youtube`, `sentry_issues`, `notify`, `vault_manager`.
- **Replace** — an existing AXI covers it, so maintaining ours is waste. Candidates:
  `slack_messenger` → `slack-axi`, `client_db` → `mongodb-axi`.
- **Leave** — thin enough that conversion is not worth it: `reindex`, `web_read`,
  `web_search`.

The table is a decision record only. Executing the replacements is separate work, because
each swap needs its own verification that the replacement covers the workflows in use.

## Error handling

The migration's risk is a half-converted suite where some tools emit TOON and others
JSON, and the agent cannot tell which. Mitigations:

- The standard is rewritten first, so converted and unconverted tools are distinguishable
  by an explicit conformance marker in each tool's docstring.
- `sherpa list` reports conformance, making the migration's state visible at a glance.
- `--json` on every converted tool guarantees an escape hatch if TOON output turns out to
  break something downstream.

## Testing

- `sherpa/toon.py` — unit tests against TOON spec examples and sherpa-shaped fixtures:
  quoting only when required, non-uniform fallback, `None` handling, nesting.
- Pilots — tests for empty-state wording, truncation notice with total, unknown-flag
  rejection and exit code, `--json` still producing the previous shape.
- Measurement — a committed script so the comparison is reproducible when tools change,
  not a one-off number in a commit message.

## Follow-on phases (out of scope here)

- **B. no-mistakes** — adversarial review in a fresh context, e2e evidence on the PR, risk
  tiering so low-risk diffs skip human review. Adopt <https://github.com/kunchenguid/no-mistakes>,
  adapt to jira keys and the user's review skills.
- **C. lavish in lumbergh** — `lavish-axi` as a review surface inside `~/src/personal/lumbergh`,
  plus better tmux interfacing.
- **D. Ephemeral worktrees** — treehouse's auto-cleanup without its pooling; folds into
  `fleet`.
- **E. Skill audit** — evaluate installed skills for token cost and benefit.
