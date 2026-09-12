# Slack corpus — setup and operation

A local SQLite corpus of your Slack, synced incrementally on a timer, queryable by
your agents through `sherpa corpus`. Everything stays on this machine; embeddings
run on your own hardware.

## What it answers

- `sherpa corpus owed "<person>"` — your own messages to someone, with the
  surrounding conversation, for an agent to read and summarise. A filter, not a
  classifier: it hands back verbatim conversation and lets the agent reason.
- `sherpa corpus with "<person>"` — the whole conversation, both directions.
- `sherpa corpus search "<query>"` — hybrid BM25 + vector retrieval.
- `sherpa corpus thread "<permalink>"` — the full thread around a message.
- `sherpa corpus sync` / `status` — ingest and health.

## Slack app

The corpus reads through a user token. Scopes required, as **User Token Scopes**:

```
channels:history  channels:read
groups:history    groups:read
im:history        im:read
mpim:history      mpim:read
users:read
```

Adding scopes to an existing app requires a **reinstall**; that may rotate the
token, which breaks anything else using it. Current install uses the "jims minion"
app, whose token is shared with `slack_messenger` and `slack_pomodoro`.

```
sherpa vault_manager set SLACK_USER_TOKEN xoxp-...
```

## Embeddings

`nomic-embed-text` (768-dim) through the LiteLLM gateway. The key is scoped to
that one model:

```
sherpa litellm_keys generate --name corpus \
  --models ollama/nomic-embed-text:latest --vault LITELLM_KEY_CORPUS
```

**Gateway routing matters.** The generic `ollama/nomic-embed-text:latest` alias
round-robins across ollama hosts. `10.0.6.45` (this desktop) was in that pool and
also serves 40GB models, so embedding requests landing there hung for minutes
behind model swaps and aborted whole syncs. It was removed from the pool on
2026-09-12; the restore config is at
`/tmp/claude-1000/litellm-node45-nomic-backup.json`. If embeddings start stalling
again, check `/health` with `LITELLM_MASTER_KEY` before suspecting this code.

## Config

`~/.sherpa/corpus.toml`:

```toml
db = "~/.sherpa/corpus.db"
stale_after_hours = 24
# knn_distance_floor = 0.96   # see "Tuning" below

[workspace.gravitate]
token_key = "SLACK_USER_TOKEN"
```

Add a workspace by adding a `[workspace.<name>]` block with its own `token_key`,
then `sherpa corpus sync --workspace <name>`.

## Scheduling

This machine has no `crontab`; scheduling is a systemd user timer.

```
~/.config/systemd/user/slack-corpus.service   oneshot: sherpa corpus sync --budget 600
~/.config/systemd/user/slack-corpus.timer     OnUnitActiveSec=15min, Persistent=true
```

```bash
systemctl --user status slack-corpus.service      # last run
systemctl --user list-timers slack-corpus.timer   # next run
journalctl --user -u slack-corpus.service -n 50   # output
systemctl --user disable --now slack-corpus.timer # stop syncing
```

`sync` takes an advisory lock, so a run that overruns its 15-minute slot does not
collide with the next — the second exits cleanly saying it stepped aside.
`Persistent=true` means a missed run (laptop asleep) fires on resume.

## Reading `status`

`status` exits non-zero only for faults you can act on: staleness, failed
channels, unembedded chunks. Progress states — `backfilling`, `unswept_threads`,
`never_synced` — appear as warnings but exit 0, because they are outstanding work
rather than something broken.

**Trust coverage, not counts.** A large message count says nothing about
completeness. The number that matters is how many channels you belong to have no
messages at all:

```sql
SELECT count(*) FROM channels ch WHERE ch.is_member = 1
  AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.channel_id = ch.id);
```

## Tuning and how `search` avoids making things up

Two independent guards, because they catch different failures.

**Adaptive distance floor.** A vector-only hit must sit at least 3 standard
deviations below the background distance for that query, measured against 200
randomly sampled chunks per search. An earlier absolute threshold
(`knn_distance_floor`, default 0.96) proved to decay as the corpus grew — it
rejected nonsense at 44k chunks and admitted it at 72k — because the nearest
neighbour of any query gets closer as chunks accumulate. The relative rule does
not drift with size. The absolute value survives only as a fallback when the
sample is too small, and is still overridable in `corpus.toml`.

**Term coverage.** Distance cannot detect a well-formed question about something
absent — "the acquisition of Northwind Logistics" is genuinely close to business
Slack in embedding space whether or not it happened. So each substantive query
term is counted against the index. Any term with zero occurrences produces a
warning naming it; if every term is absent, `search` returns nothing with a note.
This is the guard that stops an agent summarising three topically-related
conversations as an answer.

BM25 bypasses both, so exact-token queries (a Jira key, a hostname, a repo) are
never suppressed.

If real searches start coming back empty when they should not, the term-coverage
short-circuit is the first thing to look at — it trades some pure-paraphrase
recall for the guarantee above.

## What is deliberately excluded

Bot-authored messages are stored but not chunked, so they never appear in
`search`. About a third of this workspace is Jira, build and alert noise, and it
was crowding out real conversation. They still appear in `owed`/`with`
transcripts when they sit beside something a human said. To index them, drop the
bot check in `chunk.py` and rechunk.

## Security note

The corpus concentrates secrets that were scattered across years of Slack into
one unencrypted, greppable file that any agent pointed at it can read. A scan of
225k messages on 2026-09-12 found 11 messages with `API_KEY=`/`SECRET=`-style
assignments, 7 with Google API keys, and 4 containing private key blocks. That is
a real change in exposure even though the content was already in Slack. Options,
undecided: redact credential-shaped strings at ingest, encrypt at rest, or accept
it.
