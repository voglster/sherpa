# Slack Corpus — Design

**Date:** 2026-09-11
**Status:** Approved design, pending implementation plan

## Problem

Jim commits to things in Slack and forgets them. "I promised Hunter some gravi
CLI changes, and Erica something too." There is no way to ask an agent what he
owes people, or what came at him that he dropped.

Slack's own search does not answer these questions: it is keyword-only, scoped
to one workspace, and returns single messages stripped of the conversation that
gives them meaning. Nothing in the existing toolchain indexes Slack at all —
`slack_messenger` only sends.

## Goal

A local, always-current corpus of Jim's Slack that his agents (Claude Code via
the sherpa MCP server, `pi` via the CLI) can query in natural language and get
back readable conversation, not message fragments.

The two queries that define success:

1. **"What did I promise Hunter?"** — retrieve Jim's own messages to a person,
   with surrounding conversation, so an agent can read and summarize the
   commitments.
2. **"Did I miss anything?"** — retrieve messages addressed to Jim that he never
   responded to.

## Non-goals

- Not a team or org tool. Single user, single machine, no auth layer, no web UI,
  never exposed publicly.
- Not an extraction pipeline. No commitment classifier, no task objects, no
  automatic writes to tududi. See "Why no extractor" below.
- Not a replacement for Slack search on recent messages. This is for the things
  that have scrolled away.

## Scope of ingest

All DMs, group DMs, and every channel Jim is a member of, full history, across
every configured workspace. Nothing is joined that he is not already in; the
corpus contains exactly what he can already see.

Backfill runs in priority order so the corpus is useful before it is complete:

1. DMs and group DMs
2. Channels Jim has posted in
3. Remaining channels he is a member of

Newest-first within each tier.

## Architecture

```
crontab
  └─> ingest (per workspace)
        ├─ fetch  — conversations.list/history/replies, cursor per channel
        ├─ resolve — user + channel IDs to human names (cached)
        ├─ chunk  — conversation windows
        └─ embed  — nomic-embed-text via LiteLLM gateway
              └─> corpus.db  (SQLite: messages + FTS5 + chunks + sqlite-vec)
                    └─> sherpa corpus  (hybrid retrieval, context expansion)
                          └─> Claude Code (MCP) | pi (CLI) | Jim (CLI)
```

Everything on the desktop. One SQLite file. No service, no container, no daemon.

**Why the desktop and not `hd`:** the consumers are local agents, so a local
file is the shortest query path and needs no API layer. Ingest only needs to
*catch up*, not run continuously — Slack's cursors make a cold start after
downtime as cheap as a warm one. If the corpus later needs to be reachable from
other hosts, that is a config change plus an HTTP shim over the same query
functions; it is not designed for now.

**Why one SQLite file and not qdrant:** sizing is roughly 500k messages → ~50k
chunks → ~150MB of 768-dim vectors, which `sqlite-vec` brute-forces in
milliseconds. A vector service would add a daemon and a second thing to back up
in exchange for nothing at this scale.

## Data model

```sql
messages(
  id TEXT PRIMARY KEY,        -- workspace:channel:ts
  workspace TEXT,
  channel_id TEXT, channel_name TEXT,
  channel_kind TEXT,          -- im | mpim | public | private
  ts REAL, thread_ts REAL,
  author_id TEXT, author_name TEXT,
  is_me INTEGER,
  text TEXT,
  permalink TEXT,
  meta JSON                   -- reactions, files, edits, subtype
)

messages_fts                  -- FTS5 over text, author_name, channel_name

chunks(
  id INTEGER PRIMARY KEY,
  workspace TEXT, channel_id TEXT,
  start_ts REAL, end_ts REAL,
  thread_ts REAL,             -- non-null when the chunk is a thread
  participants TEXT,          -- resolved names, comma-separated
  msg_first_id TEXT, msg_last_id TEXT,
  header TEXT,                -- synthesized context line, see below
  text TEXT                   -- script format, see below
)

chunks_fts                    -- FTS5 over header || text
chunks_vec                    -- sqlite-vec, 768-dim

channels(workspace, id, name, kind, is_member, member_ids)
users(workspace, id, name, display_name, is_me)
sync_state(workspace, channel_id, last_ts, tier, status, updated_at)
```

`is_me` is load-bearing. "What did I promise Hunter" is a filter on `is_me = 1`
plus a person, and making it an indexed column means retrieval does not depend
on the model inferring authorship from text.

IDs are resolved to human names **at ingest time**. An agent searching for
"Hunter" must not need to know he is `U03K2...`.

## Chunking

The unit of embedding is a conversation window, never a single message. Slack
messages are too short and too context-dependent to embed alone — "yeah I'll get
that to you friday" carries almost no standalone semantic signal, and it is
precisely the message these queries need to find.

Windows are formed as:

