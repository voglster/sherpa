# `sherpa review` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A sherpa tool that reviews a diff against a rubric using a local model, auto-fixes what survives a majority vote, reruns the tests, and reports only the findings that need a human.

**Architecture:** One standalone PEP 723 script, `tools/review.py`, following the repo's one-file-per-tool pattern. Pure functions (config, prompt, parsing, voting, fix planning) occupy the top of the file and are unit-tested by path-loading; the I/O boundary (git, ollama HTTP, the test subprocess) is thin and sits below them. Input is a diff from any source behind one `resolve_diff()` seam, so the review core never knows whether it came from the working tree, a commit, or a branch.

**Tech Stack:** Python 3.11+ via `uv`, `python-toon==0.1.3` for AXI output, `urllib.request` for ollama (no new dependency), `subprocess` for git and tests. Model config from `~/.sherpa/gate.json`.

## Global Constraints

- Python 3.11+, `uv` for all execution. Never `pip install`, never `pipx`, never `pip install --user`.
- `tools/review.py` is a standalone PEP 723 script run as `uv run --script tools/review.py`. It reaches shared code via a two-line `sys.path` insert then `from sherpa.render import bin_line, emit, fail, parse_strict, truncate`. **Do not convert this into a package.**
- **AXI-conformant** per `docs/SHERPA_STANDARDS.md`: TOON on stdout via `emit()`, errors on stdout via `fail()`, exit codes `0` success / `1` uncooperative external world / `2` caller must change flags or environment, `axi: true` in the YAML docstring, `parse_strict` for flag rejection, `--json` on every subcommand, a no-args home view with `bin:` and `description:` lines.
- Tests run as `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/ -q`. Baseline on `feat/review` is whatever `master` had — confirm before starting and treat that as the regression bar.
- Test convention: load modules by path with `importlib.util.spec_from_file_location` (see `tests/test_render.py:14-18`).
- Commit messages: no "Generated with Claude Code", no "Co-Authored-By: Claude". Auto-committing is authorized. **Do not push without asking.**
- Comments only where naming and structure cannot carry the intent — this codebase's own rubric item 16 applies to this code. No narration comments.
- **Fail closed everywhere.** An empty, unparseable, or timed-out model response is a failure that stops the run. It is never a pass. A blank review is an empty finding list, indistinguishable from "no problems found".

### Verified facts — do not re-derive

- `sherpa/render.py` exports: `emit(payload: dict, *, as_json: bool = False)`, `fail(message: str, *, help: str | None = None, usage: bool = False) -> NoReturn` (exits 2 when `usage=True`, else 1), `truncate(text: str, limit: int = 1000) -> tuple[str, str | None]`, `bin_line(executable: str | Path) -> str`, `parse_strict(parser, subparsers: dict[str, ArgumentParser] | None, argv: list[str] | None) -> Namespace`.
- `~/.sherpa/gate.json` exists and currently reads `{"base_url": "http://10.0.6.46:11434/v1", "model": "gemma4:31b", "timeout_seconds": 120, "reasoning_effort": "none"}`.
- Live models on `10.0.6.46`: `gemma4:31b`, `qwen3.6:27b-q4_K_M`, `glm-4.7-flash:latest`. On `10.0.6.45`: `qwen3.6:35b-a3b-q8_0` plus smaller. The `qwen3.6-coder:*` and `qwen3.6:35b-a3b-q4_K_M` tags are **deleted** — do not reference them.
- Reasoning tokens count against `max_tokens`. With reasoning on, budget ≥16000; measured peak completion is 6872 tokens. `reasoning_effort: "none"` collapses completions to 23–39 tokens and skips thinking.
- Rubric items 1, 2, 4, 5, 6, 7, 8, 11, 14, 15 are greppable; 3, 9, 10, 12, 13, 16 are judgement; 17 is a fixed string match. Item 13 reports violations against clean code in 12 of 20 trials — expect it as noise, do not chase it.
- The old `tools/gate.py` implementation is on branch `feat/local-validation-gate` if its `probe` HTTP shape is worth cribbing. It is intentionally not on `feat/review`.

### File structure

| File | Responsibility |
| --- | --- |
| `scripts/gate_bench.py` | Existing eval harness. Task 0 extends it with judgement-shaped violations and voting. |
| `tools/review.py` | The tool. Pure functions above the `# --- I/O boundary ---` marker, I/O below it. |
| `tests/test_review.py` | Unit tests for every pure function, plus the git checkpoint/revert path against a scratch repo. |
| `.review.yaml` | Per-repo config: rubric path, test command, base branch. Committed. |
| `~/.sherpa/gate.json` | Model config. Already exists. Not in the repo. |

---

### Task 0: Extend the bench with judgement-shaped violations — THIS GATES THE BUILD

There is no evidence a local model can judge comment discipline. Item 3 (also judgement) is caught once in twenty trials. If item 16 recall resembles that, the tool is not worth building and this task is where we find out, for an hour rather than another two days.

