# AXI/TOON token savings — measured on real sherpa output

AXI's published headline is ~40% token savings over JSON, measured by the
author on his own tools. This measures the same thing on sherpa's two
converted tools (`youtube info`, `jira_issues search`), driving the real
CLI and capturing real stdout for both output modes — not a re-encode of
an in-memory payload.

**Update after review**: the first pass of this measurement compared
`jira_issues search`'s TOON and `--json` stdout directly and attributed
the whole delta to TOON's encoding. That comparison was not
schema-matched — see "The jira confound" below — so it measured the
*total AXI-conversion effect* (encoding + schema minimization +
pretty-vs-compact JSON), not TOON's isolated contribution. Both figures
are now reported, decomposed. The youtube comparison was already
apples-to-apples (see below) and is unaffected.

## Method

- Captured real stdout: `sherpa <tool> ... > file.json` and
  `sherpa <tool> ...` (TOON, the default), driving the real CLI rather
  than re-encoding an in-memory payload.
- Counted tokens via the LiteLLM proxy's `POST /utils/token_counter`
  (`~/.sherpa/vault.json`: `LITELLM_API_URL` / `LITELLM_API_KEY`), not the
  Anthropic SDK — there is no `ANTHROPIC_API_KEY` available in this
  environment. Two different tokenizer engines were used so a single
  engine's quirks can't be mistaken for a real effect:
  - `ollama/glm-4.7-flash` → `tokenizer_type: openai_tokenizer`
  - `claude-sonnet-4-5` → `tokenizer_type: huggingface_tokenizer`
- **Neither is Anthropic's actual billing tokenizer.** Treat all counts
  below as approximate, structurally-representative numbers, not
  Anthropic-exact token counts. See "Reproducing with a real key" below.
- Script: `scripts/measure_toon.py`, run as (see "The jira confound" for
  the additional pairs needed to decompose the jira effect):

  ```
  uv run --script scripts/measure_toon.py \
    --pair youtube-detail "1 object, 18 chapter rows" /tmp/axi-payloads/youtube-detail.json /tmp/axi-payloads/youtube-detail.toon \
    --pair jira-typical 20 /tmp/axi-payloads/jira-typical.json /tmp/axi-payloads/jira-typical.toon \
    --pair jira-large 100 /tmp/axi-payloads/jira-large.json /tmp/axi-payloads/jira-large.toon
  ```

## Payloads captured

| Payload | Command | Rows |
|---|---|---|
| youtube-detail | `sherpa youtube info iQyg-KypKAA [--json]` | 1 object, nested 18-row chapters array |
| jira-typical | `sherpa jira_issues search --mine [--json]` | 20 issues |
| jira-large | `sherpa jira_issues search --jql 'project = KB ORDER BY created DESC' --max-results 100 [--json]` | 100 issues |

Note: the brief's suggested `--limit 100` flag does not exist on
`jira_issues search`; the correct flag is `--max-results 100`, used above.

### The jira confound

`sherpa jira_issues search`'s TOON and `--json` paths do not carry the
same fields, so a direct comparison does not isolate TOON's encoding:

- TOON with no `--fields` passed emits **3 fields/row**:
  `key, summary, status` (`search_payload` → `parse_fields(args.fields)`,
  `tools/jira_issues.py`).
- `--json` unconditionally forces **all 7 fields**:
  `key, summary, status, type, assignee, priority, updated`
  (`search_rows(issues, set(EXTRA_SEARCH_FIELDS))`, ignoring
  `--fields`, `tools/jira_issues.py:893-894`).
- `--json` is also pretty-printed at `indent=2`
  (`sherpa/render.py:41-42`) while TOON output is compact.

So the direct TOON-vs-`--json` comparison bundles three effects: TOON's
key-folding, dropping 4 of 7 fields, and pretty-vs-compact JSON. To
isolate TOON's schema-matched effect, the TOON side was re-captured with
`--fields type,assignee,priority,updated` so both sides carry all 7
fields, and a compact (non-indented) re-serialization of the same
`--json` output was generated to isolate the pretty-print component:

```
sherpa jira_issues search --mine --fields type,assignee,priority,updated \
                                            > /tmp/axi-payloads/jira-typical-matched.toon
sherpa jira_issues search --jql 'project = KB ORDER BY created DESC' --max-results 100 \
       --fields type,assignee,priority,updated > /tmp/axi-payloads/jira-large-matched.toon
python3 -c "import json; d=json.load(open('jira-typical.json')); open('jira-typical-compact.json','w').write(json.dumps(d))"
python3 -c "import json; d=json.load(open('jira-large.json')); open('jira-large-compact.json','w').write(json.dumps(d))"
```

`youtube info` does not have this problem: both its TOON and `--json`
paths route through the same `build_info_payload(meta, parse_fields(args.fields), args.full)`
(`tools/youtube.py:347-351`), so the youtube-detail comparison is already
apples-to-apples and needed no correction.

## Results

### openai_tokenizer (`ollama/glm-4.7-flash`)

| Payload | Rows | JSON tokens | TOON tokens | Saved |
|---|---|---|---|---|
| youtube-detail | 1 obj / 18 rows | 834 | 601 | 27.9% |
| jira-typical — total conversion effect (3-field TOON vs 7-field pretty JSON) | 20 | 1795 | 516 | 71.3% |
| jira-typical — schema-matched (7-field TOON vs 7-field pretty JSON) | 20 | 1795 | 1054 | 41.3% |
| jira-typical — schema+format-matched (7-field TOON vs 7-field compact JSON) | 20 | 1568 | 1054 | 32.8% |
| jira-large — total conversion effect (3-field TOON vs 7-field pretty JSON) | 100 | 8198 | 1757 | 78.6% |
| jira-large — schema-matched (7-field TOON vs 7-field pretty JSON) | 100 | 8198 | 4304 | 47.5% |
| jira-large — schema+format-matched (7-field TOON vs 7-field compact JSON) | 100 | 7091 | 4304 | 39.3% |

### huggingface_tokenizer (`claude-sonnet-4-5`)

| Payload | Rows | JSON tokens | TOON tokens | Saved |
|---|---|---|---|---|
| youtube-detail | 1 obj / 18 rows | 897 | 649 | 27.6% |
| jira-typical — total conversion effect | 20 | 1791 | 513 | 71.4% |
| jira-typical — schema-matched | 20 | 1791 | 1059 | 40.9% |
| jira-typical — schema+format-matched | 20 | 1562 | 1059 | 32.2% |
| jira-large — total conversion effect | 100 | 8184 | 1706 | 79.2% |
| jira-large — schema-matched | 100 | 8184 | 4370 | 46.6% |
| jira-large — schema+format-matched | 100 | 7075 | 4370 | 38.2% |

The two tokenizers agree within ~1 point on every row of every table
above. That's strong evidence each effect is structural (not tokenizer
noise) — including the decomposition itself.

## Interpretation

- **youtube-detail (single object)**: ~28% saved, below the 40% headline,
  exactly as expected — a 1-row detail view has almost no repeated keys
  for TOON to fold away. The 28% here comes mostly from the nested
  18-row chapters array, not the top-level object. This is not TOON
  underperforming; it's the wrong shape to benefit from TOON.
- **jira "total conversion effect" (71–79%)** is what a user actually
  sees today comparing sherpa's TOON output to its `--json` output — but
  it is **not** TOON's isolated contribution. It bundles three things:
  TOON's key-folding, `--json` forcing 4 extra fields the TOON path
  doesn't emit by default, and `--json`'s pretty-printing.
- **jira "schema-matched" (41–48%: same 7 fields both sides, TOON
  compact vs JSON pretty)** is the more honest like-for-like number for
  the comparison sherpa users actually face today, since sherpa's
  pre-AXI JSON contract was always pretty-printed. This lands close to
  the 40% AXI headline, not far above it, and it grows modestly with row
  count (41%→47-48% from 20 to 100 rows) rather than dramatically.