- **A thread is one chunk.** If a thread exceeds ~1500 tokens, split it by
  message count with overlap, never mid-message.
- **Loose channel messages** are windowed by a gap heuristic: a new chunk starts
  after 30 minutes of silence or 40 messages, whichever comes first.
- **Adjacent chunks overlap by 2 messages**, so a commitment landing on a
  boundary appears in both.

### Chunk text format

Script format, with resolved speaker names:

```
Jim Vogel: can you take the gravi cli flag work?
Hunter Reeves: yeah if you can get me the schema by friday
Jim Vogel: I'll have it to you thursday
```

This puts speaker identity into the embedding, so speaker-scoped questions match
on the conversation rather than relying entirely on metadata filters.

### Contextual header

Each chunk is prefixed, for both embedding and FTS indexing, with a synthesized
context line:

```
DM between Jim Vogel and Hunter Reeves, 2026-08-14, #gravi-cli
```

This is Anthropic's Contextual Retrieval technique, which cut retrieval failures
49% when applied to both the vector and BM25 indexes. For documents it requires
an LLM call per chunk to describe where the chunk sits. Chat does not: the
corpus already holds channel, participants, date, and thread parent as
structured fields, so the header is generated deterministically from metadata at
zero backfill cost.

An optional later pass may prepend an LLM-written topic sentence. Not in v1.

## Embeddings

`nomic-embed-text` (768-dim) via the LiteLLM gateway at
`https://llm.jc.turbo.inc/v1`, which routes to ollama on nodes 45/46/16. All
local; no Slack content leaves the home network.

Backfill is a single pass over chunks, estimated 10–15 minutes for ~50k chunks.
Incremental runs re-embed only new or modified chunks, driven off the same cron
invocation. A `chunks.content_hash` guards against redundant re-embedding when a
chunk is rebuilt unchanged.

## Retrieval

```
filters (person, workspace, channel, date range, is_me)
  ├─> BM25 over chunks_fts       ──┐
  └─> KNN over chunks_vec        ──┴─> RRF fusion ─> top-k chunks
                                          └─> hydrate to messages ± context
                                                └─> dedupe overlapping ranges
                                                      └─> transcripts + permalinks
```

Hard filters are applied **before** fusion, not after, so a `--from hunter`
query spends its whole k-budget inside that conversation.

**Why hybrid and not pure vector:** Jim's work Slack is dense with exact tokens
— Jira keys, repo names, hostnames, people — which is where dense retrieval
reliably whiffs. RRF is used rather than weighted score blending because BM25
and cosine scores are not on comparable scales; RRF is rank-only and sidesteps
that entirely.

**Context expansion:** because chunks are already conversation windows, the
retrieved unit has context by construction. `--context N` widens the hydrated
message range N messages either side of the chunk boundary. Overlapping ranges
from multiple hits are merged into one transcript rather than returned twice.

**Reranking is deferred.** The same Anthropic benchmark improves from 49% to 67%
failure reduction by adding a cross-encoder reranker over the fused results.
That is the largest remaining win and the obvious v2 addition, but it puts a
model in the query path and should wait until the corpus proves its retrieval is
the bottleneck.

## Query surface

A `sherpa corpus` tool, AXI-conformant per `docs/SHERPA_STANDARDS.md`.

| Subcommand | Tier | Purpose |
|---|---|---|
| `search <query>` | read | Hybrid retrieval. Flags: `--from`, `--to`, `--mine`, `--since`, `--until`, `--workspace`, `--channel`, `--k`, `--context` |
| `with <person>` | read | Full conversation with someone, both directions, newest-first, paged |
| `owed <person>` | read | Jim's sent messages to/about a person, newest-first, for the agent to read and summarize |
| `thread <permalink\|id>` | read | Full thread around a message |
| `sync` | write | Run ingest now (what cron calls) |
| `status` | read | Per-workspace/channel sync state, corpus size, last run |

`owed` is deliberately a filtered query, not a classifier. It hands the agent
Jim's recent messages to a person and lets Claude or pi read them. This is the
design's central bet and it is defended below.

Reaches all three consumers with no extra work: `sherpa corpus` on the CLI, the
sherpa MCP server for Claude Code, the same CLI for `pi`.

## Why no extractor

*Verbatim Chunks Beat Extracted Artifacts* (arXiv 2601.00821, Jan 2026) ablates
exactly this choice, holding the retrieval pipeline constant and varying only
what is stored. Verbatim conversation chunks beat LLM-extracted typed artifacts
— summaries, facts, action items — by 15.9 points on LoCoMo (43.9% vs 28.0%) and
22.0 points on LongMemEval-S (67.4% vs 45.4%). A semantic graph representation
did not close the gap. The identified cause is lossy distillation: accuracy
tracks how much source text survives into storage.