**Files:**
- Modify: `scripts/gate_bench.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `scripts/results-judgement.json` and a go/no-go recorded in `docs/local-model-fleet.md`.

- [ ] **Step 1: Add the judgement diff and its expectations**

Add alongside the existing `DIFF` / `EXPECTED_ITEMS`. The planted violations are one narration comment (item 17), one comment restating a well-named symbol (item 16), one docstring repeating the signature (item 16), and one unreadable test name (item 16). The `parse_window` comment and the `MAX_RETRIES` comment are deliberately **clean** — item 16 explicitly permits a fact the language cannot express, and these measure false positives.

```python
JUDGEMENT_EXPECTED_ITEMS = {16, 17}

JUDGEMENT_DIFF = '''\
--- a/tools/widget.py
+++ b/tools/widget.py
@@ -20,6 +20,38 @@ from sherpa.render import bin_line, emit, fail, parse_strict, truncate

+# The upstream feed pads every window to a multiple of 300 seconds, so a
+# 290-second request silently returns 300 seconds of samples.
+WINDOW_QUANTUM_SECONDS = 300
+
+MAX_RETRIES = 3
+
+
+def parse_window(raw: str) -> int:
+    """Parse the window string and return the window as an integer.
+
+    Args:
+        raw: the window string
+
+    Returns:
+        the window as an integer
+    """
+    # convert to seconds
+    s = int(raw.rstrip("s"))
+    return s
+
+
+def test_widget_1(tmp_path):
+    # Arrange
+    config = tmp_path / "widget.json"
+    config.write_text('{"name": "left"}')
+
+    # Act
+    result = load_widget_config(config)
+
+    # Assert
+    assert result["name"] == "left"
'''
```

- [ ] **Step 2: Parameterise `run()` and the prompt over which diff is under review**

Replace the module-level `PROMPT` with a function, and thread the diff through `run()`. The `REPLACEMENT` line is what makes item 16 falsifiable — a finding that cannot name its replacement is not a finding, per the rubric.

```python
def build_prompt(diff: str) -> str:
    return f"""You are a code review gate. Check the diff below against the rubric.

{RUBRIC}

---

Diff under review:

```diff
{diff}
```

Report every rubric violation you find. For each one output:

VIOLATION item <N> <path>:<line> | <one short sentence>

For a violation of item 16, output an additional line immediately after it:

REPLACEMENT | <the rename, extraction, or restructuring that carries the intent>

A violation of item 16 without a REPLACEMENT line is not a violation — do not
report it. Then output a final line: DONE

Report only real violations that are visible in the diff. Do not report an item
if the diff does not violate it."""
```

Change `run(host, model, max_tokens, reasoning)` to `run(host, model, max_tokens, reasoning, diff, expected)` and use `build_prompt(diff)` and the passed `expected` in place of `PROMPT` and `EXPECTED_ITEMS`.

- [ ] **Step 3: Add the vote, and a `--diff` selector**

```python
def vote(trials: list[dict], threshold: float) -> dict:
    usable = [t for t in trials if "error" not in t and not t["empty"]]
    if not usable:
        return {"confident": [], "raised": [], "usable": 0}
    tally: dict[int, int] = {}
    for trial in usable:
        for item in set(trial["caught"]) | set(trial["false_positives"]):
            tally[item] = tally.get(item, 0) + 1
    needed = threshold * len(usable)
    return {
        "confident": sorted(i for i, n in tally.items() if n >= needed),
        "raised": sorted(i for i, n in tally.items() if n < needed),
        "usable": len(usable),
    }
```

Add `--diff {contract,judgement}` (default `contract`) to `main()`, selecting `(DIFF, EXPECTED_ITEMS)` or `(JUDGEMENT_DIFF, JUDGEMENT_EXPECTED_ITEMS)`, and default `--out` to `results-<diff>.json`. Print the vote at `threshold=0.6` under the ranking.

- [ ] **Step 4: Run it**

```bash
cd ~/src/scratch/jim_tools
uv run --script scripts/gate_bench.py --diff judgement --trials 5 --max-tokens 16000 \
  > /tmp/judgement-bench.log 2>&1
