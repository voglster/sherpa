# AXI catalog triage

Decision record for converting sherpa's remaining tools to the AXI contract
(`docs/SHERPA_STANDARDS.md`). Consumes the measurement in
`docs/axi-measurement.md`: schema-matched TOON savings run **41-48%** on
list/table-shaped output (many uniform rows — TOON folds the repeated field
names into one header) and **~28%** on single-object/detail-shaped output
(no repeated keys to fold; the gain there is mostly pretty-vs-compact JSON,
not TOON encoding). Priority below follows the **shape** column, not a
guess at tool importance, per that finding. A tool already emitting a lean
3-4 field default should not be promised pilot-level savings — it has
little left to shed.

Executing any row below (conversion or replacement) is out of scope for
this document; this is the decision record only.

**Row count vs. tool count**: `sherpa list` shows 21 top-level tools. The
table below has more than 21 rows because tools that mix list-shaped and
detail-shaped subcommands are split into one row per shape-group (e.g.
`jira_issues` becomes a "done" row for `search` plus two axi-ify rows for
its other subcommands), so the priority column stays honest about which
subcommands actually benefit. The two "replace candidate rejected" rows
are commentary on `client_db` and `slack_messenger`, not additional
tools. To reconcile row count against the bucket summary below: count
distinct tool names, not rows.

## Legend

- **Shape**: `list` (many uniform rows — the shape TOON's key-folding
  benefits from), `detail` (single object/scalar — folding has nothing to
  work on), or `mixed` (tool has both list and detail subcommands).
- **Decision**: `done` (already AXI-converted), `axi-ify` (convert to the
  contract), `replace` (swap for an existing community AXI), `leave`
  (not worth converting).
- **Priority**: `high` / `medium` / `low` / `n/a`, driven by shape.

## Table

