#!/usr/bin/env python3
"""Benchmark candidate models on the REAL gate workload.

Prompt = the full 16-item gate rubric + a diff carrying three planted violations
and some deliberately clean code. Scores which rubric items each model cites,
so correctness is measured rather than latency alone.

Reasoning is ON by default and the token budget is set high enough that no
candidate can be truncated mid-answer. Reasoning tokens count against
max_tokens, so a small budget silently scores thinking models on a starved
response -- that is what made the first ranking provisional.

Run: uv run --script scripts/gate_bench.py
     uv run --script scripts/gate_bench.py --reasoning none   # old behaviour
"""

import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

RUBRIC = Path("/home/jvogel/src/scratch/jim_tools/docs/gate-rubric.md").read_text()

# Planted violations, and the rubric item each should be reported against.
#   item 4 -> error printed to stderr instead of routed through fail()
#   item 3 -> exit 1 where the contract requires 2 (caller must change something)
#   item 7 -> help emitted as a bare string instead of a list
EXPECTED_ITEMS = {3, 4, 7}

DIFF = '''\
--- a/tools/widget.py
+++ b/tools/widget.py
@@ -10,6 +10,24 @@ from sherpa.render import bin_line, emit, fail, parse_strict, truncate
 CACHE_DIR = Path.home() / ".sherpa" / "cache" / "widget"


+def load_widget_config(path: Path) -> dict:
+    if not path.exists():
+        print(f"error: no widget config at {path}", file=sys.stderr)
+        sys.exit(1)
+    return json.loads(path.read_text())
+
+
+def cmd_show(args: argparse.Namespace) -> None:
+    config = load_widget_config(WIDGET_CONFIG_PATH)
+    emit(
+        {
+            "widget": config.get("name"),
+            "status": config.get("status"),
+            "help": "sherpa widget list to see every widget",
+        },
+        as_json=args.json,
+    )
+
+
 def cmd_list(args: argparse.Namespace) -> None:
     widgets = discover_widgets()
     if not widgets:
'''

PROMPT = f"""You are a code review gate. Check the diff below against the rubric.

{RUBRIC}

---

Diff under review:

```diff
{DIFF}
```

Report every rubric violation you find. For each one output a single line in
exactly this format:

VIOLATION item <N>: <one short sentence>

Then output a final line: DONE

Report only real violations that are visible in the diff. Do not report an item
if the diff does not violate it."""

GATE_HOST = "http://10.0.6.46:11434"  # 4090, every model <= 19.9 GB
ESCALATION_HOST = "http://10.0.6.45:11434"  # Strix Halo, 125 GB unified

CANDIDATES = [
    (GATE_HOST, "gemma4:31b"),
    (GATE_HOST, "qwen3.6:27b-q4_K_M"),
    (GATE_HOST, "glm-4.7-flash:latest"),
    (ESCALATION_HOST, "qwen3.6:35b-a3b-q8_0"),
]

VIOLATION_RE = re.compile(r"VIOLATION\s+item\s+(\d+)", re.I)


