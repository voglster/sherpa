# `sherpa review` — deflect the dumb work

**Status:** approved 2026-07-26. Supersedes
`docs/superpowers/specs/2026-07-25-local-validation-gate-design.md`.

## Goal

Multiply one reviewer's scale by encoding their judgement into a system that runs on
every change, fixes what it is confident about, and surfaces only what genuinely needs
a human perspective. Everything else is deflected without a human read.

Two properties make this design different from the gate it replaces:

- **Cost matters, wall-clock does not.** Local models on owned hardware mean review can
  run on every change without rationing. This is the whole point: `snd-housekeeping` in
  the AIO repo already does this work, but its own skill calls it optional because it
  "can burn a fair amount of tokens", so it does not get run, so convention drift lands
  on the reviewer at PR time.
- **Reliability is bought with repetition.** Because time is free, the model runs N
  times and only findings that survive a majority vote are trusted.

## Non-goals

- No GitHub dependency. The input is a diff; where it came from is a detail.
- No posting to other people's pull requests in v1. That is outward-facing and must be
  earned by evidence the findings are good.
- No frontier escalation in v1. Raised findings go to the human, who has a frontier
  agent already.

## Why the previous design is abandoned

The local-validation-gate plan aimed at "cheap enough to gate every change", which
pulled in `no-mistakes`, the Pi coding agent, a LiteLLM catalog, and a five-model
bake-off. That constraint only binds at per-commit volume. At the real volume — a
handful of changes a day — the machinery cost far exceeded what it saved. A simple
system that solves one problem, extended as it earns trust, is the correction.

Salvaged from that work:

| Asset | Why it survives |
| --- | --- |
| `docs/gate-rubric.md` | The conventions as numbered, citable items. The actual encoded judgement. |
| `scripts/gate_bench.py` | An eval harness for reviewers: plant known violations, measure catch rate. |
| `gate.py`'s `probe` | Fail-closed model check. An empty review reads as "no problems found". |

Dropped: `gate.py`'s `config` (generates Pi's provider file), `gate.py`'s `push`
(drives `no-mistakes`), `.no-mistakes.yaml`, and the LiteLLM catalog work.

## What the measurements already settled

From the reasoning-enabled re-benchmark (`scripts/results-reasoning.json`, five trials
per model), which the design depends on:

- **Ten of sixteen rubric items are greppable** (1, 2, 4, 5, 6, 7, 8, 11, 14, 15). A
  model is redundant on these; local models catch them 20/20.
- **Item 3 is unreachable** — caught once in twenty trials with reasoning on and no
  budget pressure. Judgement-shaped items are where a model is needed and where these
  models are weakest.
- **Item 13 is a rubric defect** — flagged against clean code in 12 of 20 trials, 5/5
  deterministically by two models. Fix the rubric, not the model.
- **Voting fixes instability, not systematic error.** `glm-4.7-flash` dropped item 4 in
  1 of 5 trials (a vote erases that); `gemma4:31b`'s item-13 false positive appeared 5
  of 5 (a vote must not hide it).
- **Latency is no longer a selection criterion.** `gemma4:31b` was chosen partly for
  being 2.2x faster than `qwen3.6:27b-q4_K_M`. With time free, the model must be
  re-picked on recall and precision alone — and `qwen3.6:27b-q4_K_M` is the only
  candidate that has ever caught a judgement item.

## Architecture

A single sherpa tool, `tools/review.py`, AXI-conformant per `docs/SHERPA_STANDARDS.md`.

### Input: a diff from anywhere

One seam, `resolve_diff()`, so the review core never knows the source:

| Invocation | Diff |
| --- | --- |
| no args | uncommitted work — `git diff HEAD` |
| `--commit <ref>` | that commit against its parent |
| `--branch` | current branch against its merge-base with the configured base |
| `--pr <url>` | via `gh pr diff` — deferred, not v1 |