| Tool | Shape | Decision | Existing AXI | Priority | Rationale |
|---|---|---|---|---|---|
| `jira_issues search` | list | done | — | — | Converted in this project; measured 41-48% schema-matched savings (`docs/axi-measurement.md`). |
| `youtube` | mixed (1 detail obj + nested 18-row list) | done | — | — | Converted in this project; measured ~28% (detail-dominated; the nested chapters list is the only source of key-folding). |
| `jira_issues get/create/update/transition/comment` | detail | axi-ify | — | medium | Five subcommands left unconverted on an already-`axi: true` file. Each returns one issue/comment object, so expect youtube-detail-level savings (~28%), not jira-search-level — but still worth doing for schema minimization, `--json` parity, and consistent exit codes across the same tool. |
| `jira_issues sprints` | list | axi-ify | — | high | Returns a uniform list of sprint objects (`id, name, state, startDate, endDate` — already only 5 fields, so schema-matched savings will run closer to the 41% floor than the 48% ceiling, but it's still list-shaped and worth doing alongside the other subcommands). |
| `jira_pulse pulse` | list (activity entries over a time window) | axi-ify | — | high | Explicitly a "pulse" of many activity/status-movement rows across a project; classic list shape. |
| `jira_pulse epic-progress` | list (one row per epic) | axi-ify | — | high | Per-epic progress rows across a project; list shape. |
| `jira_admin users <query>` | list (search results) | axi-ify | — | medium | Search returns a candidate list to disambiguate from; list shape, but likely small row counts (a handful of matches), so expect savings nearer the 41% floor. |
| `jira_admin epic/subtask/complete-subtask/assign-subtask/link/weblink` | detail (single mutation confirmation) | axi-ify | — | low | Each returns one confirmation object for one issue action. Detail-shaped; convert for contract consistency (`--json`, exit codes, `fail()`) more than for token savings. |
| `client_db find/aggregate` | list (query result rows) | axi-ify | — | high | Primary use is returning many uniform documents from a MongoDB query — the clearest list shape in the catalog outside jira. See "replace candidates rejected" below for why this is axi-ify, not replace. |
| `client_db count` | detail (single number) | axi-ify | — | low | Single scalar result; convert for contract consistency only. |
| `client_db config` | detail (one instance's connection info + database list) | axi-ify | — | low | Returns one instance's config as a single object; detail-shaped even though it lists databases, since those are nested inside one instance's record, not a row-per-instance table. |
| `client_db search/collections` | list (client instances / collection names) | axi-ify | — | medium | Search-by-name and collection listing are both small uniform lists. |
| `knowledge list/search` | list | axi-ify | — | high | Facts store; `list`/`search` return many uniform key/value/tags rows — direct list shape, same family as jira search. |
| `knowledge get/add/remove` | detail | axi-ify | — | low | Single-entry operations; convert for consistency. |
| `notes_search search/tags/links/backlinks` | list | axi-ify | — | high | Search and graph-traversal commands return many uniform note/tag/link rows. |
| `notes_search read/context/create/append/edit/rename/delete` | detail (full note content, or one note's link/backlink bundle) | axi-ify | — | low | `context` bundles content + links + backlinks + snippets for *one* note — richer than a scalar but still single-subject, not repeated uniform rows; expect detail-level savings. |
| `sentry_issues fetch/resolve` | detail | axi-ify | — | low | Both return one issue's detail. No list subcommand exists. Low priority but cheap and harmless to convert (TOON degrades gracefully on scalars, per the measurement doc). |
| `slack_messenger channels/users` | list | axi-ify | — | high | `channels [--filter]` and `users [--filter]` return many uniform rows (name, id, etc.) — the local caching/fuzzy-match logic lives in the lookup, not the output shape, so conversion doesn't touch it. |
| `slack_messenger send/dm` | detail (single message-sent confirmation) | axi-ify | — | low | One confirmation per call; convert for contract consistency. See "replace candidates rejected" — this subcommand pair is exactly where the custom mention/link/cache logic lives, which is why `slack_messenger` as a whole is axi-ify, not replace. |
| `slack_pomodoro start/status/cancel` | detail | axi-ify | — | low | Real subcommands are `start`, `status`, and `cancel` (there is no `stop`). `start` and `cancel` each return one confirmation of the state change they just made; `status` is its own query returning the current timer's single snapshot rather than something `start`/`cancel` hand back. All three are single-object, detail-shaped. The internal `_daemon` subcommand is excluded from this table — it is the background process `start` forks into, not a user-invocable command, so it has no independent output contract to triage. |
| `ask_ai models` | list | axi-ify | — | medium | Returns a list of available models; moderate row count expected. |
| `ask_ai default` | detail (get or set the default model) | axi-ify | — | low | Returns/sets one model-name value; detail-shaped, no row-per-item output. |
| `ask_ai ask` | detail (single completion) | axi-ify | — | low | One prompt, one response; detail-shaped. |
| `unsplash_search search` | list | axi-ify | — | medium | Returns N photo results, uniform fields (id, url, description, etc.) — list shape, moderate row counts (`--count`, default likely small). |
| `unsplash_search download` | detail | axi-ify | — | low | Single-file download confirmation. |
| `vault_manager list` | list (key/value rows, values likely redacted) | axi-ify | — | low | List-shaped but almost certainly tiny (a handful of vault keys) and already minimal fields — expect savings well below even the 41% floor; convert mainly for contract consistency, not tokens. |
| `vault_manager get/set/delete` | detail | axi-ify | — | low | Single-key operations. |
| `image_edit info` | detail | axi-ify | — | low | Returns one image's metadata (dimensions, format, etc.); no list subcommand. |
| `image_edit crop/resize/beautify/annotate/convert/icon` | detail | axi-ify | — | low | Each returns one output-file confirmation; detail-shaped throughout. |
| `image_gen generate` | detail (or small list when `--count` > 1) | axi-ify | — | low | Typically 1 image per call; `--count` can produce a short list of generated-file rows, but counts are small (few images), so still low priority. |
| `notify` | detail (single delivery confirmation) | axi-ify | — | low | One notification per call; no list subcommand exists. Cheap, low payoff. |
| `lumbergh sessions list` / `todos list` / `prompts list` | list | axi-ify | — | medium | Session, todo, and prompt lists are uniform-row collections, though typically short (a handful of todos/prompts per session) — expect savings toward the 41% floor, scaling with how many todos a session accumulates. |
| `lumbergh scratchpad get/set/append` / `todos add/done/undone/remove/move` / `prompts get/set` | detail | axi-ify | — | low | Single-item operations; convert for contract consistency. |
| `fleet status` | list (one row per worker) | axi-ify | — | medium | Status across all workers in a run is a uniform-row list, though a run's worker count is typically small (a handful of parallel agents), so savings will sit toward the 41% floor rather than the jira-100-row ceiling. |
| `fleet init/spawn/watch/send/land/kill/clear/report/ask/inbox/stop-hook` | detail | axi-ify | — | low | Every other subcommand acts on one worker/run and returns a single confirmation or state object; detail-shaped. |
| `client_db` — replace candidate rejected | — | axi-ify (not replace) | `mongodb-axi` (community) | — | `mongodb-axi` exists but does not cover this tool's two safety/routing features: (1) client-instance search-by-name, which resolves a human-given name to the correct MongoDB connection before any query runs — `mongodb-axi` has no concept of "which of N client databases"; (2) the `MONGO_RO_*` / `MONGO_RW_*` credential split, which is a safety property (read-only by default, `--rw` required to write) not a convenience `mongodb-axi` is known to replicate. Swapping would silently drop both. Keep local; axi-ify instead. |
| `slack_messenger` — replace candidate rejected | — | axi-ify (not replace) | `slack-axi` (community) | — | `slack-axi` exists but does not cover: `@(name)` mention resolution, automatic Jira-key auto-linking (`[A-Z][A-Z0-9]+-\d+` → link), fuzzy user/channel lookup with local disk caching (`~/.sherpa/cache`), `--blocks` support, or threading (`--thread`). All five are custom behavior baked into this tool, not generic Slack-API wrapping. Swapping would drop all of them. Keep local; axi-ify instead. |
| `reindex` | detail (or none — triggers a rebuild, returns index stats) | leave | — | n/a | Maintenance/meta-tool with no `axi:`-relevant docstring fields at all (no `secrets`, minimal `usage`); thin wrapper around the indexer subprocess. Converting buys negligible savings for an internal-only tool that isn't queried for information. |
| `web_read fetch` | detail (one URL's markdown body) | leave | `gws-axi`? no direct match | n/a | Output is one large markdown blob per call, not a row-shaped payload — TOON has nothing to fold. The dominant cost is the fetched page content itself, which TOON/JSON framing barely touches. Not worth the conversion effort. |
| `web_search search` | list (search results) | axi-ify (reconsidered from brief's "leave") | — | medium | The brief suggested "leave," but this is genuinely list-shaped (multiple uniform `title/url/snippet` rows) and already close to the jira-search shape — it fits the high-payoff pattern better than `web_read` does. Demoted to medium rather than high because result counts are typically small (`--limit`, default 5). |

## Summary by decision

- **done**: 2 tools (`youtube`, `jira_issues search`) — pilots, not candidates.
- **axi-ify**: every remaining tool/subcommand-group in the catalog (18 top-level tools, one of which — `jira_issues`'s six unconverted subcommands — is partially done). None of the 18 remaining tools has a safe drop-in AXI replacement once local features are checked (see rejections below).
- **replace**: 0. Both candidates named in the plan brief (`client_db` → `mongodb-axi`, `slack_messenger` → `slack-axi`) were checked against local feature lists and rejected — see the two rejection rows above. No other remaining tool has a plausible community AXI match: `jira_admin`/`jira_pulse`/`jira_issues` have no `jira-axi` in the catalog (confirmed absent); `fleet`, `lumbergh`, `knowledge`, `notes_search`, `sentry_issues`, `ask_ai`, `image_edit`, `image_gen`, `unsplash_search`, `vault_manager`, `notify`, `slack_pomodoro`, `web_read` have no corresponding official or community AXI at all.
- **leave**: 2 tools (`reindex`, `web_read`) — thin, non-row-shaped, low value to convert. (`web_search` was moved out of "leave" into axi-ify — see its row above.)

## Priority ordering (axi-ify only), by shape-driven payoff

**High** (uniform lists, meaningful row counts — closest to the measured 41-48% band):
1. `jira_issues sprints`
2. `jira_pulse pulse`
3. `jira_pulse epic-progress`
4. `client_db find/aggregate`
5. `knowledge list/search`
6. `notes_search search/tags/links/backlinks`
7. `slack_messenger channels/users`

**Medium** (list-shaped but typically short row counts, or lean fields already — expect savings nearer the 41% floor or below):
8. `jira_admin users <query>`
9. `client_db search/collections`
10. `ask_ai models`
11. `unsplash_search search`
12. `lumbergh sessions list` / `todos list` / `prompts list`
13. `fleet status`
14. `web_search search`

**Low** (detail-shaped single-object/scalar output — convert for contract consistency, not for token savings; expect ~28% or less, per the youtube-detail baseline):
15. `jira_issues get/create/update/transition/comment`
16. `jira_admin epic/subtask/complete-subtask/assign-subtask/link/weblink`
17. `client_db count`
18. `knowledge get/add/remove`
19. `notes_search read/context/create/append/edit/rename/delete`
20. `sentry_issues fetch/resolve`
21. `slack_messenger send/dm`
22. `slack_pomodoro start/status/cancel`
23. `ask_ai ask`
24. `ask_ai default`
25. `client_db config`
26. `unsplash_search download`
27. `vault_manager list/get/set/delete`
28. `image_edit` (all subcommands)
29. `image_gen generate`
30. `notify`
31. `lumbergh` (single-item subcommands)
32. `fleet` (all subcommands except `status`)

## Notes on tools with both list and detail subcommands

Several tools (`jira_issues`, `jira_admin`, `client_db`, `knowledge`,
`notes_search`, `slack_messenger`, `ask_ai`, `unsplash_search`, `lumbergh`,
`fleet`) mix list-shaped and detail-shaped subcommands under one file. Per
the brief's file-level `axi: true` marker, conversion happens at the
**file** level (one docstring, one `emit()` import), not per-subcommand —
but the **priority** and **expected savings** are still reported per
subcommand-group above, since that's what the shape column actually
predicts. A tool is not split into two catalog rows for the purpose of the
axi-ify/replace/leave decision; it is split only to make the priority
honest about which of its subcommands will actually benefit.