def run(host: str, model: str, max_tokens: int, reasoning: str | None) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
    }
    if reasoning is not None:
        body["reasoning_effort"] = reasoning
    request = urllib.request.Request(
        f"{host}/v1/chat/completions",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            payload = json.load(response)
    except Exception as error:  # noqa: BLE001 - benchmark reports, never raises
        return {"model": model, "error": f"{type(error).__name__}: {error}"}
    elapsed = time.time() - started

    choice = payload["choices"][0]
    message = choice["message"]
    content = (message.get("content") or "").strip()
    reasoning_text = message.get("reasoning_content") or message.get("reasoning") or ""
    cited = {int(n) for n in VIOLATION_RE.findall(content)}
    usage = payload.get("usage") or {}
    return {
        "model": model,
        "seconds": round(elapsed, 1),
        "finish_reason": choice.get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_chars": len(reasoning_text),
        "caught": sorted(cited & EXPECTED_ITEMS),
        "missed": sorted(EXPECTED_ITEMS - cited),
        "false_positives": sorted(cited - EXPECTED_ITEMS),
        "truncated": choice.get("finish_reason") == "length",
        "empty": not content,
        "content": content,
    }


def summarise(model: str, trials: list[dict]) -> dict:
    usable = [t for t in trials if "error" not in t and not t["empty"]]
    return {
        "model": model,
        "trials": len(trials),
        "usable": len(usable),
        "mean_caught": (
            round(statistics.mean(len(t["caught"]) for t in usable), 2) if usable else 0.0
        ),
        "caught_per_trial": [len(t["caught"]) for t in usable],
        "always_caught": sorted(set.intersection(*(set(t["caught"]) for t in usable)))
        if usable
        else [],
        "ever_caught": sorted(set().union(*(set(t["caught"]) for t in usable)))
        if usable
        else [],
        "mean_false_positives": (
            round(statistics.mean(len(t["false_positives"]) for t in usable), 2)
            if usable
            else 0.0
        ),
        "median_seconds": (
            round(statistics.median(t["seconds"] for t in usable), 1) if usable else None
        ),
        "max_completion_tokens": max(
            (t["completion_tokens"] or 0 for t in usable), default=0
        ),
        "truncations": sum(1 for t in trials if t.get("truncated")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=16000)
    parser.add_argument(
        "--reasoning",
        default=None,
        help="reasoning_effort to send; omit the flag to leave it unset (reasoning on)",
    )
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).parent / "results-reasoning.json"
    )
    args = parser.parse_args()

    print(
        f"trials={args.trials} max_tokens={args.max_tokens} "
        f"reasoning_effort={args.reasoning or '<unset>'}",
        flush=True,
    )

    results = []
    for host, model in CANDIDATES:
        for trial in range(1, args.trials + 1):
            print(f"-- {model} trial {trial}/{args.trials} ...", flush=True)
            result = run(host, model, args.max_tokens, args.reasoning)
            result["host"] = host
            result["trial"] = trial
            results.append(result)
            args.out.write_text(json.dumps(results, indent=2) + "\n")
            if "error" in result:
                print(f"   ERROR {result['error']}", flush=True)
                continue
            print(
                f"   {result['seconds']:6.1f}s  prompt={result['prompt_tokens']} "
                f"out={result['completion_tokens']} think_chars={result['reasoning_chars']} "
                f"finish={result['finish_reason']}  caught={result['caught']} "
                f"missed={result['missed']}  false_pos={result['false_positives']}"
                f"{'  EMPTY' if result['empty'] else ''}",
                flush=True,
            )

    print(f"\nwrote {args.out}", flush=True)

    summaries = [
        summarise(model, [r for r in results if r["model"] == model])
        for _, model in CANDIDATES
    ]
    print("\n=== ranking (mean caught desc, then median seconds asc) ===", flush=True)
    for s in sorted(
        summaries, key=lambda s: (-s["mean_caught"], s["median_seconds"] or 1e9)
    ):
        seconds = "n/a" if s["median_seconds"] is None else f"{s['median_seconds']:6.1f}s"
        truncated = f"  truncated={s['truncations']}" if s["truncations"] else ""
        print(
            f"  {s['model']:24} {s['mean_caught']:.2f}/3 mean caught {s['caught_per_trial']}"
            f"  always={s['always_caught']}  {s['mean_false_positives']:.2f} false-pos"
            f"  {seconds}  peak_out={s['max_completion_tokens']}"
            f"  usable={s['usable']}/{s['trials']}{truncated}",
            flush=True,
        )

    (args.out.parent / (args.out.stem + "-summary.json")).write_text(
        json.dumps(summaries, indent=2) + "\n"
    )


if __name__ == "__main__":
    sys.exit(main())
