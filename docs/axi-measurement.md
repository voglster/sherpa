# AXI/TOON token savings — measured on real sherpa output

AXI's published headline is ~40% token savings over JSON, measured by the
author on his own tools. This measures the same thing on sherpa's two
converted tools (`youtube info`, `jira_issues search`), driving the real
CLI and capturing real stdout for both output modes — not a re-encode of
an in-memory payload.

## Method

- Captured real stdout: `sherpa <tool> ... > file.json` and
  `sherpa <tool> ...` (TOON, the default) for the same underlying data.
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
- Script: `scripts/measure_toon.py`, run as:

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

## Results

### openai_tokenizer (`ollama/glm-4.7-flash`)

| Payload | Rows | JSON tokens | TOON tokens | Saved |
|---|---|---|---|---|
| youtube-detail | 1 obj / 18 rows | 834 | 601 | 27.9% |
| jira-typical | 20 | 1795 | 516 | 71.3% |
| jira-large | 100 | 8198 | 1757 | 78.6% |

### huggingface_tokenizer (`claude-sonnet-4-5`)

| Payload | Rows | JSON tokens | TOON tokens | Saved |
|---|---|---|---|---|
| youtube-detail | 1 obj / 18 rows | 897 | 649 | 27.6% |
| jira-typical | 20 | 1791 | 513 | 71.4% |
| jira-large | 100 | 8184 | 1706 | 79.2% |

The two tokenizers agree within ~1 point on every payload. That's strong
evidence the saving is structural (TOON not repeating field names per
row) rather than an artifact of either tokenizer's encoding.

## Interpretation

- **youtube-detail (single object)**: ~28% saved, below the 40% headline,
  exactly as expected — a 1-row detail view has almost no repeated keys
  for TOON to fold away. The 28% here comes mostly from the nested
  18-row chapters array, not the top-level object. This is not TOON
  underperforming; it's the wrong shape to benefit from TOON.
- **jira-typical (20-row list)**: ~71% saved, well above the 40% headline.
- **jira-large (100-row list)**: ~79% saved, the strongest result, and it
  grows with row count as expected (more rows = more repeated keys
  folded away per row).

List-shaped payloads beat AXI's own 40% figure by a wide margin on real
sherpa data; only the single-object detail view falls short, and it falls
short for the structural reason the brief predicted, not because the
number is soft.

## Recommendation

**Proceed with converting the remaining ~18 tools, prioritized by output
shape.** The savings are real, tokenizer-independent, and scale with row
count:

- **High priority**: tools whose primary output is a list/table
  (search, list, query-style commands) — these are the ones already
  showing 70–80% savings here and stand to gain the most.
- **Lower priority / optional**: tools whose primary output is a single
  object or a short scalar/summary — expect savings well under 40% (the
  youtube-detail case here landed at ~28%, and a payload with no nested
  array at all would land near 0%). Converting these is still cheap and
  harmless (TOON degrades gracefully on scalars) but shouldn't be the
  reason to justify the fan-out.

What would change this recommendation: if most of the remaining ~18
tools are single-object/detail-style (like `youtube info` rather than
`jira_issues search`), the aggregate benefit across the catalog would be
far more modest than these numbers suggest, and a case-by-case call would
be better than a blanket conversion. Task 7's catalog triage should
classify each remaining tool's typical output shape (list vs. detail)
before committing to convert all of them.

## Reproducing with a real Anthropic key

`scripts/measure_toon.py` calls the LiteLLM proxy's `/utils/token_counter`
because no `ANTHROPIC_API_KEY` was available here. To get Anthropic's
actual billing counts, change one thing: swap `count_tokens()`'s httpx
call for `anthropic.Anthropic().messages.count_tokens(model=..., messages=...).input_tokens`,
add `anthropic` to the script's `dependencies`, and drop the vault/proxy
plumbing. The `--pair NAME ROWS JSON_FILE TOON_FILE` capture format and
the report table stay the same.