```

Expect roughly 35 minutes. Run it in the background.

- [ ] **Step 5: Record the verdict and decide**

Append a section to `docs/local-model-fleet.md` with the table and the go/no-go. Read the result against these three outcomes, which need different responses:

| Outcome | Meaning | Response |
| --- | --- | --- |
| Item 17 caught reliably, item 16 caught with sane REPLACEMENT lines | The premise holds | Proceed to Task 1 |
| Item 16 caught but replacements are garbage names | The model detects but cannot propose | Proceed, but `apply: deletions` stays the default and renames are report-only |
| Item 16 missed like item 3, or confident violations against the two clean comments | Wording still not checkable, or the model cannot do it | **Stop.** Iterate rubric item 16 and re-run this task before writing any tool code |

- [ ] **Step 6: Commit**

```bash
git add scripts/gate_bench.py scripts/results-judgement.json docs/local-model-fleet.md
git commit -m "bench: measure judgement-shaped rubric items with voting"
```

---

### Task 1: Config loading and diff resolution

**Files:**
- Create: `tools/review.py`
- Create: `tests/test_review.py`
- Create: `.review.yaml`

**Interfaces:**
- Consumes: `sherpa.render.fail`.
- Produces: `parse_config(text: str) -> dict` returning keys `rubric`, `test`, `base`, `apply`, `passes`, `threshold`; `diff_command(mode: str, ref: str | None, base: str) -> list[str]` returning an argv list.

- [ ] **Step 1: Write the failing tests**

`.review.yaml` is parsed with a five-line reader rather than a YAML dependency: the file is flat `key: value` and adding PyYAML to a stdlib-only tool is not worth it. Unknown keys are rejected so a typo is not silently ignored.

```python
def test_parse_config_fills_defaults():
    config = review.parse_config("rubric: docs/gate-rubric.md\n")
    assert config["rubric"] == "docs/gate-rubric.md"
    assert config["base"] == "master"
    assert config["apply"] == "deletions"
    assert config["passes"] == 5
    assert config["threshold"] == 0.6


def test_parse_config_rejects_unknown_key():
    with pytest.raises(ValueError, match="rubrik"):
        review.parse_config("rubrik: docs/gate-rubric.md\n")


def test_parse_config_ignores_comments_and_blanks():
    config = review.parse_config("# a comment\n\nrubric: r.md\ntest: pytest -q\n")
    assert config["test"] == "pytest -q"


def test_diff_command_defaults_to_uncommitted():
    assert review.diff_command("worktree", None, "master") == ["git", "diff", "HEAD"]


def test_diff_command_for_a_commit():
    assert review.diff_command("commit", "abc123", "master") == [
        "git", "diff", "abc123~1", "abc123",
    ]


def test_diff_command_for_a_branch_uses_merge_base():
    assert review.diff_command("branch", None, "RC") == [
        "git", "diff", "--merge-base", "RC", "HEAD",
    ]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: FAIL — `tools/review.py` does not exist.

