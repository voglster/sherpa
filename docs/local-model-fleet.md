# Local model fleet

Which model runs where, and why. Measured on 2026-07-26 against the real gate
workload rather than published benchmarks, because published rankings measure
agentic coding on other people's hardware and this fleet's constraint is memory.

Re-run the measurement with `uv run --script scripts/gate_bench.py` whenever the
models change. Its prompt is the full 16-item `docs/gate-rubric.md` plus a diff
carrying three planted violations, so it scores correctness, not just latency.

## The hardware

| Host | Address | Compute | Memory | Role |
| --- | --- | --- | --- | --- |
| `jv-desktop2` | `10.0.6.46` | RTX 4090 | 24 GB VRAM | fast, capacity-limited — interactive gate checks |
| `jv-desktop` | `10.0.6.45` / localhost | Ryzen AI MAX+ 395, Radeon 8060S | 125 GB unified (~59 GB free) | capacity-rich, bandwidth-limited — long context |
| `llmbox` | — | Ryzen 9 3950X, RTX 3080 | 10 GB VRAM (~7 GB free) | reserved for voice transcription |
| `hd` (docker02) | — | Xeon W-11855M, RTX A5000 Laptop | 16 GB VRAM (~11 GB free) | container services; too small for this class of model |

`llmbox` and `hd` cannot hold a 17 GB+ model alongside their existing work.
Neither needs upgrading for this purpose — the two desktops already cover it.

## Measured: the gate review workload

Three trials per model on the 4090, unloading between models so no run inherits
another's memory pressure. "Caught" is out of three planted violations (rubric
items 3, 4 and 7).

These are the **reasoning-off** numbers, kept because they are what the model
removals were decided on. For the reasoning-enabled re-measurement that supersedes
their latencies and confirms their ranking, see "RESOLVED" below.

| Model | Size | Caught | False positives | Median | Verdict |
| --- | --- | --- | --- | --- | --- |
| `gemma4:31b` | 19.9 GB | 2, 2, 2 | 1, 0, 0 | 2.3 s | **chosen** — the only consistent one |
| `qwen3.6:27b-q4_K_M` | 17 GB | 3, 2, 1 | 0, 1, 1 | 5.4 s | higher ceiling, erratic |
| `glm-4.7-flash` | 19.0 GB | 1, 0, 0 | 2, 1, 6 | 1.5 s | rejected — collapses on repeats |
| `qwen3.6-coder:latest` | 23.9 GB | 2 (single) | 4 | 23.0 s | rejected — slow, noisy, oversized |
| `qwen3.6:35b-a3b-q4_K_M` | 23.9 GB | 2 (single) | 8 | 27.6 s | rejected — worst precision, wedges the card |

`gemma4:31b` wins on **predictability**, which matters more than peak for a gate:
you can learn what it does and does not catch. `qwen3.6:27b-q4_K_M` was the only
model ever to catch all three, but ranged from 1 to 3 across identical prompts,
so its verdicts cannot be trusted individually.

Single samples are misleading here. On one run `glm-4.7-flash` looked like the
best option — 2 of 3 caught in 8.2 s. Across three runs it caught 1, 0, 0 and
produced up to 6 false positives. Anything claiming a ranking from one sample,
including an earlier draft of this file, was measuring noise.

## Assignments

**4090 (`10.0.6.46`) — `gemma4:31b`, 19.9 GB.** The gate reviewer. Short prompts
(a diff plus the rubric), so it wants speed and consistency. 19.9 GB leaves
roughly 4 GB for KV cache on a 24 GB card.

**Strix Halo (`10.0.6.45`) — `qwen3.6:35b-a3b-q8_0`, 38.7 GB.** The escalation tier
and the home for anything long-context. Measured on the same workload, three
trials:

| Model | Size | Caught | False positives | Median |
| --- | --- | --- | --- | --- |
| `qwen3.6:35b-a3b-q8_0` | 38.7 GB | 3, 1, 2 | 2, 3, 3 | 5.0 s |
| `qwen3.6-coder-256k` | 23.9 GB | 2, 2, 1 | 2, 6, 5 | 16.7 s |

A 38.7 GB model at q8 answering in 5 s median on a bandwidth-limited box is the
MoE pairing paying off: only ~3 B parameters are active per token, so the weight
reads this box is slow at stay small, while its 125 GB holds the model plus a
large KV cache without spilling. The same architecture is a poor fit for the 4090,
where total size rather than active size decides whether it fits.