An extractor that renders "I'll have it to you thursday" as a task object
discards the phrasing, the hedge, and the surrounding negotiation — which is the
part Jim actually wants to read.

The paper's caveat is adopted as a constraint: extracted artifacts should
*augment* verbatim text, never replace it. If commitment extraction is built
later, it lands as an auxiliary index beside the corpus, and retrieval continues
to return verbatim conversation.

## Multi-workspace

Workspace support is in the schema and config from v1, since more workspaces are
expected.

```toml
# ~/.sherpa/corpus.toml
db = "~/.sherpa/corpus.db"

[workspace.gravitate]
token_key = "SLACK_USER_TOKEN_GRAVITATE"   # vault key
me = "U03..."

[workspace.other]
token_key = "SLACK_USER_TOKEN_OTHER"
me = "U07..."
```

Tokens live in the existing sherpa vault (`~/.sherpa/vault.json`), extending the
`SLACK_USER_TOKEN` convention `slack_messenger` already uses rather than
introducing a second secrets path.

**Auth:** internal Slack app per workspace with a user token (`xoxp`). Jim owns
both current workspaces, so installation is not a constraint, and internal apps
were explicitly exempted from the 2025 rate-limit reduction on
`conversations.history`. The token is obtained through a small provider
interface so a browser-session-token workspace (`xoxc`/`xoxd`) can be added
later without touching the ingester.

**Terms of service:** Slack's API ToS restricts persistent archives of Slack
data and its use with LLMs. This corpus is single-user, local, covers only
content Jim can already read, is never exposed off the machine, and uses local
models for inference. Jim owns the workspaces involved and has made this call
knowingly.

## Ingest mechanics

- Cron entry invokes `sherpa corpus sync`.
- Per-channel cursor in `sync_state.last_ts`; `conversations.history` pages
  forward from it.
- A separate thread sweep re-checks recently-active `thread_ts` parents for new
  replies, since replies to old parents do not advance the channel cursor.
- Rate limiting: token-bucket honoring `Retry-After` on 429. Backfill tiers run
  to a wall-clock budget per invocation so a cold start spreads across runs
  instead of hammering the API in one burst.
- Idempotent on `messages.id`; edits update in place and mark the owning chunk
  dirty for re-chunk and re-embed.
- Deletions and `tombstone` subtypes are honored — a deleted message is removed,
  not retained.

## Error handling

**Fail loudly, never degrade quietly.** Jim owns every piece of hardware in this
path and can fix any of it within minutes. The expensive failure is not a broken
run — it is a corpus that keeps answering while silently missing data, because
every answer it gives is then untrustworthy in a way nothing surfaces. A sync
that stops and shouts costs an evening; a sync that half-works costs the
credibility of every query built on it.

So:

- **Embedding gateway unavailable → abort the run.** No degrading to
  keyword-only, no queueing chunks as unembedded. Push via `sherpa notify` and
  exit non-zero so cron mail carries it too. Messages already committed stay
  committed; the run simply stops advancing.
- **Token expired or revoked → abort that workspace's run.** Push via
  `sherpa notify` naming the workspace and the vault key to refresh, and exit
  non-zero.
- **A channel erroring** marks `sync_state.status` and lets other channels
  finish, but the run still exits non-zero with the failed channels named. One
  bad channel should not block the other 200; it also should not pass unnoticed.
- **`search` refuses to answer on a stale corpus.** If the last successful sync
  for any configured workspace is older than a threshold (default 24h), or any
  chunk is unembedded, retrieval prints a prominent warning above its results
  naming the affected workspace and the staleness. `--stale-ok` suppresses it
  for deliberate offline use. An agent reading results must be told when the
  corpus cannot answer completely — otherwise it will confidently report that
  Jim promised nobody anything.
- `status` reports last successful sync per workspace, unembedded chunk count,
  and any channel in a failed state, and exits non-zero if the corpus is not
  fully healthy.

## Testing

- Ingest, chunking, and retrieval are separately testable; the Slack client sits
  behind an interface with a fixture-backed fake.
- Chunking: golden tests over recorded conversation fixtures — gap boundaries,
  thread splitting, overlap, script formatting.
- Retrieval: a small labeled set of query → expected-message assertions built
  from real corpus content once it exists, including the two motivating queries.
  This is the only honest way to know whether reranking is later needed.
- Ingest: cursor advance, idempotent re-run, edit/delete handling, 429 backoff,
  resumption after interruption mid-tier.

## Future sources

The single-table design exists so that adding a source is a script that inserts
into `messages` and lets the shared chunker and embedder handle the rest:

- Meeting/call transcripts — `llmbox` already runs WhisperLive.
- Screenshots — `qwen3-vl:8b` is already loaded for OCR/description.
- Gmail, Jira comments, Claude Code session transcripts.

No connector framework is planned. Each source is an independent script, which
is what keeps them cheap to add and impossible to break each other.