- [ ] **Step 3: Create the tool skeleton and implement both functions**

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==0.1.3"]
# ///
"""
name: review
description: Review a diff against a rubric on a local model, auto-fix what survives a majority vote, and raise only what needs a human.
categories: [review, quality, git, local-model, write]
axi: true
usage: |
  run [--commit REF | --branch] [--passes N] [--apply deletions|all|none] [--fields ...] [--json]
  probe [--json]
  version [--json]
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate

GATE_CONFIG_PATH = Path.home() / ".sherpa" / "gate.json"
REVIEW_CONFIG_NAME = ".review.yaml"

CONFIG_DEFAULTS = {
    "rubric": "docs/gate-rubric.md",
    "test": "",
    "base": "master",
    "apply": "deletions",
    "passes": 5,
    "threshold": 0.6,
}
INTEGER_KEYS = {"passes"}
FLOAT_KEYS = {"threshold"}


def parse_config(text: str) -> dict:
    config = dict(CONFIG_DEFAULTS)
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition(":")
        if not separator:
            raise ValueError(f"line {number} is not 'key: value': {stripped}")
        key = key.strip()
        if key not in CONFIG_DEFAULTS:
            raise ValueError(f"unknown key {key!r}; valid keys are {sorted(CONFIG_DEFAULTS)}")
        value = value.strip()
        config[key] = int(value) if key in INTEGER_KEYS else float(value) if key in FLOAT_KEYS else value
    return config


def diff_command(mode: str, ref: str | None, base: str) -> list[str]:
    if mode == "commit":
        return ["git", "diff", f"{ref}~1", ref]
    if mode == "branch":
        return ["git", "diff", "--merge-base", base, "HEAD"]
    return ["git", "diff", "HEAD"]
```

Add the path-loading preamble to `tests/test_review.py`, mirroring `tests/test_render.py:14-18`:

```python
_SPEC = importlib.util.spec_from_file_location(
    "review", Path(__file__).resolve().parent.parent / "tools" / "review.py"
)
review = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(review)
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: 6 passed.

- [ ] **Step 5: Write `.review.yaml` for this repo**

```yaml
rubric: docs/gate-rubric.md
test: uv run --with pytest --with python-toon==0.1.3 pytest tests/ -q
base: master
```

- [ ] **Step 6: Commit**

```bash
git add tools/review.py tests/test_review.py .review.yaml
git commit -m "feat(review): config parsing and diff resolution"
```

---

### Task 2: Prompt construction and finding parsing

**Files:**
- Modify: `tools/review.py`
- Modify: `tests/test_review.py`

**Interfaces:**
- Consumes: nothing from Task 1 beyond the module.
- Produces: `@dataclass(frozen=True) Finding(item: int, path: str, line: int, description: str, replacement: str | None)`; `build_prompt(rubric: str, diff: str) -> str`; `parse_findings(text: str) -> list[Finding]`.

- [ ] **Step 1: Write the failing tests**

The two rejection tests are the heart of this task. An unnumbered finding is unparseable per the rubric's own contract, and an item-16 finding with no replacement is not a finding per item 16 itself.

```python
def test_parse_findings_reads_one_violation():
    findings = review.parse_findings(
        "VIOLATION item 17 tools/widget.py:42 | narration comment\nDONE\n"
    )
    assert findings == [
        review.Finding(item=17, path="tools/widget.py", line=42,
                       description="narration comment", replacement=None)
    ]


def test_parse_findings_attaches_a_replacement():
    findings = review.parse_findings(
        "VIOLATION item 16 tools/widget.py:9 | comment restates the name\n"
        "REPLACEMENT | rename s to seconds and delete the comment\n"
        "DONE\n"
    )
    assert findings[0].replacement == "rename s to seconds and delete the comment"


def test_parse_findings_drops_item_16_without_a_replacement():
    assert review.parse_findings(
        "VIOLATION item 16 tools/widget.py:9 | comment restates the name\nDONE\n"
    ) == []


def test_parse_findings_drops_an_unnumbered_finding():
    assert review.parse_findings("VIOLATION tools/widget.py:9 | something\nDONE\n") == []


def test_parse_findings_ignores_prose_around_the_findings():
    findings = review.parse_findings(
        "Here is my review.\n"
        "VIOLATION item 4 tools/widget.py:12 | bare print instead of fail()\n"
        "Hope that helps!\nDONE\n"
    )
    assert len(findings) == 1 and findings[0].item == 4


def test_build_prompt_embeds_rubric_and_diff():
    prompt = review.build_prompt("RUBRIC TEXT", "DIFF TEXT")
    assert "RUBRIC TEXT" in prompt and "DIFF TEXT" in prompt
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: FAIL with `AttributeError: module 'review' has no attribute 'Finding'`.

- [ ] **Step 3: Implement**

```python
VIOLATION_RE = re.compile(
    r"^VIOLATION\s+item\s+(\d+)\s+(\S+?):(\d+)\s*\|\s*(.+?)\s*$", re.I | re.M
)
REPLACEMENT_RE = re.compile(r"^REPLACEMENT\s*\|\s*(.+?)\s*$", re.I)
REPLACEMENT_REQUIRED_ITEMS = {16}


@dataclass(frozen=True)
class Finding:
    item: int
    path: str
    line: int
    description: str
    replacement: str | None


def parse_findings(text: str) -> list[Finding]:
    lines = text.splitlines()
    findings = []
    for index, line in enumerate(lines):
        match = VIOLATION_RE.match(line)
        if not match:
            continue
        replacement = None
        if index + 1 < len(lines):
            following = REPLACEMENT_RE.match(lines[index + 1])
            replacement = following.group(1) if following else None
        item = int(match.group(1))
        if item in REPLACEMENT_REQUIRED_ITEMS and not replacement:
            continue
        findings.append(
            Finding(item, match.group(2), int(match.group(3)), match.group(4), replacement)
        )
    return findings
```

`build_prompt` is the Task 0 Step 2 function with `rubric` as a parameter instead of a module global. Add `import re` to the imports.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/review.py tests/test_review.py
git commit -m "feat(review): prompt construction and finding parsing"
```

---

### Task 3: The vote

**Files:**
- Modify: `tools/review.py`
- Modify: `tests/test_review.py`

**Interfaces:**
- Consumes: `Finding` from Task 2.
- Produces: `finding_key(finding: Finding) -> tuple[int, str, int]`; `tally_findings(passes: list[list[Finding]], threshold: float) -> tuple[list[Finding], list[Finding]]` returning `(confident, raised)`.

- [ ] **Step 1: Write the failing tests**

The last two tests encode what the benchmark measured, and they are the reason the vote exists at all: it must erase an unstable finding and must **not** hide a systematic one.

```python
def _finding(item, line=1, path="a.py", replacement=None):
    return review.Finding(item, path, line, "d", replacement)


def test_vote_promotes_a_finding_in_every_pass():
    passes = [[_finding(17)], [_finding(17)], [_finding(17)]]
    confident, raised = review.tally_findings(passes, 0.6)
    assert [f.item for f in confident] == [17] and raised == []


def test_vote_raises_a_finding_below_the_threshold():
    passes = [[_finding(17)], [], []]
    confident, raised = review.tally_findings(passes, 0.6)
    assert confident == [] and [f.item for f in raised] == [17]


def test_vote_distinguishes_findings_by_line():
    passes = [[_finding(17, line=4)], [_finding(17, line=9)], [_finding(17, line=4)]]
    confident, raised = review.tally_findings(passes, 0.6)
    assert [f.line for f in confident] == [4] and [f.line for f in raised] == [9]


def test_vote_erases_an_unstable_finding_like_glm_dropping_item_4():
    passes = [[_finding(4)], [_finding(4)], [], [_finding(4)], [_finding(4)]]
    confident, _ = review.tally_findings(passes, 0.6)
    assert [f.item for f in confident] == [4]


def test_vote_cannot_hide_a_systematic_false_positive_like_item_13():
    passes = [[_finding(13)] for _ in range(5)]
    confident, raised = review.tally_findings(passes, 0.6)
    assert [f.item for f in confident] == [13] and raised == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: FAIL with `AttributeError: module 'review' has no attribute 'tally_findings'`.

- [ ] **Step 3: Implement**

A finding is the same finding across passes when it cites the same item at the same place; the model's wording will differ every time, so the description is deliberately not part of the key. The first-seen instance is kept so a real description and replacement survive.

```python
def finding_key(finding: Finding) -> tuple[int, str, int]:
    return (finding.item, finding.path, finding.line)


def tally_findings(
    passes: list[list[Finding]], threshold: float
) -> tuple[list[Finding], list[Finding]]:
    counts: dict[tuple[int, str, int], int] = {}
    first_seen: dict[tuple[int, str, int], Finding] = {}
    for findings in passes:
        for finding in {finding_key(f): f for f in findings}.values():
            key = finding_key(finding)
            counts[key] = counts.get(key, 0) + 1
            first_seen.setdefault(key, finding)
    needed = threshold * len(passes)
    confident = [first_seen[k] for k, n in counts.items() if n >= needed]
    raised = [first_seen[k] for k, n in counts.items() if n < needed]
    return sorted(confident, key=finding_key), sorted(raised, key=finding_key)
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/review.py tests/test_review.py
git commit -m "feat(review): majority vote over independent passes"
```

---

### Task 4: Fix planning

**Files:**
- Modify: `tools/review.py`
- Modify: `tests/test_review.py`

**Interfaces:**
- Consumes: `Finding` from Task 2.
- Produces: `plan_deletion(source: str, finding: Finding) -> str | None` returning the new file content, or `None` when the cited line is not a safe comment-only deletion.

- [ ] **Step 1: Write the failing tests**

Only whole-line comments are deletable. A trailing comment on a code line and a line inside a string literal both return `None` — the model's line numbers come from a diff and cannot be trusted to be exact, so this function verifies the target before touching it.

```python
def test_plan_deletion_removes_a_whole_line_comment():
    source = "x = 1\n# narration\ny = 2\n"
    assert review.plan_deletion(source, _finding(17, line=2)) == "x = 1\ny = 2\n"


def test_plan_deletion_preserves_indentation_context():
    source = "def f():\n    # narration\n    return 1\n"
    assert review.plan_deletion(source, _finding(17, line=2)) == "def f():\n    return 1\n"


def test_plan_deletion_refuses_a_trailing_comment():
    source = "x = 1  # not a whole-line comment\n"
    assert review.plan_deletion(source, _finding(17, line=1)) is None


def test_plan_deletion_refuses_a_code_line():
    source = "x = 1\ny = 2\n"
    assert review.plan_deletion(source, _finding(17, line=2)) is None


def test_plan_deletion_refuses_a_line_out_of_range():
    assert review.plan_deletion("x = 1\n", _finding(17, line=99)) is None


def test_plan_deletion_refuses_a_shebang():
    assert review.plan_deletion("#!/usr/bin/env python3\n", _finding(17, line=1)) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: FAIL with `AttributeError: module 'review' has no attribute 'plan_deletion'`.

- [ ] **Step 3: Implement**

```python
def plan_deletion(source: str, finding: Finding) -> str | None:
    lines = source.splitlines(keepends=True)
    if not 1 <= finding.line <= len(lines):
        return None
    target = lines[finding.line - 1]
    stripped = target.strip()
    if not stripped.startswith("#") or stripped.startswith("#!"):
        return None
    return "".join(lines[: finding.line - 1] + lines[finding.line :])
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: 23 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/review.py tests/test_review.py
git commit -m "feat(review): plan comment-line deletions defensively"
```

---

### Task 5: Model client and fail-closed probe

**Files:**
- Modify: `tools/review.py`
- Modify: `tests/test_review.py`

**Interfaces:**
- Consumes: `~/.sherpa/gate.json`.
- Produces: `load_gate_config() -> dict`; `chat_body(config: dict, prompt: str) -> dict`; `review_pass(config: dict, prompt: str) -> str` (I/O); `probe(config: dict) -> dict` with keys `ok`, `model`, `seconds`, `reason`.

- [ ] **Step 1: Write the failing tests**

Only the pure part is unit-tested; `review_pass` is exercised for real in Task 7. The budget assertion matters: with reasoning on, measured peak completion is 6872 tokens, and a budget below that silently truncates into an empty `content`, which reads as "no problems found".

```python
def test_chat_body_uses_a_budget_that_cannot_truncate():
    body = review.chat_body({"model": "m"}, "prompt")
    assert body["max_tokens"] >= 16000
    assert body["model"] == "m"
    assert body["messages"] == [{"role": "user", "content": "prompt"}]


def test_chat_body_omits_reasoning_effort_when_unset():
    assert "reasoning_effort" not in review.chat_body({"model": "m"}, "p")


def test_chat_body_passes_reasoning_effort_through():
    body = review.chat_body({"model": "m", "reasoning_effort": "none"}, "p")
    assert body["reasoning_effort"] == "none"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: FAIL with `AttributeError: module 'review' has no attribute 'chat_body'`.

- [ ] **Step 3: Implement**

```python
MAX_TOKENS = 16000
PROBE_QUESTION = "Reply with exactly the word: ready"


def load_gate_config() -> dict:
    if not GATE_CONFIG_PATH.exists():
        fail(
            f"no model config at {GATE_CONFIG_PATH}",
            help="sherpa review probe after creating it with base_url and model",
            usage=True,
        )
    return json.loads(GATE_CONFIG_PATH.read_text())


def chat_body(config: dict, prompt: str) -> dict:
    body = {
        "model": config["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
    }
    if config.get("reasoning_effort"):
        body["reasoning_effort"] = config["reasoning_effort"]
    return body


# --- I/O boundary ---


def review_pass(config: dict, prompt: str) -> str:
    request = urllib.request.Request(
        f"{config['base_url'].rstrip('/')}/chat/completions",
        json.dumps(chat_body(config, prompt)).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=config.get("timeout_seconds", 600)) as response:
        payload = json.load(response)
    return (payload["choices"][0]["message"].get("content") or "").strip()


def probe(config: dict) -> dict:
    started = time.monotonic()
    try:
        content = review_pass(config, PROBE_QUESTION)
    except Exception as error:  # noqa: BLE001 - a probe reports, it never raises
        return {"ok": False, "model": config["model"], "seconds": 0.0,
                "reason": f"{type(error).__name__}: {error}"}
    seconds = round(time.monotonic() - started, 1)
    if not content:
        return {"ok": False, "model": config["model"], "seconds": seconds,
                "reason": "empty response — an empty review reads as 'no problems found'"}
    return {"ok": True, "model": config["model"], "seconds": seconds, "reason": ""}
```

Add `import time` to the imports.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: 26 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/review.py tests/test_review.py
git commit -m "feat(review): model client and fail-closed probe"
```

---

### Task 6: Checkpoint, apply, test, revert

**Files:**
- Modify: `tools/review.py`
- Modify: `tests/test_review.py`

**Interfaces:**
- Consumes: `plan_deletion` from Task 4.
- Produces: `git(args: list[str], cwd: Path) -> str`; `checkpoint(cwd: Path) -> str | None` returning the created commit sha or `None` when the tree was already clean; `apply_deletions(findings: list[Finding], cwd: Path) -> list[Finding]` returning those actually applied; `run_tests(command: str, cwd: Path) -> tuple[bool, str]`; `revert_to(sha: str | None, cwd: Path) -> None`.

- [ ] **Step 1: Write the failing tests**

These run against a real scratch git repo, because the promise being tested — "a broken tree is never left behind" — is a property of git, not of a mock.

```python
@pytest.fixture
def scratch_repo(tmp_path):
    review.git(["init", "-q", "-b", "master"], tmp_path)
    review.git(["config", "user.email", "t@t"], tmp_path)
    review.git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "w.py").write_text("x = 1\n# narration\ny = 2\n")
    review.git(["add", "-A"], tmp_path)
    review.git(["commit", "-q", "-m", "base"], tmp_path)
    return tmp_path


def test_checkpoint_returns_none_for_a_clean_tree(scratch_repo):
    assert review.checkpoint(scratch_repo) is None


def test_checkpoint_commits_a_dirty_tree(scratch_repo):
    (scratch_repo / "w.py").write_text("x = 1\n# narration\ny = 3\n")
    sha = review.checkpoint(scratch_repo)
    assert sha and review.git(["status", "--porcelain"], scratch_repo) == ""


def test_apply_deletions_removes_the_comment(scratch_repo):
    applied = review.apply_deletions([_finding(17, line=2, path="w.py")], scratch_repo)
    assert len(applied) == 1
    assert (scratch_repo / "w.py").read_text() == "x = 1\ny = 2\n"


def test_apply_deletions_skips_a_finding_it_cannot_verify(scratch_repo):
    assert review.apply_deletions([_finding(17, line=1, path="w.py")], scratch_repo) == []
    assert (scratch_repo / "w.py").read_text() == "x = 1\n# narration\ny = 2\n"


def test_revert_restores_the_tree_byte_for_byte(scratch_repo):
    before = (scratch_repo / "w.py").read_text()
    sha = review.git(["rev-parse", "HEAD"], scratch_repo)
    review.apply_deletions([_finding(17, line=2, path="w.py")], scratch_repo)
    review.revert_to(sha, scratch_repo)
    assert (scratch_repo / "w.py").read_text() == before


def test_run_tests_reports_failure_with_output(scratch_repo):
    passed, output = review.run_tests("python3 -c 'import sys; sys.exit(1)'", scratch_repo)
    assert passed is False


def test_run_tests_reports_success(scratch_repo):
    passed, _ = review.run_tests("python3 -c 'pass'", scratch_repo)
    assert passed is True
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: FAIL with `AttributeError: module 'review' has no attribute 'git'`.

- [ ] **Step 3: Implement**

```python
CHECKPOINT_MESSAGE = "wip: sherpa review checkpoint"


def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def checkpoint(cwd: Path) -> str | None:
    if not git(["status", "--porcelain"], cwd):
        return None
    git(["add", "-A"], cwd)
    git(["commit", "-q", "-m", CHECKPOINT_MESSAGE], cwd)
    return git(["rev-parse", "HEAD"], cwd)


def apply_deletions(findings: list[Finding], cwd: Path) -> list[Finding]:
    applied = []
    for finding in sorted(findings, key=lambda f: (f.path, -f.line)):
        path = cwd / finding.path
        if not path.exists():
            continue
        planned = plan_deletion(path.read_text(), finding)
        if planned is None:
            continue
        path.write_text(planned)
        applied.append(finding)
    return applied


def run_tests(command: str, cwd: Path) -> tuple[bool, str]:
    result = subprocess.run(
        command, cwd=cwd, shell=True, capture_output=True, text=True, check=False
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def revert_to(sha: str | None, cwd: Path) -> None:
    git(["checkout", "--", "."], cwd)
    if sha:
        git(["reset", "--hard", "-q", sha], cwd)
```

Deletions are applied bottom-up within a file (`-f.line`) so that removing one line does not shift the line numbers of the findings below it.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: 33 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/review.py tests/test_review.py
git commit -m "feat(review): checkpoint, apply, test and revert"
```

---

### Task 7: Wire the tool together

**Files:**
- Modify: `tools/review.py`
- Modify: `tests/test_review.py`

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: `cmd_run`, `cmd_probe`, `cmd_version`, `cmd_home`, `main(argv)`. Payload keys: `fixed`, `raised`, `tests`, `passes`, `model`.

- [ ] **Step 1: Write the failing tests**

```python
def test_home_view_shows_bin_and_description(capsys):
    review.cmd_home()
    out = capsys.readouterr().out
    assert "bin:" in out and "description:" in out


def test_unknown_flag_exits_2(capsys):
    with pytest.raises(SystemExit) as exit_info:
        review.main(["run", "--nope"])
    assert exit_info.value.code == 2


def test_summarise_reports_a_clean_diff():
    payload = review.summarise([], [], None, "m", 5)
    assert payload["fixed"] == 0
    assert payload["raised"] == "none — nothing needs your attention"


def test_summarise_lists_raised_findings():
    payload = review.summarise([], [_finding(16, replacement="rename s")], None, "m", 5)
    assert payload["raised"][0]["item"] == 16
    assert payload["raised"][0]["replacement"] == "rename s"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: FAIL with `AttributeError: module 'review' has no attribute 'summarise'`.

- [ ] **Step 3: Implement the orchestration**

The order below is the spec's, and the baseline test run before any edit is what stops a pre-existing failure from being blamed on the tool's own changes.

```python
def summarise(applied, raised, tests, model, passes) -> dict:
    return {
        "model": model,
        "passes": passes,
        "fixed": len(applied),
        "fixes": [{"item": f.item, "at": f"{f.path}:{f.line}"} for f in applied]
                 or "none",
        "raised": [
            {"item": f.item, "at": f"{f.path}:{f.line}",
             "note": f.description, "replacement": f.replacement or ""}
            for f in raised
        ] or "none — nothing needs your attention",
        "tests": tests or "not run",
    }


def cmd_run(args: argparse.Namespace) -> None:
    root = Path(git(["rev-parse", "--show-toplevel"], Path.cwd()))
    config_path = root / REVIEW_CONFIG_NAME
    if not config_path.exists():
        fail(f"no {REVIEW_CONFIG_NAME} at {root}",
             help=f"create {config_path} with a rubric: and test: line", usage=True)
    config = parse_config(config_path.read_text())
    if args.passes:
        config["passes"] = args.passes
    if args.apply:
        config["apply"] = args.apply

    gate = load_gate_config()
    probed = probe(gate)
    if not probed["ok"]:
        emit(probed, as_json=args.json)
        fail(f"model probe failed: {probed['reason']}",
             help=f"check {GATE_CONFIG_PATH} then run sherpa review probe")

    mode = "commit" if args.commit else "branch" if args.branch else "worktree"
    diff = git(diff_command(mode, args.commit, config["base"])[1:], root)
    if not diff.strip():
        emit({"reviewed": "nothing", "note": f"no changes for mode {mode}"},
             as_json=args.json)
        return

    rubric = (root / config["rubric"]).read_text()
    prompt = build_prompt(rubric, diff)
    passes = [parse_findings(review_pass(gate, prompt)) for _ in range(config["passes"])]
    confident, raised = tally_findings(passes, config["threshold"])

    if config["apply"] == "none" or not confident:
        emit(summarise([], confident + raised, None, gate["model"], config["passes"]),
             as_json=args.json)
        return

    baseline_passed, _ = run_tests(config["test"], root) if config["test"] else (True, "")
    sha = checkpoint(root)
    applied = apply_deletions(confident, root)
    unapplied = [f for f in confident if f not in applied]

    tests = "not run"
    if config["test"] and applied:
        passed, output = run_tests(config["test"], root)
        if passed:
            tests = "pass"
        elif not baseline_passed:
            tests = "fail — already failing before this run"
        else:
            revert_to(sha, root)
            emit(summarise([], confident + raised, "fail — reverted", gate["model"],
                           config["passes"]), as_json=args.json)
            fail("tests failed after applying fixes; the tree was reverted",
                 help="sherpa review run --apply none to see the findings without fixing")

    emit(summarise(applied, raised + unapplied, tests, gate["model"], config["passes"]),
         as_json=args.json)
```

Then the remaining subcommands and the entry point:

```python
def cmd_probe(args: argparse.Namespace) -> None:
    payload = probe(load_gate_config())
    emit(payload, as_json=args.json)
    if not payload["ok"]:
        fail(f"probe failed: {payload['reason']}",
             help=f"edit {GATE_CONFIG_PATH} and re-run sherpa review probe")


def cmd_version(args: argparse.Namespace) -> None:
    emit({"tool": "review", "version": VERSION}, as_json=args.json)


def cmd_home() -> None:
    gate = json.loads(GATE_CONFIG_PATH.read_text()) if GATE_CONFIG_PATH.exists() else {}
    emit({
        "bin": bin_line(__file__),
        "description": "Review a diff against a rubric on a local model; fix what is certain, raise what is not.",
        "model": gate.get("model", "unconfigured"),
        "commands": ["run", "probe", "version"],
    })


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        cmd_home()
        return

    parser = argparse.ArgumentParser(prog="review", add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    registry = {}

    run_parser = subparsers.add_parser("run", add_help=False)
    run_parser.add_argument("--commit")
    run_parser.add_argument("--branch", action="store_true")
    run_parser.add_argument("--passes", type=int)
    run_parser.add_argument("--apply", choices=["deletions", "all", "none"])
    registry["run"] = run_parser

    for name in ("probe", "version"):
        registry[name] = subparsers.add_parser(name, add_help=False)

    for subparser in registry.values():
        subparser.add_argument("--json", action="store_true")

    args = parse_strict(parser, registry, argv)
    {"run": cmd_run, "probe": cmd_probe, "version": cmd_version}[args.command](args)


VERSION = "0.1.0"

if __name__ == "__main__":
    main()
```

`VERSION` belongs at the top of the file with the other constants; it is shown here so the task is self-contained.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_review.py -q`
Expected: 37 passed.

- [ ] **Step 5: Run the whole suite for regressions**

Run: `uv run --with pytest --with 'python-toon==0.1.3' pytest tests/ -q`
Expected: the Task 1 baseline plus 37, no failures.

- [ ] **Step 6: Commit**

```bash
git add tools/review.py tests/test_review.py
git commit -m "feat(review): wire the run end to end"
```

---

### Task 8: Use it on itself

The real test. `review.py` is a fresh diff written partly by an agent, which is exactly the population this tool exists to police.

**Files:**
- Modify: whatever the tool finds.

- [ ] **Step 1: Probe**

Run: `uv run --script tools/review.py probe`
Expected: `ok: true` with a latency. If it fails, stop — nothing downstream is trustworthy.

- [ ] **Step 2: Review the branch against master, without fixing**

Run: `uv run --script tools/review.py run --branch --apply none`
Expected: a TOON payload. Read the `raised` list.

- [ ] **Step 3: Judge the findings by hand, once**

Record in the commit message how many findings were real, how many were noise, and whether item 13 showed up as predicted. This is the only manual review in the plan and it is the calibration that tells you whether to trust the tool unattended.

- [ ] **Step 4: Let it fix**

Run: `uv run --script tools/review.py run --branch`
Expected: `tests: pass`, and `git diff HEAD~1` shows only comment deletions.

- [ ] **Step 5: Commit and report**

```bash
git add -A
git commit -m "chore(review): first self-review pass"
```

Report: findings real vs noise, whether the vote suppressed anything it should not have, and whether `apply: all` looks safe enough to become the default.

---

## Deferred — do not build these now

1. `--pr <url>` input via `gh pr diff`.
2. Posting findings as PR comments. Outward-facing; gated on Task 8's calibration.
3. Automatic triggering (loop or cron over changed branches).
4. Frontier escalation for the `raised` bucket.
5. Deterministic checks replacing the ten greppable rubric items.
6. Fixing rubric item 13, and splitting item 3 into mechanical assertions.