It has a higher ceiling than `gemma4:31b` — it caught all three planted violations
once — but 2 to 3 false positives per run against gemma's 0 to 1. Better as the
escalation tier than as the default gate.

**`llmbox`** — leave to voice transcription. **`hd`** — leave to containers.

## Hazard: do not exceed ~20 GB on the 4090

`qwen3.6:35b-a3b-q4_K_M` is listed as 23.9 GB but goes resident at **28.2 GB**
once context is allocated. On a 24 GB card it does not merely run slowly — it
wedges the GPU, and every subsequent model load fails with HTTP 500 until it is
unloaded:

```sh
curl -X POST http://10.0.6.46:11434/api/generate \
  -d '{"model":"<wedged-model>","keep_alive":0}'
```

This bit during benchmarking and cost a full set of trials. Keep the 4090 at or
below about 20 GB, and send anything larger to the Strix Halo.

## RESOLVED: re-measured with reasoning enabled

Everything above was measured with `reasoning_effort: "none"` at a fixed
`max_tokens=900`, so every model was scored with **reasoning disabled**, two were
truncated mid-list, and `gemma4:31b` was the only candidate the budget never
constrained — so its win might have reflected terseness rather than review quality.

Re-measured 2026-07-26, five trials per model, reasoning **on** (`reasoning_effort`
unset) at `max_tokens=16000`. All 20 trials finished with `finish_reason: "stop"`;
peak completion was 6872 tokens, so nothing was truncated and no response was empty.

| Model | Mean caught | Per trial | Always caught | False pos | Median | Peak out |
| --- | --- | --- | --- | --- | --- | --- |
| `qwen3.6:27b-q4_K_M` | 2.20/3 | 2, 2, **3**, 2, 2 | 4, 7 | 0.40 | 202.1 s | 5032 |
| `gemma4:31b` | 2.00/3 | 2, 2, 2, 2, 2 | 4, 7 | 1.00 | 90.6 s | 2163 |
| `qwen3.6:35b-a3b-q8_0` | 2.00/3 | 2, 2, 2, 2, 2 | 4, 7 | 1.00 | 111.0 s | 4643 |
| `glm-4.7-flash` | 1.80/3 | 2, 2, **1**, 2, 2 | 7 | 0.20 | 40.6 s | 6872 |

Raw trials in `scripts/results-reasoning.json`. Two of the five original candidates
(`qwen3.6-coder:latest`, `qwen3.6:35b-a3b-q4_K_M`) no longer exist, so the candidate
list is now the three live 4090 models plus the Strix Halo escalation model.

**The concern was legitimate and the conclusion survives it.** Recall is unchanged
from the reasoning-off run: every model still catches items 4 and 7 on every trial.
With the budget removed, `gemma4:31b` matches the two far larger and slower models
exactly while still spending less than half their tokens — so its terseness was not
buying it a false win.

`gemma4:31b` stays the gate model. Its 2.00 against `qwen3.6:27b-q4_K_M`'s 2.20 rests
entirely on one item-3 hit in five trials, while it runs 2.2x faster and returned
byte-identical findings on all five trials. For a check that runs on every push, that
determinism beats a coin flip on an item neither model reliably catches.

### Reasoning on costs ~16x latency and buys nothing here

`gemma4:31b` answers in ~5.6 s with reasoning off and a 90.6 s median with it on,
and reports the same two items either way. The hardcoded `reasoning_effort: "none"`
in `sherpa gate probe` is therefore now evidence-backed rather than merely
convenient — **for mechanical rubric items**. This is one diff with three planted
violations; do not generalise it to judgement-shaped review.

### Item 3 is unreachable, not budget-starved

Caught **once in twenty trials** with reasoning enabled and no budget pressure. The
earlier 8-of-9 miss rate was not an artifact of disabled reasoning. `qwen3.6:27b-q4_K_M`
is the only model that has ever caught it, which makes it the natural escalation
reviewer if item 3 is routed there instead of being split into mechanical assertions.

### Item 13 is a rubric defect, not a model defect

Flagged as violated in 12 of 20 trials across three of four models, and 5/5
deterministically by both `gemma4:31b` and `qwen3.6:35b-a3b-q8_0`. The diff under
review does not violate it. Item 13 was flagged as subjective when the rubric was
written and is now measured: its wording reads as violated by clean code. Fix the
rubric before reading anything into these false-positive counts.

## Thinking models need an adequate token budget

