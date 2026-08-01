# `litellm_keys` — Sherpa tool design

**Date:** 2026-08-01
**Status:** approved, ready for implementation plan

## Problem

Minting a LiteLLM proxy key with the right name and model permissions, then
getting the secret into wherever it's needed (a `.env`, the Sherpa vault, a
Python session, 1Password) is slow and annoying to do by hand. Worse: a freshly
minted `sk-...` printed to a terminal lands in Claude Code's transcript and trips
secret-scanning hooks. The goal is to make key generation and safe placement
trivial, with the raw secret **never** touching stdout, the transcript, or any
shell command line.

## Non-goals (this spec)

- Usage / spend observability ("why are my fans spinning, who is hammering the
  proxy") — deferred to a **Phase 2** spec that builds on the newly-enabled
  prompt logging. Explicitly out of scope here.
- Budget, expiry/duration, and rate-limit (rpm/tpm) flags on `generate`. Easy to
  add later; not wanted now.

## Proxy facts (verified 2026-08-01)

- Proxy: `https://llm.jc.turbo.inc` (from vault `LITELLM_API_URL`). Healthy,
  DB connected.
- The existing vault `LITELLM_API_KEY` is a **scoped consumer key** — it returns
  `403` on `/key/list` and `/user/list`, so it cannot mint keys. A dedicated
  admin/master credential is required.

## Command surface (Phase 1)

```
litellm_keys                      # home view
litellm_keys generate --name <alias> [--models m1,m2,…] [--note "..."]
                       [--vault [NAME]] [--env-file PATH [--var NAME]]
                       [--op [ITEM]] [--clip] [--reveal] [--json]
litellm_keys list [--fields ...] [--json]
litellm_keys info <alias|hash> [--json]
litellm_keys delete <alias|hash> [--json]
```

### `generate`

- `--name` (required) → LiteLLM `key_alias`.
- `--models` → comma-separated allowlist; default = all models the proxy exposes.
- `--note` → stored in the key's LiteLLM `metadata`, and passed to any sink that
  carries notes (1Password item, optionally a comment in `.env`).
- Sinks (see below). At least one always applies; default is `--vault`.
- Output: masked preview (`sk-…<last4>`) + the alias, models, and which sink(s)
  received the secret. Never the raw value unless `--reveal`.

### `list`

- Default fields (≤4): `alias`, `models`, `spend`, `created`.
- `--fields` to widen. True totals (`count: N of M total`).
- Contextual hints on output (e.g. `sherpa litellm_keys info <ALIAS>`).

### `info <alias|hash>`

- Resolve alias → key hash via `/key/list` when an alias is given.
- Detail view: alias, models, metadata/note, spend, created, expires. **Never**
  the secret. No contextual hints (detail view).

### `delete <alias|hash>`

- Revoke via `/key/delete`. Idempotent no-op (exit 0) if already gone.
- Confirmation via flags only — no interactive prompt.

## Security / sink model (load-bearing)

The raw secret must never appear in stdout, the Claude transcript, or a shell
command line.

- **Masked stdout by default.** Normal output shows `sk-…<last4>` only. `--reveal`
  is the single, deliberate opt-in that prints the full secret to stdout.
- **All secret placement is in-process.** The tool itself writes
  `~/.sherpa/vault.json`, writes/updates the `.env` file, and pipes the secret to
  `op` via **stdin**. It never shells out `vault_manager set <KEY> <secret>` or
  similar, because the secret would then appear in the Bash command line that the
  harness logs.
- **Sinks (repeatable; choose any combination):**
  - `--vault [NAME]` — write to `~/.sherpa/vault.json`. Default key name
    `LITELLM_KEY_<ALIAS>` (alias uppercased, non-alphanumerics → `_`).
  - `--env-file PATH [--var NAME]` — create/update the file, setting `VAR=value`
    (default var name = same derivation as vault). Preserves other lines; updates
    in place if the var already exists.
  - `--op [ITEM]` — 1Password via the `op` CLI, secret passed on stdin. If `op`
    is not installed or not signed in, fail cleanly (exit 2, actionable `help:`),
    having still applied any other requested sinks.
  - `--clip` — system clipboard (secret never touches stdout or disk).
  - **Default when no sink flag is given:** `--vault` with the derived name, so a
    generated secret is always captured safely somewhere.
- The masked preview and sink report are the only key-related data on stdout.

## Auth & configuration

- **Master key:** new vault secret `LITELLM_MASTER_KEY`, distinct from the
  consumer `LITELLM_API_KEY`. Used for all admin operations.
- Missing-secret protocol: `MISSING_SECRET: LITELLM_MASTER_KEY` on stderr **and**
  the structured `error:`/`help:` pair on stdout via `fail()`, exit 2.
- **Proxy URL:** reuse vault `LITELLM_API_URL`.
- **Endpoints:** `POST /key/generate`, `GET /key/list`, `GET /key/info`,
  `POST /key/delete`. Exact request/response shapes confirmed against the live
  proxy during implementation.

## AXI output contract

- stdout is TOON via `emit()`; every subcommand accepts `--json` for the raw shape.
- Strict flag parsing via `parse_strict()`; unknown flags rejected (exit 2) with
  the valid set listed.
- Exit codes per `docs/SHERPA_STANDARDS.md`: `0` success/no-op, `1` uncooperative
  world (network, proxy 5xx), `2` caller must fix flags or environment (missing
  secret, unauthorized/401/403, bad flag).
- No interactive prompts; every operation completable by flags alone.
- No-args home view: plain `bin:` / `description:` / `hint:` lines (not a TOON
  document), showing proxy URL, whether `LITELLM_MASTER_KEY` is present, and the
  current key count.
- `help` values in emitted payloads are always lists of strings.
- Docstring YAML: `name: litellm_keys`, `categories: [ai, llm, admin, secrets]`,
  `secrets: [LITELLM_MASTER_KEY, LITELLM_API_URL]`, `axi: true`.

## Testing

- Unit: sink writers (vault merge, `.env` create + in-place update, name
  derivation, masking) with no network — the secret-handling code is the riskiest
  part and must be covered directly.
- Integration (guarded / manual): against the live proxy once `LITELLM_MASTER_KEY`
  is stored — generate → info → list → delete round-trip, asserting the raw
  secret never appears in captured stdout.
- Contract: `--json` shape stable; exit codes; `MISSING_SECRET` on stderr when the
  master key is absent.