- **jira "schema+format-matched" (32–39%: same 7 fields, TOON compact vs
  JSON also compact)** isolates TOON's table format alone, with the
  pretty-print advantage removed from both sides. This is the figure
  that predicts what TOON's encoding contributes on its own, independent
  of any field-count decision a tool's `--json` path happens to make, or
  of whether that tool's JSON was ever pretty-printed to begin with. It's
  below the 40% headline and roughly comparable to the youtube-detail
  figure once schema effects are stripped out.

The takeaway: **most of the eye-catching 71–79% "total conversion
effect" in the first pass of this measurement came from
`jira_issues search --json` emitting more fields than its own TOON
default, and from pretty-printing — not from TOON's encoding.** TOON's
own contribution, isolated, is real but more modest: **41–48%
(schema-matched, against sherpa's actual pretty-printed JSON baseline)**,
or **32–39% (schema+format-matched, the conservative floor with no
pretty-print advantage to strip)**. Both sit in the same neighborhood as
AXI's published 40% figure rather than dramatically above it. The
schema-minimization gain is real too, but it belongs to the tool's own
field-selection design (TOON's default omitting
`type`/`assignee`/`priority`/`updated` unless asked for), not to the
TOON format.

## Recommendation

**Proceed with converting the remaining ~18 tools, but calibrate
expectations to the schema-matched figure (~41–48%), not the unadjusted
total-conversion figure (71–79%).** Schema-matched is the right headline
here because it compares TOON against sherpa's actual JSON baseline —
pretty-printed, which is what the pre-AXI contract always emitted — so
it's the savings a tool genuinely realizes on conversion, not a
best-case or worst-case bound. (The conservative floor, if a tool's JSON
was already compact, is schema+format-matched: ~32–39%.) These savings
are real, tokenizer-independent, still meaningful, and scale (modestly)
with row count:

- **High priority**: tools whose primary output is a list/table
  (search, list, query-style commands) — these are the shape that
  benefits from TOON's key-folding at all, with the schema-matched
  effect measured here at roughly 41–48% depending on row count.
- **Lower priority / optional**: tools whose primary output is a single
  object or a short scalar/summary — expect savings well under 40% (the
  youtube-detail case here landed at ~28%, and a payload with no nested
  array at all would land near 0%). Converting these is still cheap and
  harmless (TOON degrades gracefully on scalars) but shouldn't be the
  reason to justify the fan-out.
- **Separately worth doing regardless of TOON**: if any of the remaining
  tools' `--json` paths force more fields than a sensible default (as
  `jira_issues search --json` does), that's an independent field-scoping
  fix worth making on its own merits — it's real token savings, but it's
  a schema decision, not a TOON encoding win, and should not be counted
  toward "TOON saved X%" claims.

What would change this recommendation: if a converted tool's realistic
default field set is already lean (3–4 fields, no `--json`-only
expansion), expect its schema-matched savings to sit closer to the
youtube-detail ~28% figure than to the 41–48% jira figures — the field
count matters as much as the row count. Task 7's catalog triage should
classify each remaining tool's (a) typical output shape (list vs.
detail) and (b) whether `--json` already matches TOON's default field
set, before committing to convert all of them.

## Reproducing with a real Anthropic key

`scripts/measure_toon.py` calls the LiteLLM proxy's `/utils/token_counter`
because no `ANTHROPIC_API_KEY` was available here. To get Anthropic's
actual billing counts: swap `count_tokens()`'s httpx call for
`anthropic.Anthropic().messages.count_tokens(model=..., messages=...).input_tokens`,
add `anthropic` to the script's `dependencies`, and remove the
`load_vault`/`require_secrets` plumbing (an Anthropic key has no vault
lookup to perform — `anthropic.Anthropic()` reads `ANTHROPIC_API_KEY`
from the environment directly). The `--pair NAME ROWS JSON_FILE
TOON_FILE` capture format and the report table stay the same.