**An earlier version of this file misdiagnosed this** as an undocumented Ollama bug
that emptied `content`, citing ollama/ollama#14820. That was wrong.

Reasoning tokens count against `max_tokens`. Run out mid-thought and you get a
truncated response with empty `content` and `finish_reason: "length"`. Content
appears as soon as the reason is `stop`. Measured floors: `gemma4:31b` ~300 tokens,
`qwen3.6:27b-q4_K_M` ~3000 (4047 characters of reasoning).

`reasoning_effort: "none"` stops the empty responses only by skipping the thinking —
completions collapse to 23–39 tokens. It is a way to ask for a non-reasoning answer,
not a fix for truncation, and it should not be a global default.

`sherpa gate probe` still sends `reasoning_effort: "none"`, which is why it works at
a small budget. That is now a measured choice rather than an unexamined one — see
"Reasoning on costs ~16x latency" above. `scripts/gate_bench.py` no longer sends it:
reasoning is on by default there, and `--reasoning none` reproduces the old run.

## Disabling thinking through Pi, when you want that

Ollama's `/v1/chat/completions` returns **empty `content`** for hybrid-thinking
models unless thinking is switched off — the reasoning text goes to a separate
field and `content` is left blank (ollama/ollama#14820, undocumented).

- Direct HTTP: send `reasoning_effort: "none"`. `"low"` still thinks;
  `reasoning: false` is rejected; `think: false` works only on the native
  `/api/generate`, not on `/v1`.
- Through Pi: `pi --thinking off`, or a `:<thinking>` suffix on `--model`.

Verified working end to end, including tool calls:

```sh
pi --provider gate-local --model gemma4:31b --thinking off --no-session \
   -p "read the first line of docs/gate-rubric.md and reply with just that line"
```

An empty review is an empty finding list, which reads as "no problems found" —
so this is a correctness issue, not a performance one. `sherpa gate probe` exists
to catch it, and `sherpa gate push` refuses to push when the probe fails.

## Finding: rubric item 3 is not mechanically checkable

Every model caught items 4 (error routed to stderr instead of `fail()`) and 7
(`help` emitted as a bare string) — both pattern matches. Item 3 (exit codes) was
missed in 8 of 9 trials, because it requires inferring that "config file missing"
means "the caller must change their environment", therefore exit 2.

That is judgement, not matching, and it is the class of thing this design assumed
small models would be weak at. Options, none yet taken:

- Split item 3 into concrete assertions (`sys.exit(1)` for a missing file or
  absent config is wrong; that case is exit 2).
- Accept that item 3 needs the escalation path and mark it as such in the rubric.

Items 13 and 16 were flagged as similarly subjective when the rubric was written
and have not been measured yet.

## Cleanup — done 2026-07-26

Roughly 120 GB reclaimed. Removed:

- **`10.0.6.46`**: `qwen3.6-coder:latest` (23.9 GB, 23 s and 4 false positives) and
  `qwen3.6:35b-a3b-q4_K_M` (23.9 GB, 8 false positives, and the model that wedges
  this card by going resident at 28.2 GB).
- **`10.0.6.45`**: `qwen3.6-coder-128k:latest`, `qwen3.6-coder-256k:latest`, and
  `qwen3.6:35b-a3b-q4_K_M`.

The custom `-256k` build was removed once `/api/show` confirmed
`qwen3.6:35b-a3b-q8_0` reports the **same 262144 native context** and the same
`qwen35moe` family — so it offered no capability the q8 lacks, while measuring
slower (16.7 s vs 5.0 s median) and noisier. `qwen3.6-coder:*` is not an official
Ollama repository, so those were custom local repackagings of unknown quantisation.

Resulting state, verified after removal — gate probe green at 5.58 s and Pi
tool-calling intact:

| Host | Models | Total | Largest |
| --- | --- | --- | --- |
| `10.0.6.46` | 5 | 62.7 GB | `gemma4:31b` 19.9 GB |
| `10.0.6.45` | 7 | 74.2 GB | `qwen3.6:35b-a3b-q8_0` 38.7 GB |

**Every model on the 4090 is now at or below 19.9 GB**, so the wedging hazard above
is structurally prevented rather than merely documented. Keep it that way: send
anything larger to the Strix Halo.

`glm-4.7-flash` was kept on the 4090 despite being rejected as a gate reviewer — it
is in the LiteLLM catalog and may serve other purposes. It should not be selected
as the gate model.