### Config: two repo-agnostic inputs

`.review.yaml` at the repo root is the only per-repo knowledge, which is what makes the
tool portable:

```yaml
rubric: docs/gate-rubric.md
test: uv run --with pytest pytest tests/ -q
base: master
```

The model comes from `~/.sherpa/gate.json`, which already exists.

### The run

1. **Resolve the diff.** Nothing to review is stated explicitly and exits 0.
2. **Probe the model.** Fail closed: an empty or unparseable response is a failure that
   stops the run, never a pass.
3. **Checkpoint.** If the tree is dirty, auto-commit a `wip:` restore point so every
   edit that follows is revertable with one command.
4. **N passes** (default 5) of the model over rubric + diff.
5. **Vote.** Findings appearing in a majority of passes are *confident*; the rest are
   *raised*.
6. **Apply the confident fixes** to the working tree.
7. **Run the configured test command** — at the checkpoint first, to establish a
   baseline, then again after the fixes. Time is free, and without the baseline a
   pre-existing failure gets blamed on the tool's own edits. Pass → report what was
   fixed. Fail → revert to the checkpoint, move every finding to *raised*, and attach
   the failure. A broken tree is never left behind. Skipped entirely when no fix
   applied.
8. **`emit()`** the result: `fixed`, `raised`, `tests`.

The *raised* list is the only output a human reads.

### Isolation

Pure functions, testable with no network, git, or subprocess:

- prompt construction from rubric + diff
- finding parsing (item number, `file:line`, description, optional fix)
- the vote
- planning a fix application against file content

I/O boundary, thin and separately tested: git invocations, the ollama HTTP call, the
test subprocess.

## Error handling

- **Empty or unparseable model response** — fail closed, exit 1. The design's central
  hazard: a blank review is an empty finding list, which reads as "no problems found".
- **A finding citing no rubric item number** — rejected as unparseable rather than
  guessed at, per the rubric's own contract.
- **A fix that does not apply cleanly** — that finding moves to *raised*, the run
  continues.
- **Tests fail after fixes** — revert to the checkpoint, everything becomes *raised*.
- **Tests were already failing before fixes** — detected by running them at the
  checkpoint; the tool reports this and does not attribute a pre-existing failure to
  its own edits.
- **Exit codes** — `0` clean or fixed, `1` model or environment failure, `2` caller
  must change flags or config.

## Testing

- Unit tests for every pure function, per repo convention (load by path with
  `importlib.util.spec_from_file_location`).
- Vote logic tested against the real recorded trial data in
  `scripts/results-reasoning.json`, including the two cases that matter: an unstable
  finding that a vote should erase, and a systematic false positive it must not hide.
- The fix/revert path tested against a scratch git repo, asserting that a failing test
  command leaves the tree byte-identical to the checkpoint.

## Task 0, before any code: extend the bench

There is **no evidence** a local model can judge comment discipline or test
readability. Item 3 suggests it may not. So the first task is not the tool:

Extend `scripts/gate_bench.py` with a second diff planting judgement-shaped
violations — a narration comment, a comment restating a well-named symbol, a docstring
repeating its signature, an unreadable test name — plus the majority-vote logic, run
across all three live candidates.

**This gates the build.** If recall on comment discipline resembles item 3's 1-in-20,
the premise is wrong and an hour was spent instead of another two days. If it holds up,
the same run picks the model on the axis that now matters.

## Deferred, in likely order

1. `--pr <url>` input, reading a diff from GitHub.
2. Posting findings as PR comments — outward-facing, so gated on the findings being
   demonstrably good.
3. Automatic triggering (a loop or cron over changed branches).
4. Frontier escalation for the *raised* bucket.
5. Fixing rubric item 13, and either splitting item 3 into mechanical assertions or
   marking it as escalation-only.
6. Replacing the ten greppable rubric items with deterministic checks, so the model is
   only asked the questions it is good at.
