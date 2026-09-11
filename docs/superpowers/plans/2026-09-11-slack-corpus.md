# Slack Corpus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local, cron-updated SQLite corpus of Jim's Slack that his agents query in natural language and get back readable conversation.

**Architecture:** Ingest logic lives in a `sherpa/corpus/` package; `tools/corpus.py` is a thin AXI-conformant CLI over it. One SQLite file holds messages, an FTS5 index, conversation chunks, and their embeddings in a `sqlite-vec` virtual table. Retrieval runs BM25 and vector KNN in parallel behind hard filters, fuses with RRF, then hydrates chunks back to full message transcripts.

**Tech Stack:** Python 3.13, SQLite (FTS5 + `sqlite-vec`), `httpx`, Slack Web API, `nomic-embed-text` via the LiteLLM gateway at `https://llm.jc.turbo.inc/v1`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-11-slack-corpus-design.md`

## Global Constraints

- **Python `>=3.13`** (repo `requires-python`). `tomllib` is stdlib — do not add a TOML dependency.
- **Every tool pins `python-toon==0.1.3`** in its PEP 723 block, alongside its own deps.
- **All output goes through `emit()`** from `sherpa.render`. Never `print(json.dumps(...))`, never `toon.encode` directly.
- **Every tool accepts `--json`**, emitting the same payload as indented JSON.
- **Exit codes (note these are inverted from sherpa's pre-AXI meanings):** `0` success including no-ops; `1` the world was uncooperative and retrying identically could work; `2` the caller must change flags or environment — bad flags, missing vault secret, rejected credentials, missing config.
- **Missing secret reports on both channels:** `MISSING_SECRET: <KEY>` on stderr *and* `fail(..., usage=True)` on stdout.
- **Flags are parsed via `parse_strict()`**, never `parser.parse_args()`. Abbreviations are rejected.
- **stderr is diagnostics only.** No progress output on stdout.
- **Any `help` key in emitted output is a list of strings**, never a bare string.
- **Every subcommand declares an `operations` entry** with a `tier` and `argv`, or `tests/test_manifest.py` fails.
- **Embedding dimension is 768** (`nomic-embed-text`). Referenced as `EMBED_DIM`.
- **Fail loudly.** No silent degradation anywhere: a dead embedding gateway or an expired token aborts with a `sherpa notify` push and a non-zero exit. A stale corpus warns on every query.
- **Test command:** `uv run --with pytest --with httpx --with sqlite-vec --with python-toon==0.1.3 pytest tests/<file> -v`

---

## File Structure

| File | Responsibility |
|---|---|
| `sherpa/corpus/__init__.py` | Package marker; re-exports `open_db`, `Config`. |
| `sherpa/corpus/schema.py` | DDL and migration. The only place table definitions live. |
| `sherpa/corpus/db.py` | Connection open, extension load, migration run, row helpers. |
| `sherpa/corpus/config.py` | `~/.sherpa/corpus.toml` parsing, vault token resolution. |
| `sherpa/corpus/slack.py` | `SlackClient` protocol, real HTTP impl, rate limiting, `FakeSlack` for tests. |
| `sherpa/corpus/directory.py` | Users and channels sync; ID→name resolution. |
| `sherpa/corpus/ingest.py` | Message backfill tiers, cursors, thread sweep, edits/deletes. |
| `sherpa/corpus/chunk.py` | Conversation windowing, script formatting, contextual headers. |
| `sherpa/corpus/embed.py` | Gateway client, batch embedding, `chunks_vec` writes. |
| `sherpa/corpus/search.py` | Filters, BM25, KNN, RRF, hydration, transcript assembly. |
| `sherpa/corpus/health.py` | Staleness and health checks shared by `status` and `search`. |
| `tools/corpus.py` | AXI CLI: `search`, `with`, `owed`, `thread`, `sync`, `status`. |
| `tests/test_corpus_db.py` | Schema, migration, FTS triggers. |
| `tests/test_corpus_config.py` | Config parsing, missing secret behavior. |
| `tests/test_corpus_ingest.py` | Cursors, tiers, thread sweep, idempotency, 429, edits/deletes. |
| `tests/test_corpus_chunk.py` | Gap boundaries, thread splitting, overlap, script format, headers. |
| `tests/test_corpus_embed.py` | Batching, hash skip, gateway failure aborts. |
| `tests/test_corpus_search.py` | Filters before fusion, RRF, hydration, dedupe, staleness warning. |
| `tests/fixtures/corpus/*.json` | Recorded Slack API shapes. |

**Phase 1 (Tasks 1–6)** ends with a working, keyword-searchable corpus that syncs on cron. **Phase 2 (Tasks 7–11)** adds chunking, embeddings, and hybrid retrieval.

---

## Task 1: Schema and database open

**Files:**
- Create: `sherpa/corpus/__init__.py`, `sherpa/corpus/schema.py`, `sherpa/corpus/db.py`
- Test: `tests/test_corpus_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sherpa.corpus.schema.SCHEMA_VERSION: int`
  - `sherpa.corpus.schema.MIGRATIONS: list[str]`
  - `sherpa.corpus.db.open_db(path: Path, *, vec: bool = True) -> sqlite3.Connection`
  - `sherpa.corpus.db.EMBED_DIM: int` (= 768)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_db.py
"""Schema creation, migration idempotency, and FTS trigger behavior.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_db.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import EMBED_DIM, open_db
from sherpa.corpus.schema import SCHEMA_VERSION


def test_open_db_creates_schema_and_stamps_version(tmp_path):
    conn = open_db(tmp_path / "c.db")
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    assert {"messages", "chunks", "channels", "users", "sync_state"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_open_db_is_idempotent(tmp_path):
    path = tmp_path / "c.db"
    open_db(path).close()
    conn = open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_message_insert_is_searchable_via_fts(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind,"
        " ts, author_id, author_name, is_me, text, permalink)"
        " VALUES ('w:c:1', 'w', 'C1', 'gravi-cli', 'public', 1.0, 'U1', 'Jim Vogel', 1,"
        " 'I will have the schema thursday', 'https://x')"
    )
    conn.commit()
    hits = conn.execute(
        "SELECT id FROM messages_fts WHERE messages_fts MATCH 'schema'"
    ).fetchall()
    assert [row[0] for row in hits] == ["w:c:1"]


def test_message_delete_removes_it_from_fts(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind,"
        " ts, author_id, author_name, is_me, text, permalink)"
        " VALUES ('w:c:1', 'w', 'C1', 'gravi-cli', 'public', 1.0, 'U1', 'Jim', 1,"
        " 'tombstone me', 'https://x')"
    )
    conn.execute("DELETE FROM messages WHERE id = 'w:c:1'")
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'tombstone'"
    ).fetchone()[0] == 0


def test_vec_table_accepts_embed_dim_vectors(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute(
        "INSERT INTO chunks (id, workspace, channel_id, start_ts, end_ts, participants,"
        " msg_first_id, msg_last_id, header, text, content_hash)"
        " VALUES (1, 'w', 'C1', 1.0, 2.0, 'Jim', 'w:c:1', 'w:c:2', 'hdr', 'body', 'h')"
    )
    conn.execute(
        "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (1, ?)",
        (bytes(4 * EMBED_DIM),),
    )
    conn.commit()
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sherpa.corpus'`

- [ ] **Step 3: Write the schema**

```python
# sherpa/corpus/__init__.py
"""Local Slack corpus: ingest, chunking, embedding, and hybrid retrieval."""

from sherpa.corpus.db import EMBED_DIM, open_db

__all__ = ["EMBED_DIM", "open_db"]
```

```python
# sherpa/corpus/schema.py
"""Table definitions for the corpus database.

Migrations are append-only: to change the schema, add a statement to the end of
MIGRATIONS and bump SCHEMA_VERSION. PRAGMA user_version records how many have
been applied, so a partially-migrated database picks up where it stopped.
"""

from __future__ import annotations

EMBED_DIM = 768

MIGRATIONS: list[str] = [
    """
    CREATE TABLE messages (
        id            TEXT PRIMARY KEY,   -- workspace:channel_id:ts
        workspace     TEXT NOT NULL,
        channel_id    TEXT NOT NULL,
        channel_name  TEXT NOT NULL,
        channel_kind  TEXT NOT NULL,      -- im | mpim | public | private
        ts            REAL NOT NULL,
        thread_ts     REAL,
        author_id     TEXT NOT NULL,
        author_name   TEXT NOT NULL,
        is_me         INTEGER NOT NULL DEFAULT 0,
        text          TEXT NOT NULL,
        permalink     TEXT NOT NULL,
        meta          TEXT NOT NULL DEFAULT '{}'
    );

    CREATE INDEX messages_channel_ts ON messages (workspace, channel_id, ts);
    CREATE INDEX messages_thread     ON messages (workspace, channel_id, thread_ts);
    CREATE INDEX messages_mine       ON messages (is_me, ts);
    CREATE INDEX messages_author     ON messages (author_name, ts);

    CREATE VIRTUAL TABLE messages_fts USING fts5 (
        id UNINDEXED, text, author_name, channel_name,
        content = 'messages', content_rowid = 'rowid'
    );

    CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts (rowid, id, text, author_name, channel_name)
        VALUES (new.rowid, new.id, new.text, new.author_name, new.channel_name);
    END;

    CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, id, text, author_name, channel_name)
        VALUES ('delete', old.rowid, old.id, old.text, old.author_name, old.channel_name);
    END;

    CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, id, text, author_name, channel_name)
        VALUES ('delete', old.rowid, old.id, old.text, old.author_name, old.channel_name);
        INSERT INTO messages_fts (rowid, id, text, author_name, channel_name)
        VALUES (new.rowid, new.id, new.text, new.author_name, new.channel_name);
    END;

    CREATE TABLE channels (
        workspace  TEXT NOT NULL,
        id         TEXT NOT NULL,
        name       TEXT NOT NULL,
        kind       TEXT NOT NULL,
        is_member  INTEGER NOT NULL DEFAULT 0,
        member_ids TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (workspace, id)
    );

    CREATE TABLE users (
        workspace    TEXT NOT NULL,
        id           TEXT NOT NULL,
        name         TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        is_me        INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace, id)
    );

    CREATE TABLE sync_state (
        workspace  TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        last_ts    REAL NOT NULL DEFAULT 0,
        tier       INTEGER NOT NULL DEFAULT 3,
        status     TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | failed
        detail     TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace, channel_id)
    );

    CREATE TABLE chunks (
        id           INTEGER PRIMARY KEY,
        workspace    TEXT NOT NULL,
        channel_id   TEXT NOT NULL,
        start_ts     REAL NOT NULL,
        end_ts       REAL NOT NULL,
        thread_ts    REAL,
        participants TEXT NOT NULL,
        msg_first_id TEXT NOT NULL,
        msg_last_id  TEXT NOT NULL,
        header       TEXT NOT NULL,
        text         TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        embedded     INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX chunks_channel  ON chunks (workspace, channel_id, start_ts);
    CREATE INDEX chunks_pending  ON chunks (embedded);
    CREATE UNIQUE INDEX chunks_range ON chunks (workspace, channel_id, msg_first_id, msg_last_id);

    CREATE VIRTUAL TABLE chunks_fts USING fts5 (
        chunk_id UNINDEXED, header, text, participants
    );

    CREATE TABLE sync_runs (
        id         INTEGER PRIMARY KEY,
        workspace  TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at   REAL,
        status     TEXT NOT NULL,   -- running | ok | failed
        detail     TEXT NOT NULL DEFAULT ''
    );
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)
```

Note the vector table is created in `db.py`, not here — it needs the loaded extension and an f-string for the dimension.

- [ ] **Step 4: Write the connection helper**

```python
# sherpa/corpus/db.py
"""Opening the corpus database: extension load, migration, pragmas."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from sherpa.corpus.schema import EMBED_DIM, MIGRATIONS, SCHEMA_VERSION

__all__ = ["EMBED_DIM", "SCHEMA_VERSION", "open_db"]


def open_db(path: Path, *, vec: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) the corpus database, migrated to SCHEMA_VERSION."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")

    if vec:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    _migrate(conn, vec=vec)
    return conn


def _migrate(conn: sqlite3.Connection, *, vec: bool) -> None:
    applied = conn.execute("PRAGMA user_version").fetchone()[0]
    for statement in MIGRATIONS[applied:]:
        conn.executescript(statement)
    if vec:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])"
        )
    if applied != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
```

- [ ] **Step 5: Run the tests**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_db.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add sherpa/corpus/__init__.py sherpa/corpus/schema.py sherpa/corpus/db.py tests/test_corpus_db.py
git commit -m "feat(corpus): schema and database open"
```

---

## Task 2: Config and token resolution

**Files:**
- Create: `sherpa/corpus/config.py`
- Test: `tests/test_corpus_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sherpa.corpus.config.WorkspaceConfig` — dataclass `(name: str, token_key: str, me: str | None)`
  - `sherpa.corpus.config.Config` — dataclass `(db: Path, workspaces: list[WorkspaceConfig], stale_after_hours: float)`
  - `sherpa.corpus.config.load_config(path: Path | None = None) -> Config`
  - `sherpa.corpus.config.resolve_token(workspace: WorkspaceConfig, vault_path: Path | None = None) -> str`
  - `sherpa.corpus.config.ConfigError` — exception carrying `.message` and `.help` for the CLI to hand to `fail()`
  - `sherpa.corpus.config.MissingSecret` — subclass of `ConfigError` carrying `.key`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_config.py
"""Config parsing and vault token resolution.

Run: uv run --with pytest pytest tests/test_corpus_config.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.config import ConfigError, MissingSecret, load_config, resolve_token

CONFIG = """
db = "~/.sherpa/corpus.db"
stale_after_hours = 24

[workspace.gravitate]
token_key = "SLACK_USER_TOKEN_GRAVITATE"
me = "U03ABC"

[workspace.side]
token_key = "SLACK_USER_TOKEN_SIDE"
"""


def test_load_config_reads_workspaces_in_order(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text(CONFIG)
    config = load_config(path)
    assert [w.name for w in config.workspaces] == ["gravitate", "side"]
    assert config.workspaces[0].token_key == "SLACK_USER_TOKEN_GRAVITATE"
    assert config.workspaces[0].me == "U03ABC"
    assert config.workspaces[1].me is None
    assert config.stale_after_hours == 24


def test_load_config_expands_db_path(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text(CONFIG)
    assert load_config(path).db == Path.home() / ".sherpa" / "corpus.db"


def test_missing_config_raises_with_actionable_help(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path / "absent.toml")
    assert "corpus.toml" in caught.value.message
    assert caught.value.help


def test_workspace_without_token_key_is_rejected(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text('db = "x.db"\n\n[workspace.broken]\nme = "U1"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_resolve_token_reads_the_vault(tmp_path):
    from sherpa.corpus.config import WorkspaceConfig

    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"SLACK_USER_TOKEN_GRAVITATE": "xoxp-real"}))
    workspace = WorkspaceConfig(name="gravitate", token_key="SLACK_USER_TOKEN_GRAVITATE", me=None)
    assert resolve_token(workspace, vault) == "xoxp-real"


def test_resolve_token_raises_missing_secret_naming_the_key(tmp_path):
    from sherpa.corpus.config import WorkspaceConfig

    vault = tmp_path / "vault.json"
    vault.write_text("{}")
    workspace = WorkspaceConfig(name="gravitate", token_key="SLACK_USER_TOKEN_GRAVITATE", me=None)
    with pytest.raises(MissingSecret) as caught:
        resolve_token(workspace, vault)
    assert caught.value.key == "SLACK_USER_TOKEN_GRAVITATE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest tests/test_corpus_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sherpa.corpus.config'`

- [ ] **Step 3: Write the implementation**

```python
# sherpa/corpus/config.py
"""Corpus configuration and per-workspace token resolution.

Tokens live in the sherpa vault under a per-workspace key, extending the
SLACK_USER_TOKEN convention slack_messenger already uses rather than adding a
second secrets path.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".sherpa" / "corpus.toml"
DEFAULT_VAULT_PATH = Path.home() / ".sherpa" / "vault.json"


class ConfigError(Exception):
    def __init__(self, message: str, help: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.help = help


class MissingSecret(ConfigError):
    def __init__(self, key: str) -> None:
        super().__init__(
            f"missing secret {key}",
            help=f"sherpa vault_manager set {key} <xoxp-token>",
        )
        self.key = key


@dataclass(frozen=True)
class WorkspaceConfig:
    name: str
    token_key: str
    me: str | None = None


@dataclass(frozen=True)
class Config:
    db: Path
    workspaces: list[WorkspaceConfig]
    stale_after_hours: float = 24.0


def load_config(path: Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    if not path.exists():
        raise ConfigError(
            f"no corpus.toml at {path}",
            help="sherpa corpus status  (after creating ~/.sherpa/corpus.toml)",
        )

    raw = tomllib.loads(path.read_text())
    workspaces = []
    for name, entry in raw.get("workspace", {}).items():
        token_key = entry.get("token_key")
        if not token_key:
            raise ConfigError(
                f"workspace '{name}' has no token_key",
                help=f"add token_key = \"SLACK_USER_TOKEN_{name.upper()}\" to {path}",
            )
        workspaces.append(WorkspaceConfig(name=name, token_key=token_key, me=entry.get("me")))

    if not workspaces:
        raise ConfigError(
            f"no workspaces configured in {path}",
            help=f"add a [workspace.<name>] section to {path}",
        )

    return Config(
        db=Path(raw.get("db", "~/.sherpa/corpus.db")).expanduser(),
        workspaces=workspaces,
        stale_after_hours=float(raw.get("stale_after_hours", 24.0)),
    )


def resolve_token(workspace: WorkspaceConfig, vault_path: Path | None = None) -> str:
    path = Path(vault_path or DEFAULT_VAULT_PATH).expanduser()
    vault = json.loads(path.read_text()) if path.exists() else {}
    token = vault.get(workspace.token_key)
    if not token:
        raise MissingSecret(workspace.token_key)
    return token
```

- [ ] **Step 4: Run the tests**

Run: `uv run --with pytest pytest tests/test_corpus_config.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add sherpa/corpus/config.py tests/test_corpus_config.py
git commit -m "feat(corpus): config and per-workspace token resolution"
```

---

## Task 3: Slack client with rate limiting and a test fake

**Files:**
- Create: `sherpa/corpus/slack.py`, `tests/fixtures/corpus/conversations_history.json`
- Test: `tests/test_corpus_slack.py`

**Interfaces:**
- Consumes: `sherpa.corpus.config.WorkspaceConfig`
- Produces:
  - `sherpa.corpus.slack.SlackError(Exception)` with `.code: str` and `.retryable: bool`
  - `sherpa.corpus.slack.AuthError(SlackError)` — raised on `invalid_auth`, `token_revoked`, `account_inactive`
  - `sherpa.corpus.slack.SlackClient` — protocol with `call(method: str, **params) -> dict` and `paginate(method: str, key: str, **params) -> Iterator[dict]`
  - `sherpa.corpus.slack.HttpSlackClient(token: str, *, sleep=time.sleep)` implementing it
  - `sherpa.corpus.slack.FakeSlack(pages: dict[str, list[dict]])` implementing it for tests

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_slack.py
"""Slack client: pagination, 429 backoff, auth error classification.

Run: uv run --with pytest --with httpx pytest tests/test_corpus_slack.py -v
No network — every response is stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.slack import AuthError, FakeSlack, HttpSlackClient, SlackError


class StubResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class StubTransport:
    """Returns queued responses in order, recording each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, headers=None, data=None):
        self.requests.append((url, data))
        return self.responses.pop(0)


def test_paginate_follows_the_cursor_and_stops_when_empty():
    transport = StubTransport([
        StubResponse({
            "ok": True,
            "messages": [{"ts": "1.0"}, {"ts": "2.0"}],
            "response_metadata": {"next_cursor": "abc"},
        }),
        StubResponse({"ok": True, "messages": [{"ts": "3.0"}], "response_metadata": {}}),
    ])
    client = HttpSlackClient("xoxp-x", transport=transport)
    got = list(client.paginate("conversations.history", "messages", channel="C1"))
    assert [m["ts"] for m in got] == ["1.0", "2.0", "3.0"]
    assert transport.requests[1][1]["cursor"] == "abc"


def test_rate_limit_sleeps_for_retry_after_then_retries():
    transport = StubTransport([
        StubResponse({"ok": False, "error": "ratelimited"}, status_code=429, headers={"Retry-After": "3"}),
        StubResponse({"ok": True, "messages": []}),
    ])
    slept = []
    client = HttpSlackClient("xoxp-x", transport=transport, sleep=slept.append)
    client.call("conversations.history", channel="C1")
    assert slept == [3.0]


def test_invalid_auth_raises_auth_error():
    transport = StubTransport([StubResponse({"ok": False, "error": "invalid_auth"})])
    client = HttpSlackClient("xoxp-x", transport=transport)
    with pytest.raises(AuthError) as caught:
        client.call("auth.test")
    assert caught.value.code == "invalid_auth"


def test_other_api_errors_raise_slack_error():
    transport = StubTransport([StubResponse({"ok": False, "error": "channel_not_found"})])
    client = HttpSlackClient("xoxp-x", transport=transport)
    with pytest.raises(SlackError) as caught:
        client.call("conversations.history", channel="C9")
    assert caught.value.code == "channel_not_found"
    assert not isinstance(caught.value, AuthError)


def test_fake_slack_paginates_its_queued_pages():
    fake = FakeSlack({"conversations.history": [
        {"ok": True, "messages": [{"ts": "1.0"}], "response_metadata": {"next_cursor": "n"}},
        {"ok": True, "messages": [{"ts": "2.0"}]},
    ]})
    assert [m["ts"] for m in fake.paginate("conversations.history", "messages", channel="C1")] == ["1.0", "2.0"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with httpx pytest tests/test_corpus_slack.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sherpa.corpus.slack'`

- [ ] **Step 3: Write the implementation**

```python
# sherpa/corpus/slack.py
"""Slack Web API access for the corpus ingester.

The client is deliberately synchronous and narrow: ingest is a cron job with no
latency budget, and a token-bucket over one connection is easier to keep inside
Slack's limits than concurrency would be.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, Protocol

import httpx

API = "https://slack.com/api/"
AUTH_ERRORS = {"invalid_auth", "token_revoked", "account_inactive", "not_authed"}
MAX_RETRIES = 5


class SlackError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AuthError(SlackError):
    """The token is bad. Retrying identically will never work."""


class SlackClient(Protocol):
    def call(self, method: str, **params: Any) -> dict: ...

    def paginate(self, method: str, key: str, **params: Any) -> Iterator[dict]: ...


class HttpSlackClient:
    def __init__(self, token: str, *, transport: Any = None, sleep=time.sleep) -> None:
        self._token = token
        self._transport = transport or httpx.Client(timeout=30.0)
        self._sleep = sleep

    def call(self, method: str, **params: Any) -> dict:
        payload = {k: v for k, v in params.items() if v is not None}
        for attempt in range(MAX_RETRIES):
            response = self._transport.post(
                API + method,
                headers={"Authorization": f"Bearer {self._token}"},
                data=payload,
            )
            if response.status_code == 429:
                self._sleep(float(response.headers.get("Retry-After", "1")))
                continue
            body = response.json()
            if body.get("ok"):
                return body
            code = body.get("error", "unknown")
            if code in AUTH_ERRORS:
                raise AuthError(code)
            if code == "ratelimited":
                self._sleep(2.0 * (attempt + 1))
                continue
            raise SlackError(code)
        raise SlackError("ratelimited", retryable=True)

    def paginate(self, method: str, key: str, **params: Any) -> Iterator[dict]:
        cursor = None
        while True:
            body = self.call(method, cursor=cursor, limit=200, **params)
            yield from body.get(key, [])
            cursor = body.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                return


class FakeSlack:
    """Replays queued response bodies per method. Records every call made."""

    def __init__(self, pages: dict[str, list[dict]]) -> None:
        self._pages = {method: list(bodies) for method, bodies in pages.items()}
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, **params: Any) -> dict:
        self.calls.append((method, params))
        queue = self._pages.get(method)
        if not queue:
            raise SlackError("no_fake_response_queued")
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        if not body.get("ok", True):
            code = body.get("error", "unknown")
            raise AuthError(code) if code in AUTH_ERRORS else SlackError(code)
        return body

    def paginate(self, method: str, key: str, **params: Any) -> Iterator[dict]:
        cursor = None
        while True:
            body = self.call(method, cursor=cursor, **params)
            yield from body.get(key, [])
            cursor = body.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                return
```

- [ ] **Step 4: Run the tests**

Run: `uv run --with pytest --with httpx pytest tests/test_corpus_slack.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add sherpa/corpus/slack.py tests/test_corpus_slack.py
git commit -m "feat(corpus): slack client with backoff and a test fake"
```

---

## Task 4: Directory sync — users and channels

**Files:**
- Create: `sherpa/corpus/directory.py`
- Test: `tests/test_corpus_directory.py`

**Interfaces:**
- Consumes: `open_db`, `SlackClient`, `WorkspaceConfig`
- Produces:
  - `sherpa.corpus.directory.sync_directory(conn, client: SlackClient, workspace: str, me: str | None) -> str` — upserts `users` and `channels`, returns the resolved `me` user ID (from `auth.test` when config omits it)
  - `sherpa.corpus.directory.Resolver(conn, workspace: str)` with `user_name(user_id: str) -> str`, `channel(channel_id: str) -> tuple[str, str]` returning `(name, kind)`, and `me_id: str`
  - `sherpa.corpus.directory.tier_for(channel_row, has_posted: bool) -> int` — `1` for im/mpim, `2` for posted-in channels, `3` otherwise

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_directory.py
"""Users and channels sync, name resolution, and backfill tier assignment.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_directory.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import open_db
from sherpa.corpus.directory import Resolver, sync_directory, tier_for
from sherpa.corpus.slack import FakeSlack


def make_client():
    return FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
            {"id": "U_H", "name": "hunter", "profile": {"real_name": "Hunter Reeves"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "C1", "name": "gravi-cli", "is_channel": True, "is_private": False, "is_member": True},
            {"id": "D1", "is_im": True, "user": "U_H"},
        ]}],
    })


def test_sync_directory_stores_users_with_real_names(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_client(), "gravitate", me=None)
    names = dict(conn.execute("SELECT id, name FROM users WHERE workspace='gravitate'"))
    assert names["U_H"] == "Hunter Reeves"


def test_sync_directory_returns_and_flags_me(tmp_path):
    conn = open_db(tmp_path / "c.db")
    assert sync_directory(conn, make_client(), "gravitate", me=None) == "U_ME"
    assert conn.execute("SELECT is_me FROM users WHERE id='U_ME'").fetchone()[0] == 1


def test_dm_channel_is_named_for_the_other_person(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_client(), "gravitate", me=None)
    resolver = Resolver(conn, "gravitate")
    assert resolver.channel("D1") == ("Hunter Reeves", "im")
    assert resolver.channel("C1") == ("gravi-cli", "public")


def test_resolver_falls_back_to_the_raw_id_for_unknown_users(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_client(), "gravitate", me=None)
    assert Resolver(conn, "gravitate").user_name("U_GHOST") == "U_GHOST"


def test_tier_for_ranks_dms_then_posted_then_lurked():
    assert tier_for({"kind": "im"}, has_posted=False) == 1
    assert tier_for({"kind": "mpim"}, has_posted=False) == 1
    assert tier_for({"kind": "public"}, has_posted=True) == 2
    assert tier_for({"kind": "public"}, has_posted=False) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_directory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sherpa.corpus.directory'`

- [ ] **Step 3: Write the implementation**

```python
# sherpa/corpus/directory.py
"""Users and channels: fetch, store, and resolve IDs to human names.

Resolution happens at ingest time so the corpus holds "Hunter Reeves", not
U03K2. An agent searching for a person must not need to know their Slack ID.
"""

from __future__ import annotations

import json
import sqlite3

from sherpa.corpus.slack import SlackClient


def sync_directory(conn: sqlite3.Connection, client: SlackClient, workspace: str, me: str | None) -> str:
    me_id = me or client.call("auth.test")["user_id"]

    for member in client.paginate("users.list", "members"):
        profile = member.get("profile", {})
        conn.execute(
            "INSERT INTO users (workspace, id, name, display_name, is_me)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (workspace, id) DO UPDATE SET"
            " name = excluded.name, display_name = excluded.display_name, is_me = excluded.is_me",
            (
                workspace,
                member["id"],
                profile.get("real_name") or member.get("name") or member["id"],
                profile.get("display_name", ""),
                1 if member["id"] == me_id else 0,
            ),
        )

    for channel in client.paginate(
        "conversations.list",
        "channels",
        types="public_channel,private_channel,mpim,im",
        exclude_archived=True,
    ):
        kind = _kind(channel)
        conn.execute(
            "INSERT INTO channels (workspace, id, name, kind, is_member, member_ids)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (workspace, id) DO UPDATE SET"
            " name = excluded.name, kind = excluded.kind, is_member = excluded.is_member",
            (
                workspace,
                channel["id"],
                _name(channel, kind),
                kind,
                1 if channel.get("is_member") or kind in ("im", "mpim") else 0,
                json.dumps(channel.get("members", [])),
            ),
        )

    conn.commit()
    return me_id


def _kind(channel: dict) -> str:
    if channel.get("is_im"):
        return "im"
    if channel.get("is_mpim"):
        return "mpim"
    return "private" if channel.get("is_private") else "public"


def _name(channel: dict, kind: str) -> str:
    if kind == "im":
        return channel.get("user", channel["id"])
    return channel.get("name", channel["id"])


def tier_for(channel_row, has_posted: bool) -> int:
    """Backfill priority: DMs first, then channels Jim has spoken in, then the rest."""
    if channel_row["kind"] in ("im", "mpim"):
        return 1
    return 2 if has_posted else 3


class Resolver:
    """ID to human name, cached per workspace for the life of one sync run."""

    def __init__(self, conn: sqlite3.Connection, workspace: str) -> None:
        self._users = {
            row["id"]: row["name"]
            for row in conn.execute("SELECT id, name FROM users WHERE workspace = ?", (workspace,))
        }
        self._channels = {
            row["id"]: (row["name"], row["kind"])
            for row in conn.execute(
                "SELECT id, name, kind FROM channels WHERE workspace = ?", (workspace,)
            )
        }
        me = conn.execute(
            "SELECT id FROM users WHERE workspace = ? AND is_me = 1", (workspace,)
        ).fetchone()
        self.me_id = me["id"] if me else ""

    def user_name(self, user_id: str) -> str:
        return self._users.get(user_id, user_id)

    def channel(self, channel_id: str) -> tuple[str, str]:
        name, kind = self._channels.get(channel_id, (channel_id, "public"))
        return (self.user_name(name), kind) if kind == "im" else (name, kind)
```

- [ ] **Step 4: Run the tests**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_directory.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add sherpa/corpus/directory.py tests/test_corpus_directory.py
git commit -m "feat(corpus): users and channels sync with name resolution"
```

---

## Task 5: Message ingest — cursors, tiers, thread sweep

**Files:**
- Create: `sherpa/corpus/ingest.py`
- Test: `tests/test_corpus_ingest.py`

**Interfaces:**
- Consumes: `open_db`, `SlackClient`, `Resolver`, `sync_directory`, `tier_for`, `AuthError`
- Produces:
  - `sherpa.corpus.ingest.IngestReport` — dataclass `(workspace: str, messages: int, channels_done: int, channels_failed: list[tuple[str, str]])`
  - `sherpa.corpus.ingest.ingest_workspace(conn, client, workspace: str, me: str | None, *, budget_seconds: float = 900, now=time.time) -> IngestReport`
  - `sherpa.corpus.ingest.message_id(workspace: str, channel_id: str, ts: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_ingest.py
"""Message ingest: cursors, tier order, thread sweep, idempotency, deletions.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_ingest.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import open_db
from sherpa.corpus.ingest import ingest_workspace, message_id
from sherpa.corpus.slack import AuthError, FakeSlack, SlackError

DIRECTORY = {
    "auth.test": [{"ok": True, "user_id": "U_ME"}],
    "users.list": [{"ok": True, "members": [
        {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
        {"id": "U_H", "name": "hunter", "profile": {"real_name": "Hunter Reeves"}},
    ]}],
    "conversations.list": [{"ok": True, "channels": [
        {"id": "D1", "is_im": True, "user": "U_H"},
    ]}],
}


def client_with(history, replies=None):
    pages = dict(DIRECTORY)
    pages["conversations.history"] = history
    pages["conversations.replies"] = replies or [{"ok": True, "messages": []}]
    return FakeSlack(pages)


def test_ingest_stores_messages_with_resolved_names_and_is_me(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "100.0", "user": "U_ME", "text": "I'll have the schema thursday"},
        {"ts": "99.0", "user": "U_H", "text": "can you take the cli work?"},
    ]}])
    report = ingest_workspace(conn, client, "gravitate", me=None)
    assert report.messages == 2
    rows = dict(conn.execute("SELECT author_name, is_me FROM messages"))
    assert rows == {"Jim Vogel": 1, "Hunter Reeves": 0}


def test_ingest_advances_the_cursor_to_the_newest_ts(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "100.0", "user": "U_H", "text": "a"},
        {"ts": "99.0", "user": "U_H", "text": "b"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute(
        "SELECT last_ts FROM sync_state WHERE channel_id='D1'"
    ).fetchone()[0] == 100.0


def test_second_run_requests_only_messages_after_the_cursor(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U_H", "text": "a"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None)
    history_calls = [params for method, params in client.calls if method == "conversations.history"]
    assert history_calls and history_calls[0]["oldest"] == "100.0"


def test_reingesting_the_same_message_does_not_duplicate_it(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U_H", "text": "a"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    conn.execute("UPDATE sync_state SET last_ts = 0")
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_edited_message_text_is_updated_in_place(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U_H", "text": "before"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    conn.execute("UPDATE sync_state SET last_ts = 0")
    client._pages["conversations.history"] = [
        {"ok": True, "messages": [{"ts": "100.0", "user": "U_H", "text": "after", "edited": {"ts": "101.0"}}]}
    ]
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT text FROM messages").fetchone()[0] == "after"
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'after'"
    ).fetchone()[0] == 1


def test_tombstone_messages_are_not_stored(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "100.0", "user": "U_H", "text": "real"},
        {"ts": "101.0", "user": "U_H", "text": "This message was deleted.", "subtype": "tombstone"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_thread_sweep_pulls_replies_for_parents_with_reply_count(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0"},
            {"ts": "100.5", "user": "U_ME", "text": "on it", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None)
    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert texts == {"parent", "on it"}


def test_auth_error_propagates_rather_than_being_swallowed(tmp_path):
    conn = open_db(tmp_path / "c.db")
    pages = dict(DIRECTORY)
    pages["conversations.history"] = [{"ok": False, "error": "token_revoked"}]
    with pytest.raises(AuthError):
        ingest_workspace(conn, FakeSlack(pages), "gravitate", me=None)


def test_one_failing_channel_is_recorded_and_others_continue(tmp_path):
    conn = open_db(tmp_path / "c.db")
    pages = dict(DIRECTORY)
    pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": "D1", "is_im": True, "user": "U_H"},
        {"id": "C2", "name": "other", "is_channel": True, "is_member": True},
    ]}]
    calls = {"n": 0}
    client = FakeSlack(pages)
    original = client.call

    def flaky(method, **params):
        if method == "conversations.history" and params.get("channel") == "D1":
            raise SlackError("channel_not_found")
        if method == "conversations.history":
            return {"ok": True, "messages": [{"ts": "5.0", "user": "U_H", "text": "hi"}]}
        return original(method, **params)

    client.call = flaky
    report = ingest_workspace(conn, client, "gravitate", me=None)
    assert [c for c, _ in report.channels_failed] == ["D1"]
    assert report.messages == 1


def test_message_id_is_stable_and_namespaced():
    assert message_id("gravitate", "D1", "100.0") == "gravitate:D1:100.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sherpa.corpus.ingest'`

- [ ] **Step 3: Write the implementation**

```python
# sherpa/corpus/ingest.py
"""Message ingest: tiered backfill, per-channel cursors, thread sweep.

Slack's channel cursor does not advance when a reply lands on an old thread
parent, so replies need their own sweep or they are lost forever — the single
most likely way for this corpus to go quietly incomplete.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from sherpa.corpus.directory import Resolver, sync_directory, tier_for
from sherpa.corpus.slack import AuthError, SlackClient, SlackError

SKIP_SUBTYPES = {"tombstone", "channel_join", "channel_leave", "message_deleted"}


@dataclass
class IngestReport:
    workspace: str
    messages: int = 0
    channels_done: int = 0
    channels_failed: list[tuple[str, str]] = field(default_factory=list)


def message_id(workspace: str, channel_id: str, ts: str) -> str:
    return f"{workspace}:{channel_id}:{ts}"


def ingest_workspace(
    conn: sqlite3.Connection,
    client: SlackClient,
    workspace: str,
    me: str | None,
    *,
    budget_seconds: float = 900.0,
    now=time.time,
) -> IngestReport:
    sync_directory(conn, client, workspace, me)
    resolver = Resolver(conn, workspace)
    report = IngestReport(workspace=workspace)
    deadline = now() + budget_seconds

    for channel in _channels_in_tier_order(conn, workspace):
        if now() >= deadline:
            break
        try:
            report.messages += _ingest_channel(conn, client, resolver, workspace, channel)
            report.channels_done += 1
            _mark(conn, workspace, channel["id"], "ok", "")
        except AuthError:
            raise
        except SlackError as error:
            report.channels_failed.append((channel["id"], error.code))
            _mark(conn, workspace, channel["id"], "failed", error.code)

    conn.commit()
    return report


def _channels_in_tier_order(conn: sqlite3.Connection, workspace: str) -> list[sqlite3.Row]:
    posted_in = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT channel_id FROM messages WHERE workspace = ? AND is_me = 1",
            (workspace,),
        )
    }
    channels = conn.execute(
        "SELECT * FROM channels WHERE workspace = ? AND is_member = 1", (workspace,)
    ).fetchall()
    for channel in channels:
        conn.execute(
            "INSERT INTO sync_state (workspace, channel_id, tier) VALUES (?, ?, ?)"
            " ON CONFLICT (workspace, channel_id) DO UPDATE SET tier = excluded.tier",
            (workspace, channel["id"], tier_for(channel, channel["id"] in posted_in)),
        )
    conn.commit()
    return sorted(channels, key=lambda c: tier_for(c, c["id"] in posted_in))


def _ingest_channel(conn, client, resolver, workspace: str, channel) -> int:
    channel_id = channel["id"]
    state = conn.execute(
        "SELECT last_ts FROM sync_state WHERE workspace = ? AND channel_id = ?",
        (workspace, channel_id),
    ).fetchone()
    oldest = state["last_ts"] if state else 0.0

    stored = 0
    newest = oldest
    thread_parents: list[str] = []

    for raw in client.paginate(
        "conversations.history",
        "messages",
        channel=channel_id,
        oldest=f"{oldest}" if oldest else None,
        inclusive=False,
    ):
        newest = max(newest, float(raw["ts"]))
        if raw.get("reply_count"):
            thread_parents.append(raw.get("thread_ts", raw["ts"]))
        stored += _store(conn, resolver, workspace, channel, raw)

    for parent_ts in thread_parents:
        for raw in client.paginate(
            "conversations.replies", "messages", channel=channel_id, ts=parent_ts
        ):
            newest = max(newest, float(raw["ts"]))
            stored += _store(conn, resolver, workspace, channel, raw)

    conn.execute(
        "UPDATE sync_state SET last_ts = ?, updated_at = ? WHERE workspace = ? AND channel_id = ?",
        (newest, time.time(), workspace, channel_id),
    )
    conn.commit()
    return stored


def _store(conn, resolver: Resolver, workspace: str, channel, raw: dict) -> int:
    if raw.get("subtype") in SKIP_SUBTYPES or not raw.get("text"):
        return 0

    author_id = raw.get("user") or raw.get("bot_id", "")
    channel_name, kind = resolver.channel(channel["id"])
    identifier = message_id(workspace, channel["id"], raw["ts"])
    meta = {
        key: raw[key]
        for key in ("reactions", "files", "edited", "subtype", "reply_count")
        if key in raw
    }

    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " thread_ts, author_id, author_name, is_me, text, permalink, meta)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (id) DO UPDATE SET text = excluded.text, meta = excluded.meta",
        (
            identifier,
            workspace,
            channel["id"],
            channel_name,
            kind,
            float(raw["ts"]),
            float(raw["thread_ts"]) if raw.get("thread_ts") else None,
            author_id,
            resolver.user_name(author_id),
            1 if author_id == resolver.me_id else 0,
            raw["text"],
            _permalink(workspace, channel["id"], raw["ts"]),
            json.dumps(meta),
        ),
    )
    return 1


def _permalink(workspace: str, channel_id: str, ts: str) -> str:
    return f"https://{workspace}.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def _mark(conn, workspace: str, channel_id: str, status: str, detail: str) -> None:
    conn.execute(
        "UPDATE sync_state SET status = ?, detail = ?, updated_at = ?"
        " WHERE workspace = ? AND channel_id = ?",
        (status, detail, time.time(), workspace, channel_id),
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_ingest.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add sherpa/corpus/ingest.py tests/test_corpus_ingest.py
git commit -m "feat(corpus): tiered message ingest with cursors and thread sweep"
```

---

## Task 6: CLI skeleton — `sync` and `status`

**Files:**
- Create: `tools/corpus.py`, `sherpa/corpus/health.py`
- Test: `tests/test_corpus_cli.py`

**Interfaces:**
- Consumes: `load_config`, `resolve_token`, `open_db`, `ingest_workspace`, `HttpSlackClient`, `ConfigError`, `MissingSecret`, `AuthError`
- Produces:
  - `sherpa.corpus.health.Health` — dataclass `(workspace: str, last_sync: float, stale: bool, unembedded: int, failed_channels: int)`
  - `sherpa.corpus.health.check(conn, config) -> list[Health]`
  - `sherpa.corpus.health.is_healthy(reports: list[Health]) -> bool`
  - `tools/corpus.py` subcommands `sync` and `status`

After this task the corpus syncs on cron and is keyword-searchable via SQL. Phase 1 is complete.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_cli.py
"""CLI contract: exit codes, TOON/JSON output, loud failures.

Run: uv run --with pytest --with httpx --with sqlite-vec --with python-toon==0.1.3 \
     pytest tests/test_corpus_cli.py -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_SPEC = importlib.util.spec_from_file_location("corpus_tool", ROOT / "tools" / "corpus.py")
corpus_tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus_tool)

from sherpa.corpus.db import open_db


@pytest.fixture
def workspace(tmp_path):
    config = tmp_path / "corpus.toml"
    config.write_text(
        f'db = "{tmp_path / "c.db"}"\n\n[workspace.gravitate]\n'
        'token_key = "SLACK_USER_TOKEN_GRAVITATE"\n'
    )
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"SLACK_USER_TOKEN_GRAVITATE": "xoxp-x"}))
    return config, vault


def test_unknown_flag_exits_2(workspace, capsys):
    config, _ = workspace
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["status", "--nonsense", "--config", str(config)])
    assert caught.value.code == 2
    assert "nonsense" in capsys.readouterr().out


def test_missing_config_exits_2_with_help(tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["status", "--config", str(tmp_path / "absent.toml")])
    assert caught.value.code == 2
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "help:" in out


def test_missing_secret_reports_on_stderr_and_exits_2(workspace, tmp_path, capsys):
    config, _ = workspace
    empty_vault = tmp_path / "empty.json"
    empty_vault.write_text("{}")
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(empty_vault)])
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "MISSING_SECRET: SLACK_USER_TOKEN_GRAVITATE" in captured.err
    assert captured.out.startswith("error:")


def test_status_emits_json_when_asked(workspace, capsys):
    config, _ = workspace
    open_db(Path(json.loads('"' + str(config.parent / "c.db") + '"'))).close()
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["status", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "workspaces" in payload
    assert caught.value.code == 2  # never synced is not healthy


def test_operations_declares_every_subcommand():
    import yaml

    meta = yaml.safe_load(corpus_tool.__doc__)
    assert meta["name"] == "corpus"
    assert meta["axi"] is True
    declared = set(meta["operations"])
    assert {"search", "with", "owed", "thread", "sync", "status"} == declared
    assert meta["operations"]["sync"]["tier"] == "write"
    assert meta["operations"]["search"]["tier"] == "read"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with httpx --with sqlite-vec --with python-toon==0.1.3 --with pyyaml pytest tests/test_corpus_cli.py -v`
Expected: FAIL — `FileNotFoundError` for `tools/corpus.py`

- [ ] **Step 3: Write the health module**

```python
# sherpa/corpus/health.py
"""Corpus health: staleness and embedding backlog.

Shared by `status` and by `search`, because a query answered from a stale corpus
is worse than a query that fails — the agent reports that Jim promised nobody
anything and nothing indicates the corpus simply stopped three weeks ago.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from sherpa.corpus.config import Config


@dataclass
class Health:
    workspace: str
    last_sync: float
    stale: bool
    unembedded: int
    failed_channels: int


def check(conn: sqlite3.Connection, config: Config, *, now=time.time) -> list[Health]:
    reports = []
    for workspace in config.workspaces:
        row = conn.execute(
            "SELECT max(updated_at) AS last, sum(status = 'failed') AS failed"
            " FROM sync_state WHERE workspace = ?",
            (workspace.name,),
        ).fetchone()
        last = row["last"] or 0.0
        unembedded = conn.execute(
            "SELECT count(*) FROM chunks WHERE workspace = ? AND embedded = 0",
            (workspace.name,),
        ).fetchone()[0]
        reports.append(
            Health(
                workspace=workspace.name,
                last_sync=last,
                stale=(now() - last) > config.stale_after_hours * 3600,
                unembedded=unembedded,
                failed_channels=row["failed"] or 0,
            )
        )
    return reports


def is_healthy(reports: list[Health]) -> bool:
    return all(
        not r.stale and r.unembedded == 0 and r.failed_channels == 0 for r in reports
    )


def warnings(reports: list[Health]) -> list[str]:
    lines = []
    for report in reports:
        if report.last_sync == 0:
            lines.append(f"{report.workspace}: never synced — results are empty or partial")
        elif report.stale:
            age = (time.time() - report.last_sync) / 3600
            lines.append(f"{report.workspace}: last synced {age:.0f}h ago — results may be incomplete")
        if report.unembedded:
            lines.append(f"{report.workspace}: {report.unembedded} chunks unembedded — semantic recall is degraded")
        if report.failed_channels:
            lines.append(f"{report.workspace}: {report.failed_channels} channels failed to sync")
    return lines
```

- [ ] **Step 4: Write the CLI with `sync` and `status`**

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["python-toon==0.1.3", "httpx", "sqlite-vec"]
# ///
"""
name: corpus
description: Search an indexed local corpus of your Slack — what you promised, what you missed.
categories: [slack, search, memory, knowledge]
axi: true
secrets: [SLACK_USER_TOKEN_GRAVITATE]
usage: |
  search <QUERY> [--from NAME] [--mine] [--since 30d] [--k 5] [--context 3]
  with <NAME> [--since 30d]
  owed <NAME>
  thread <PERMALINK>
  sync [--workspace NAME] [--budget 900]
  status
operations:
  search:
    tier: read
    argv: ["search", "{query}"]
    optional:
      from_person: "--from"
      mine: "--mine"
      since: "--since"
      workspace: "--workspace"
      k: "--k"
      context: "--context"
  with:
    tier: read
    argv: ["with", "{person}"]
    optional:
      since: "--since"
  owed:
    tier: read
    argv: ["owed", "{person}"]
  thread:
    tier: read
    argv: ["thread", "{permalink}"]
  sync:
    tier: write
    argv: ["sync"]
    optional:
      workspace: "--workspace"
      budget: "--budget"
    notes: "Hits the Slack API and writes to the local corpus. Safe to re-run."
  status:
    tier: read
    argv: ["status"]
risk: low
outputBudget: 4000
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate

from sherpa.corpus.config import ConfigError, MissingSecret, load_config, resolve_token
from sherpa.corpus.db import open_db
from sherpa.corpus.health import check, is_healthy, warnings
from sherpa.corpus.ingest import ingest_workspace
from sherpa.corpus.slack import AuthError, HttpSlackClient


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    parser = argparse.ArgumentParser(prog="corpus", add_help=True, allow_abbrev=False)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--config", default=None)
    parser.add_argument("--vault", default=None)
    subparsers = parser.add_subparsers(dest="command")

    registry = {}
    sync = subparsers.add_parser("sync", allow_abbrev=False)
    sync.add_argument("--workspace", default=None)
    sync.add_argument("--budget", type=float, default=900.0)
    registry["sync"] = sync

    status = subparsers.add_parser("status", allow_abbrev=False)
    registry["status"] = status

    return parser, registry


def notify(title: str, message: str) -> None:
    """Push loudly. A corpus that stops updating must not do so quietly."""
    try:
        subprocess.run(
            ["sherpa", "notify", "send", message, "--title", title, "--priority", "high"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass  # the non-zero exit and stdout error still carry the failure


def cmd_sync(args, config) -> int:
    conn = open_db(config.db)
    selected = [w for w in config.workspaces if args.workspace in (None, w.name)]
    if not selected:
        fail(
            f"no workspace named {args.workspace}",
            help="sherpa corpus status",
            usage=True,
        )

    results = []
    for workspace in selected:
        try:
            token = resolve_token(workspace, args.vault)
        except MissingSecret as missing:
            print(f"MISSING_SECRET: {missing.key}", file=sys.stderr)
            notify("Slack corpus: token missing", f"{workspace.name}: set {missing.key}")
            fail(missing.message, help=missing.help, usage=True)

        client = HttpSlackClient(token)
        try:
            report = ingest_workspace(
                conn, client, workspace.name, workspace.me, budget_seconds=args.budget
            )
        except AuthError as error:
            notify(
                "Slack corpus: auth failed",
                f"{workspace.name}: {error.code} — refresh {workspace.token_key}",
            )
            fail(
                f"{workspace.name}: slack rejected the token ({error.code})",
                help=f"sherpa vault_manager set {workspace.token_key} <xoxp-token>",
                usage=True,
            )
        results.append(
            {
                "workspace": report.workspace,
                "messages": report.messages,
                "channels": report.channels_done,
                "failed": len(report.channels_failed),
            }
        )

    failed = [r for r in results if r["failed"]]
    payload = {"synced": results, "count": len(results)}
    if failed:
        payload["help"] = ["sherpa corpus status"]
    emit(payload, as_json=args.json)
    return 1 if failed else 0


def cmd_status(args, config) -> int:
    conn = open_db(config.db)
    reports = check(conn, config)
    messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]

    payload = {
        "db": str(config.db),
        "messages": messages,
        "chunks": chunks,
        "workspaces": [
            {
                "workspace": r.workspace,
                "last_sync": round(r.last_sync, 0),
                "stale": r.stale,
                "unembedded": r.unembedded,
                "failed": r.failed_channels,
            }
            for r in reports
        ],
    }
    if lines := warnings(reports):
        payload["warnings"] = lines
        payload["help"] = ["sherpa corpus sync"]
    emit(payload, as_json=args.json)
    return 0 if is_healthy(reports) else 2


def home_view(config_path) -> int:
    print(bin_line(Path(__file__).resolve()))
    print("description: Search an indexed local corpus of your Slack.")
    print("hint: sherpa corpus status")
    print("hint: sherpa corpus search <QUERY>")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser, registry = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.exit(home_view(None))

    args = parse_strict(parser, registry, argv)
    if not args.command:
        sys.exit(home_view(None))

    try:
        config = load_config(args.config)
    except ConfigError as error:
        fail(error.message, help=error.help, usage=True)

    handlers = {"sync": cmd_sync, "status": cmd_status}
    sys.exit(handlers[args.command](args, config))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests**

Run: `uv run --with pytest --with httpx --with sqlite-vec --with python-toon==0.1.3 --with pyyaml pytest tests/test_corpus_cli.py -v`
Expected: 5 passed

- [ ] **Step 6: Run the manifest test to confirm the operations block is well-formed**

Run: `uv run --with pytest --with pyyaml pytest tests/test_manifest.py -v`
Expected: PASS — `corpus` is declared, not listed under `undeclared`

Note: the docstring declares all six subcommands from the start, because `operations` describes the tool's contract and `test_manifest.py` checks it. Only `sync` and `status` are implemented in this task. Add the other four to `build_parser()` now as stubs so the parser and the docstring agree — Task 10 replaces them:

```python
    for name, positional in (("search", "query"), ("with", "person"),
                             ("owed", "person"), ("thread", "permalink")):
        stub = subparsers.add_parser(name, allow_abbrev=False)
        stub.add_argument(positional)
        registry[name] = stub
```

and give each a handler that fails cleanly until Task 10 lands:

```python
def cmd_unimplemented(args, config) -> int:
    fail(
        f"'{args.command}' is not implemented yet",
        help="sherpa corpus status",
    )
```

Register all four to `cmd_unimplemented` in the `handlers` dict.

- [ ] **Step 7: Commit**

```bash
git add tools/corpus.py sherpa/corpus/health.py tests/test_corpus_cli.py
git commit -m "feat(corpus): sync and status CLI with loud failure on auth and config"
```

---

## Task 7: Conversation chunking

**Files:**
- Create: `sherpa/corpus/chunk.py`
- Test: `tests/test_corpus_chunk.py`

**Interfaces:**
- Consumes: `open_db`
- Produces:
  - `sherpa.corpus.chunk.GAP_SECONDS: int` (= 1800), `MAX_MESSAGES: int` (= 40), `OVERLAP: int` (= 2), `MAX_CHARS: int` (= 6000)
  - `sherpa.corpus.chunk.build_chunks(conn, workspace: str, channel_id: str) -> int` — rebuilds chunks for one channel, returns count written
  - `sherpa.corpus.chunk.rechunk_all(conn, workspace: str) -> int`
  - `sherpa.corpus.chunk.format_script(messages: list) -> str`
  - `sherpa.corpus.chunk.build_header(channel_name: str, kind: str, participants: list[str], start_ts: float) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_chunk.py
"""Conversation windowing: gaps, thread isolation, overlap, script format, headers.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_chunk.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.chunk import (
    GAP_SECONDS,
    MAX_MESSAGES,
    build_chunks,
    build_header,
    format_script,
)
from sherpa.corpus.db import open_db


def insert(conn, ts, author, text, *, thread_ts=None, channel="C1", kind="public", name="gravi-cli"):
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " thread_ts, author_id, author_name, is_me, text, permalink)"
        " VALUES (?, 'w', ?, ?, ?, ?, ?, 'U', ?, 0, ?, 'https://x')",
        (f"w:{channel}:{ts}", channel, name, kind, ts, thread_ts, author, text),
    )


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "c.db")


def test_messages_within_the_gap_land_in_one_chunk(conn):
    for i in range(5):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    assert build_chunks(conn, "w", "C1") == 1


def test_a_silence_longer_than_the_gap_starts_a_new_chunk(conn):
    insert(conn, 1000.0, "Jim Vogel", "before")
    insert(conn, 1000.0 + GAP_SECONDS + 1, "Jim Vogel", "after")
    conn.commit()
    assert build_chunks(conn, "w", "C1") == 2


def test_a_long_run_splits_at_the_message_cap(conn):
    for i in range(MAX_MESSAGES + 5):
        insert(conn, 1000.0 + i, "Jim Vogel", f"m{i}")
    conn.commit()
    assert build_chunks(conn, "w", "C1") == 2


def test_adjacent_chunks_overlap_so_boundary_commitments_appear_twice(conn):
    insert(conn, 1000.0, "Jim Vogel", "unique_early")
    insert(conn, 1000.0 + 60, "Jim Vogel", "boundary_promise")
    insert(conn, 1000.0 + GAP_SECONDS + 61, "Jim Vogel", "unique_late")
    conn.commit()
    build_chunks(conn, "w", "C1")
    texts = [row[0] for row in conn.execute("SELECT text FROM chunks ORDER BY start_ts")]
    assert sum("boundary_promise" in t for t in texts) == 2


def test_a_thread_becomes_its_own_chunk(conn):
    insert(conn, 1000.0, "Jim Vogel", "channel chatter")
    insert(conn, 1001.0, "Hunter Reeves", "thread parent", thread_ts=1001.0)
    insert(conn, 1002.0, "Jim Vogel", "thread reply", thread_ts=1001.0)
    conn.commit()
    build_chunks(conn, "w", "C1")
    thread_chunks = conn.execute("SELECT text FROM chunks WHERE thread_ts IS NOT NULL").fetchall()
    assert len(thread_chunks) == 1
    assert "thread reply" in thread_chunks[0][0]
    assert "channel chatter" not in thread_chunks[0][0]


def test_script_format_names_each_speaker(conn):
    rows = [
        {"author_name": "Jim Vogel", "text": "I'll have it thursday"},
        {"author_name": "Hunter Reeves", "text": "perfect"},
    ]
    assert format_script(rows) == "Jim Vogel: I'll have it thursday\nHunter Reeves: perfect"


def test_header_describes_a_dm_with_date_and_participants():
    header = build_header("Hunter Reeves", "im", ["Jim Vogel", "Hunter Reeves"], 1755129600.0)
    assert "DM" in header
    assert "Hunter Reeves" in header
    assert "2025-08" in header or "2025-08" in header


def test_header_describes_a_channel_with_its_name():
    header = build_header("gravi-cli", "public", ["Jim Vogel"], 1755129600.0)
    assert "#gravi-cli" in header


def test_rebuilding_a_channel_replaces_rather_than_duplicates(conn):
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    build_chunks(conn, "w", "C1")
    build_chunks(conn, "w", "C1")
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


def test_unchanged_chunk_keeps_its_embedded_flag(conn):
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    build_chunks(conn, "w", "C1")
    conn.execute("UPDATE chunks SET embedded = 1")
    conn.commit()
    build_chunks(conn, "w", "C1")
    assert conn.execute("SELECT embedded FROM chunks").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_chunk.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sherpa.corpus.chunk'`

- [ ] **Step 3: Write the implementation**

```python
# sherpa/corpus/chunk.py
"""Conversation windowing — the unit of embedding.

A single Slack message is too short and too context-dependent to embed on its
own: "yeah I'll get that to you friday" carries almost no standalone semantic
signal, and it is exactly the message these queries need to find. So the corpus
embeds conversation windows, formatted as a script and prefixed with a
deterministic context header.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

GAP_SECONDS = 1800
MAX_MESSAGES = 40
OVERLAP = 2
MAX_CHARS = 6000


def format_script(messages) -> str:
    return "\n".join(f"{m['author_name']}: {m['text']}" for m in messages)


def build_header(channel_name: str, kind: str, participants: list[str], start_ts: float) -> str:
    when = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    people = ", ".join(participants)
    if kind == "im":
        return f"DM between {people}, {when}"
    if kind == "mpim":
        return f"Group DM between {people}, {when}"
    return f"#{channel_name}, {when}, participants: {people}"


def rechunk_all(conn: sqlite3.Connection, workspace: str) -> int:
    channels = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT channel_id FROM messages WHERE workspace = ?", (workspace,)
        )
    ]
    return sum(build_chunks(conn, workspace, channel_id) for channel_id in channels)


def build_chunks(conn: sqlite3.Connection, workspace: str, channel_id: str) -> int:
    messages = conn.execute(
        "SELECT * FROM messages WHERE workspace = ? AND channel_id = ? ORDER BY ts",
        (workspace, channel_id),
    ).fetchall()
    if not messages:
        return 0

    threads: dict[float, list] = {}
    loose = []
    for message in messages:
        if message["thread_ts"]:
            threads.setdefault(message["thread_ts"], []).append(message)
        else:
            loose.append(message)

    windows = _window(loose)
    for thread_messages in threads.values():
        windows.extend(_split_oversized(thread_messages))

    written = 0
    for window in windows:
        if window:
            written += _write(conn, workspace, channel_id, window)
    conn.commit()
    return written


def _window(messages: list) -> list[list]:
    """Split loose channel messages on silence or length, with overlap."""
    if not messages:
        return []

    windows, current = [], [messages[0]]
    for previous, message in zip(messages, messages[1:]):
        too_quiet = message["ts"] - previous["ts"] > GAP_SECONDS
        too_long = len(current) >= MAX_MESSAGES
        if too_quiet or too_long:
            windows.append(current)
            current = current[-OVERLAP:] + [message]
        else:
            current.append(message)
    windows.append(current)
    return windows


def _split_oversized(messages: list) -> list[list]:
    """A thread is one chunk unless it is too big, then split by count with overlap."""
    if len(messages) <= MAX_MESSAGES and len(format_script(messages)) <= MAX_CHARS:
        return [messages]

    windows, current = [], []
    for message in messages:
        current.append(message)
        if len(current) >= MAX_MESSAGES or len(format_script(current)) > MAX_CHARS:
            windows.append(current)
            current = current[-OVERLAP:]
    if len(current) > OVERLAP or not windows:
        windows.append(current)
    return windows


def _write(conn: sqlite3.Connection, workspace: str, channel_id: str, window: list) -> int:
    text = format_script(window)
    participants = sorted({m["author_name"] for m in window})
    header = build_header(
        window[0]["channel_name"], window[0]["channel_kind"], participants, window[0]["ts"]
    )
    content_hash = hashlib.sha256(f"{header}\n{text}".encode()).hexdigest()

    existing = conn.execute(
        "SELECT id, content_hash FROM chunks"
        " WHERE workspace = ? AND channel_id = ? AND msg_first_id = ? AND msg_last_id = ?",
        (workspace, channel_id, window[0]["id"], window[-1]["id"]),
    ).fetchone()

    if existing and existing["content_hash"] == content_hash:
        return 0  # unchanged; leave `embedded` alone so it is not re-embedded

    if existing:
        conn.execute("DELETE FROM chunks WHERE id = ?", (existing["id"],))
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (existing["id"],))
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (existing["id"],))

    cursor = conn.execute(
        "INSERT INTO chunks (workspace, channel_id, start_ts, end_ts, thread_ts, participants,"
        " msg_first_id, msg_last_id, header, text, content_hash, embedded)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            workspace,
            channel_id,
            window[0]["ts"],
            window[-1]["ts"],
            window[0]["thread_ts"],
            ", ".join(participants),
            window[0]["id"],
            window[-1]["id"],
            header,
            text,
            content_hash,
        ),
    )
    conn.execute(
        "INSERT INTO chunks_fts (chunk_id, header, text, participants) VALUES (?, ?, ?, ?)",
        (cursor.lastrowid, header, text, ", ".join(participants)),
    )
    return 1
```

- [ ] **Step 4: Run the tests**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_chunk.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add sherpa/corpus/chunk.py tests/test_corpus_chunk.py
git commit -m "feat(corpus): conversation-window chunking with script format and headers"
```

---

## Task 8: Embeddings

**Files:**
- Create: `sherpa/corpus/embed.py`
- Test: `tests/test_corpus_embed.py`

**Interfaces:**
- Consumes: `open_db`, `EMBED_DIM`
- Produces:
  - `sherpa.corpus.embed.EmbeddingError(Exception)` with `.detail: str`
  - `sherpa.corpus.embed.Embedder` — protocol with `embed(texts: list[str]) -> list[list[float]]`
  - `sherpa.corpus.embed.GatewayEmbedder(base_url: str, api_key: str, model: str = "ollama/nomic-embed-text:latest", *, transport=None)`
  - `sherpa.corpus.embed.FakeEmbedder(dim: int = EMBED_DIM)` — deterministic vectors for tests
  - `sherpa.corpus.embed.embed_pending(conn, embedder: Embedder, *, batch: int = 64) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_embed.py
"""Embedding backfill: batching, skip-when-embedded, loud failure.

Run: uv run --with pytest --with httpx --with sqlite-vec pytest tests/test_corpus_embed.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import EMBED_DIM, open_db
from sherpa.corpus.embed import EmbeddingError, FakeEmbedder, GatewayEmbedder, embed_pending


def add_chunk(conn, chunk_id, text="hello", embedded=0):
    conn.execute(
        "INSERT INTO chunks (id, workspace, channel_id, start_ts, end_ts, participants,"
        " msg_first_id, msg_last_id, header, text, content_hash, embedded)"
        " VALUES (?, 'w', 'C1', 1.0, 2.0, 'Jim', 'w:C1:1', 'w:C1:2', 'hdr', ?, ?, ?)",
        (chunk_id, text, f"h{chunk_id}", embedded),
    )


def test_embed_pending_writes_vectors_and_flags_chunks(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1)
    add_chunk(conn, 2)
    conn.commit()
    assert embed_pending(conn, FakeEmbedder()) == 2
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM chunks WHERE embedded = 0").fetchone()[0] == 0


def test_already_embedded_chunks_are_skipped(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1, embedded=1)
    conn.commit()
    assert embed_pending(conn, FakeEmbedder()) == 0


def test_embedding_text_includes_the_header(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1, text="body text")
    conn.commit()
    embedder = FakeEmbedder()
    embed_pending(conn, embedder)
    assert embedder.seen[0].startswith("hdr")
    assert "body text" in embedder.seen[0]


def test_batching_respects_the_batch_size(tmp_path):
    conn = open_db(tmp_path / "c.db")
    for i in range(5):
        add_chunk(conn, i + 1)
    conn.commit()
    embedder = FakeEmbedder()
    embed_pending(conn, embedder, batch=2)
    assert [len(b) for b in embedder.batches] == [2, 2, 1]


def test_gateway_failure_raises_rather_than_leaving_chunks_pending(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1)
    conn.commit()

    class DeadEmbedder:
        def embed(self, texts):
            raise EmbeddingError("connection refused")

    with pytest.raises(EmbeddingError):
        embed_pending(conn, DeadEmbedder())
    assert conn.execute("SELECT embedded FROM chunks").fetchone()[0] == 0


def test_gateway_embedder_raises_embedding_error_on_http_failure():
    class DeadTransport:
        def post(self, url, headers=None, json=None):
            raise OSError("connection refused")

    embedder = GatewayEmbedder("https://llm.jc.turbo.inc/v1", "key", transport=DeadTransport())
    with pytest.raises(EmbeddingError):
        embedder.embed(["hello"])


def test_gateway_embedder_rejects_wrong_dimension():
    class ShortTransport:
        def post(self, url, headers=None, json=None):
            class R:
                status_code = 200

                def json(self):
                    return {"data": [{"embedding": [0.0, 1.0]}]}

            return R()

    embedder = GatewayEmbedder("https://llm.jc.turbo.inc/v1", "key", transport=ShortTransport())
    with pytest.raises(EmbeddingError) as caught:
        embedder.embed(["hello"])
    assert str(EMBED_DIM) in caught.value.detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with httpx --with sqlite-vec pytest tests/test_corpus_embed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sherpa.corpus.embed'`

- [ ] **Step 3: Write the implementation**

```python
# sherpa/corpus/embed.py
"""Chunk embeddings via the local LiteLLM gateway.

Failures raise. Nothing here degrades to keyword-only or quietly leaves chunks
pending: a corpus whose semantic half silently stopped working still answers
every question, just worse, and nothing about the answer says so.
"""

from __future__ import annotations

import sqlite3
import struct
from typing import Any, Protocol

from sherpa.corpus.db import EMBED_DIM

DEFAULT_MODEL = "ollama/nomic-embed-text:latest"
DEFAULT_BASE_URL = "https://llm.jc.turbo.inc/v1"


class EmbeddingError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class GatewayEmbedder:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        *,
        transport: Any = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._key = api_key
        self._model = model
        self._transport = transport

    def _client(self):
        if self._transport is None:
            import httpx

            self._transport = httpx.Client(timeout=120.0)
        return self._transport

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client().post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                json={"model": self._model, "input": texts},
            )
        except Exception as error:
            raise EmbeddingError(f"embedding gateway unreachable: {error}") from error

        if response.status_code != 200:
            raise EmbeddingError(f"embedding gateway returned HTTP {response.status_code}")

        vectors = [item["embedding"] for item in response.json().get("data", [])]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding gateway returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        for vector in vectors:
            if len(vector) != EMBED_DIM:
                raise EmbeddingError(
                    f"embedding gateway returned dimension {len(vector)}, expected {EMBED_DIM}"
                )
        return vectors


class FakeEmbedder:
    """Deterministic vectors for tests; records what it was asked to embed."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim
        self.seen: list[str] = []
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.seen.extend(texts)
        self.batches.append(list(texts))
        return [[float((hash(t) >> i) % 7) / 7.0 for i in range(self.dim)] for t in texts]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def embed_pending(conn: sqlite3.Connection, embedder: Embedder, *, batch: int = 64) -> int:
    pending = conn.execute(
        "SELECT id, header, text FROM chunks WHERE embedded = 0 ORDER BY id"
    ).fetchall()

    done = 0
    for start in range(0, len(pending), batch):
        window = pending[start : start + batch]
        texts = [f"{row['header']}\n{row['text']}" for row in window]
        vectors = embedder.embed(texts)
        for row, vector in zip(window, vectors):
            conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                (row["id"], _pack(vector)),
            )
            conn.execute("UPDATE chunks SET embedded = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        done += len(window)

    return done
```

- [ ] **Step 4: Run the tests**

Run: `uv run --with pytest --with httpx --with sqlite-vec pytest tests/test_corpus_embed.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add sherpa/corpus/embed.py tests/test_corpus_embed.py
git commit -m "feat(corpus): chunk embeddings via the local gateway, failing loudly"
```

---

## Task 9: Hybrid retrieval

**Files:**
- Create: `sherpa/corpus/search.py`
- Test: `tests/test_corpus_search.py`

**Interfaces:**
- Consumes: `open_db`, `EMBED_DIM`, `Embedder`
- Produces:
  - `sherpa.corpus.search.Filters` — dataclass `(person: str | None = None, mine: bool = False, workspace: str | None = None, channel: str | None = None, since: float | None = None, until: float | None = None)`
  - `sherpa.corpus.search.Hit` — dataclass `(chunk_id: int, score: float, header: str, transcript: str, permalink: str, channel: str, workspace: str, start_ts: float)`
  - `sherpa.corpus.search.RRF_K: int` (= 60)
  - `sherpa.corpus.search.search(conn, query: str, embedder, *, filters: Filters, k: int = 5, context: int = 3) -> list[Hit]`
  - `sherpa.corpus.search.conversation_with(conn, person: str, *, since: float | None = None, limit: int = 50) -> list[Hit]`
  - `sherpa.corpus.search.sent_to(conn, person: str, *, limit: int = 50) -> list[Hit]`
  - `sherpa.corpus.search.thread_at(conn, permalink_or_id: str) -> Hit | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_search.py
"""Hybrid retrieval: filters before fusion, RRF, hydration, dedupe.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_search.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.chunk import build_chunks
from sherpa.corpus.db import open_db
from sherpa.corpus.embed import FakeEmbedder, embed_pending
from sherpa.corpus.search import (
    Filters,
    RRF_K,
    conversation_with,
    search,
    sent_to,
    thread_at,
)


def insert(conn, ts, author, text, *, is_me=0, channel="D1", kind="im", name="Hunter Reeves"):
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " author_id, author_name, is_me, text, permalink)"
        " VALUES (?, 'gravitate', ?, ?, ?, ?, 'U', ?, ?, ?, ?)",
        (
            f"gravitate:{channel}:{ts}",
            channel,
            name,
            kind,
            ts,
            author,
            is_me,
            text,
            f"https://gravitate.slack.com/archives/{channel}/p{int(ts)}",
        ),
    )


@pytest.fixture
def corpus(tmp_path):
    conn = open_db(tmp_path / "c.db")
    insert(conn, 1000.0, "Hunter Reeves", "can you take the gravi cli flag work?")
    insert(conn, 1060.0, "Jim Vogel", "yeah I'll have the schema to you thursday", is_me=1)
    insert(conn, 9000.0, "Erica Lin", "need the KB-51508 writeup", channel="D2", name="Erica Lin")
    insert(conn, 9060.0, "Jim Vogel", "I'll get it done monday", is_me=1, channel="D2", name="Erica Lin")
    conn.commit()
    build_chunks(conn, "gravitate", "D1")
    build_chunks(conn, "gravitate", "D2")
    embed_pending(conn, FakeEmbedder())
    return conn


def test_search_returns_hits_with_transcripts_and_permalinks(corpus):
    hits = search(corpus, "schema", FakeEmbedder(), filters=Filters(), k=5)
    assert hits
    assert "Jim Vogel:" in hits[0].transcript
    assert hits[0].permalink.startswith("https://gravitate.slack.com/archives/")


def test_exact_token_is_found_even_though_embeddings_are_fake(corpus):
    """BM25 carries the exact-token case; this is why retrieval is hybrid."""
    hits = search(corpus, "KB-51508", FakeEmbedder(), filters=Filters(), k=5)
    assert any("KB-51508" in hit.transcript for hit in hits)


def test_person_filter_excludes_other_conversations(corpus):
    hits = search(corpus, "I'll", FakeEmbedder(), filters=Filters(person="Erica Lin"), k=5)
    assert hits
    assert all("Hunter" not in hit.transcript for hit in hits)


def test_mine_filter_requires_a_message_from_jim_in_the_chunk(corpus):
    corpus.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " author_id, author_name, is_me, text, permalink)"
        " VALUES ('gravitate:C9:1', 'gravitate', 'C9', 'noise', 'public', 500.0, 'U',"
        " 'Someone Else', 0, 'schema chatter nobody answered', 'https://x')"
    )
    corpus.commit()
    build_chunks(corpus, "gravitate", "C9")
    embed_pending(corpus, FakeEmbedder())
    hits = search(corpus, "schema", FakeEmbedder(), filters=Filters(mine=True), k=5)
    assert all("Someone Else" not in hit.transcript or "Jim Vogel" in hit.transcript for hit in hits)


def test_since_filter_excludes_older_conversations(corpus):
    hits = search(corpus, "I'll", FakeEmbedder(), filters=Filters(since=5000.0), k=5)
    assert all(hit.start_ts >= 5000.0 for hit in hits)


def test_context_widens_the_hydrated_range(corpus):
    for i in range(6):
        insert(corpus, 2000.0 + i, "Hunter Reeves", f"filler{i}", channel="D3", name="Hunter Reeves")
    insert(corpus, 2010.0, "Jim Vogel", "distinctive_promise", is_me=1, channel="D3", name="Hunter Reeves")
    corpus.commit()
    build_chunks(corpus, "gravitate", "D3")
    embed_pending(corpus, FakeEmbedder())
    narrow = search(corpus, "distinctive_promise", FakeEmbedder(), filters=Filters(channel="D3"), k=1, context=0)
    wide = search(corpus, "distinctive_promise", FakeEmbedder(), filters=Filters(channel="D3"), k=1, context=3)
    assert len(wide[0].transcript) >= len(narrow[0].transcript)


def test_overlapping_hits_are_merged_not_returned_twice(corpus):
    hits = search(corpus, "I'll", FakeEmbedder(), filters=Filters(), k=10)
    starts = [hit.start_ts for hit in hits]
    assert len(starts) == len(set(starts))


def test_conversation_with_returns_both_directions_newest_first(corpus):
    hits = conversation_with(corpus, "Hunter Reeves")
    assert hits
    assert "Hunter Reeves:" in hits[0].transcript
    assert "Jim Vogel:" in hits[0].transcript


def test_sent_to_returns_only_chunks_containing_jims_messages(corpus):
    hits = sent_to(corpus, "Hunter Reeves")
    assert hits
    assert all("Jim Vogel:" in hit.transcript for hit in hits)


def test_thread_at_resolves_a_permalink_to_its_conversation(corpus):
    permalink = corpus.execute(
        "SELECT permalink FROM messages WHERE text LIKE 'yeah I%'"
    ).fetchone()[0]
    hit = thread_at(corpus, permalink)
    assert hit is not None
    assert "thursday" in hit.transcript


def test_empty_query_result_is_an_empty_list_not_an_error(corpus):
    assert search(corpus, "zzzznonexistent", FakeEmbedder(), filters=Filters(), k=5) == []


def test_rrf_k_is_the_standard_constant():
    assert RRF_K == 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sherpa.corpus.search'`

- [ ] **Step 3: Write the implementation**

```python
# sherpa/corpus/search.py
"""Hybrid retrieval: BM25 and vector KNN behind hard filters, fused with RRF.

Filters are applied BEFORE fusion, not after, so a `--from hunter` query spends
its whole k-budget inside that conversation rather than retrieving globally and
discarding most of it.

RRF is used rather than weighted score blending because BM25 scores and cosine
distances are not on comparable scales; RRF is rank-only and sidesteps that.
"""

from __future__ import annotations

import re
import sqlite3
import struct
from dataclasses import dataclass

from sherpa.corpus.chunk import format_script
from sherpa.corpus.embed import Embedder

RRF_K = 60
CANDIDATES = 50


@dataclass
class Filters:
    person: str | None = None
    mine: bool = False
    workspace: str | None = None
    channel: str | None = None
    since: float | None = None
    until: float | None = None


@dataclass
class Hit:
    chunk_id: int
    score: float
    header: str
    transcript: str
    permalink: str
    channel: str
    workspace: str
    start_ts: float


def _where(filters: Filters) -> tuple[str, list]:
    clauses, params = ["1 = 1"], []
    if filters.workspace:
        clauses.append("c.workspace = ?")
        params.append(filters.workspace)
    if filters.channel:
        clauses.append("c.channel_id = ?")
        params.append(filters.channel)
    if filters.since is not None:
        clauses.append("c.end_ts >= ?")
        params.append(filters.since)
    if filters.until is not None:
        clauses.append("c.start_ts <= ?")
        params.append(filters.until)
    if filters.person:
        clauses.append("c.participants LIKE ?")
        params.append(f"%{filters.person}%")
    if filters.mine:
        clauses.append(
            "EXISTS (SELECT 1 FROM messages m WHERE m.workspace = c.workspace"
            " AND m.channel_id = c.channel_id AND m.ts BETWEEN c.start_ts AND c.end_ts"
            " AND m.is_me = 1)"
        )
    return " AND ".join(clauses), params


def _bm25_ranked(conn, query: str, filters: Filters) -> list[int]:
    where, params = _where(filters)
    sanitized = _fts_query(query)
    if not sanitized:
        return []
    rows = conn.execute(
        f"SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.chunk_id"
        f" WHERE chunks_fts MATCH ? AND {where}"
        f" ORDER BY bm25(chunks_fts) LIMIT ?",
        [sanitized, *params, CANDIDATES],
    ).fetchall()
    return [row[0] for row in rows]


def _fts_query(query: str) -> str:
    """FTS5 treats punctuation as syntax; quote each term so KB-51508 is literal."""
    terms = [t for t in re.findall(r"[\w\-\.]+", query) if t]
    return " OR ".join(f'"{term}"' for term in terms)


def _knn_ranked(conn, query: str, embedder: Embedder, filters: Filters) -> list[int]:
    where, params = _where(filters)
    allowed = [
        row[0]
        for row in conn.execute(f"SELECT c.id FROM chunks c WHERE {where}", params).fetchall()
    ]
    if not allowed:
        return []

    vector = embedder.embed([query])[0]
    placeholders = ",".join("?" * len(allowed))
    rows = conn.execute(
        f"SELECT chunk_id FROM chunks_vec"
        f" WHERE embedding MATCH ? AND k = ? AND chunk_id IN ({placeholders})"
        f" ORDER BY distance",
        [struct.pack(f"{len(vector)}f", *vector), CANDIDATES, *allowed],
    ).fetchall()
    return [row[0] for row in rows]


def _fuse(*rankings: list[int]) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + position + 1)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def search(
    conn: sqlite3.Connection,
    query: str,
    embedder: Embedder,
    *,
    filters: Filters | None = None,
    k: int = 5,
    context: int = 3,
) -> list[Hit]:
    filters = filters or Filters()
    fused = _fuse(
        _bm25_ranked(conn, query, filters),
        _knn_ranked(conn, query, embedder, filters),
    )

    hits, covered = [], []
    for chunk_id, score in fused:
        hit = _hydrate(conn, chunk_id, score, context)
        if hit is None:
            continue
        if any(_overlaps(hit, seen) for seen in covered):
            continue
        covered.append(hit)
        hits.append(hit)
        if len(hits) >= k:
            break
    return hits


def _overlaps(a: Hit, b: Hit) -> bool:
    return a.channel == b.channel and a.workspace == b.workspace and a.start_ts == b.start_ts


def _hydrate(conn, chunk_id: int, score: float, context: int) -> Hit | None:
    chunk = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    if chunk is None:
        return None

    messages = conn.execute(
        "SELECT * FROM messages WHERE workspace = ? AND channel_id = ?"
        " AND ts BETWEEN ? AND ? ORDER BY ts",
        (chunk["workspace"], chunk["channel_id"], chunk["start_ts"], chunk["end_ts"]),
    ).fetchall()

    if context:
        before = conn.execute(
            "SELECT * FROM messages WHERE workspace = ? AND channel_id = ? AND ts < ?"
            " ORDER BY ts DESC LIMIT ?",
            (chunk["workspace"], chunk["channel_id"], chunk["start_ts"], context),
        ).fetchall()
        after = conn.execute(
            "SELECT * FROM messages WHERE workspace = ? AND channel_id = ? AND ts > ?"
            " ORDER BY ts LIMIT ?",
            (chunk["workspace"], chunk["channel_id"], chunk["end_ts"], context),
        ).fetchall()
        messages = list(reversed(before)) + list(messages) + list(after)

    if not messages:
        return None

    return Hit(
        chunk_id=chunk_id,
        score=score,
        header=chunk["header"],
        transcript=format_script(messages),
        permalink=messages[0]["permalink"],
        channel=chunk["channel_id"],
        workspace=chunk["workspace"],
        start_ts=messages[0]["ts"],
    )


def conversation_with(
    conn: sqlite3.Connection, person: str, *, since: float | None = None, limit: int = 50
) -> list[Hit]:
    params = [f"%{person}%"]
    clause = "participants LIKE ?"
    if since is not None:
        clause += " AND end_ts >= ?"
        params.append(since)
    rows = conn.execute(
        f"SELECT id FROM chunks WHERE {clause} ORDER BY start_ts DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [h for h in (_hydrate(conn, row[0], 0.0, 0) for row in rows) if h]


def sent_to(conn: sqlite3.Connection, person: str, *, limit: int = 50) -> list[Hit]:
    """Chunks where Jim spoke, in a conversation involving this person."""
    rows = conn.execute(
        "SELECT c.id FROM chunks c WHERE c.participants LIKE ?"
        " AND EXISTS (SELECT 1 FROM messages m WHERE m.workspace = c.workspace"
        " AND m.channel_id = c.channel_id AND m.ts BETWEEN c.start_ts AND c.end_ts"
        " AND m.is_me = 1)"
        " ORDER BY c.start_ts DESC LIMIT ?",
        (f"%{person}%", limit),
    ).fetchall()
    return [h for h in (_hydrate(conn, row[0], 0.0, 0) for row in rows) if h]


def thread_at(conn: sqlite3.Connection, permalink_or_id: str) -> Hit | None:
    message = conn.execute(
        "SELECT * FROM messages WHERE permalink = ? OR id = ?",
        (permalink_or_id, permalink_or_id),
    ).fetchone()
    if message is None:
        return None

    chunk = conn.execute(
        "SELECT id FROM chunks WHERE workspace = ? AND channel_id = ?"
        " AND ? BETWEEN start_ts AND end_ts ORDER BY start_ts LIMIT 1",
        (message["workspace"], message["channel_id"], message["ts"]),
    ).fetchone()
    return _hydrate(conn, chunk[0], 0.0, 3) if chunk else None
```

- [ ] **Step 4: Run the tests**

Run: `uv run --with pytest --with sqlite-vec pytest tests/test_corpus_search.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add sherpa/corpus/search.py tests/test_corpus_search.py
git commit -m "feat(corpus): hybrid BM25 + vector retrieval with RRF and context expansion"
```

---

## Task 10: Wire the read subcommands into the CLI

**Files:**
- Modify: `tools/corpus.py`
- Modify: `tests/test_corpus_cli.py`

**Interfaces:**
- Consumes: `search`, `conversation_with`, `sent_to`, `thread_at`, `Filters`, `Hit`, `GatewayEmbedder`, `check`, `warnings`, `embed_pending`, `rechunk_all`
- Produces: `corpus.parse_since(value: str) -> float` — accepts `30d`, `6h`, `2w`, or an ISO date

- [ ] **Step 1: Write the failing test (append to `tests/test_corpus_cli.py`)**

```python
def test_parse_since_accepts_relative_and_iso_forms():
    import time

    now = time.time()
    assert abs(corpus_tool.parse_since("1d") - (now - 86400)) < 5
    assert abs(corpus_tool.parse_since("2h") - (now - 7200)) < 5
    assert abs(corpus_tool.parse_since("1w") - (now - 604800)) < 5
    assert corpus_tool.parse_since("2026-01-01") < now


def test_parse_since_rejects_garbage():
    with pytest.raises(SystemExit) as caught:
        corpus_tool.parse_since("soonish")
    assert caught.value.code == 2


def test_search_warns_loudly_when_the_corpus_is_stale(workspace, capsys, monkeypatch):
    config, _ = workspace
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: _StubEmbedder())
    with pytest.raises(SystemExit):
        corpus_tool.main(["search", "anything", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"]


def test_stale_ok_suppresses_the_warning(workspace, capsys, monkeypatch):
    config, _ = workspace
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: _StubEmbedder())
    with pytest.raises(SystemExit):
        corpus_tool.main(["search", "anything", "--config", str(config), "--stale-ok", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" not in payload


class _StubEmbedder:
    def embed(self, texts):
        return [[0.0] * 768 for _ in texts]
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run --with pytest --with httpx --with sqlite-vec --with python-toon==0.1.3 --with pyyaml pytest tests/test_corpus_cli.py -v`
Expected: FAIL — `AttributeError: module 'corpus_tool' has no attribute 'parse_since'`

- [ ] **Step 3: Add `parse_since`, `make_embedder`, and the read handlers to `tools/corpus.py`**

Add these imports alongside the existing ones:

```python
import re
import time

from sherpa.corpus.chunk import rechunk_all
from sherpa.corpus.embed import DEFAULT_BASE_URL, DEFAULT_MODEL, EmbeddingError, GatewayEmbedder, embed_pending
from sherpa.corpus.search import Filters, Hit, conversation_with, search, sent_to, thread_at

RELATIVE = re.compile(r"^(\d+)([hdw])$")
SECONDS = {"h": 3600, "d": 86400, "w": 604800}
```

```python
def parse_since(value: str) -> float:
    if match := RELATIVE.match(value):
        amount, unit = match.groups()
        return time.time() - int(amount) * SECONDS[unit]
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        fail(
            f"could not read --since {value!r}",
            help="sherpa corpus search <QUERY> --since 30d   (or an ISO date: 2026-01-01)",
            usage=True,
        )


def make_embedder():
    vault_path = Path.home() / ".sherpa" / "vault.json"
    vault = json.loads(vault_path.read_text()) if vault_path.exists() else {}
    key = vault.get("LITELLM_KEY_CORPUS") or vault.get("LITELLM_KEY", "")
    if not key:
        print("MISSING_SECRET: LITELLM_KEY_CORPUS", file=sys.stderr)
        fail(
            "missing secret LITELLM_KEY_CORPUS",
            help="sherpa vault_manager set LITELLM_KEY_CORPUS <key>   (mint one: sherpa litellm_keys create)",
            usage=True,
        )
    return GatewayEmbedder(DEFAULT_BASE_URL, key, DEFAULT_MODEL)


def render(hits: list[Hit], *, limit_chars: int = 1200) -> list[dict]:
    rendered = []
    for hit in hits:
        transcript, notice = truncate(hit.transcript, limit_chars)
        entry = {
            "when": datetime.fromtimestamp(hit.start_ts).strftime("%Y-%m-%d %H:%M"),
            "where": hit.header,
            "conversation": transcript,
            "permalink": hit.permalink,
        }
        if notice:
            entry["truncated"] = notice
        rendered.append(entry)
    return rendered


def with_health(payload: dict, conn, config, stale_ok: bool) -> dict:
    if stale_ok:
        return payload
    if lines := warnings(check(conn, config)):
        payload["warnings"] = lines
        payload["help"] = ["sherpa corpus sync"]
    return payload


def cmd_search(args, config) -> int:
    conn = open_db(config.db)
    filters = Filters(
        person=args.from_person,
        mine=args.mine,
        workspace=args.workspace,
        channel=args.channel,
        since=parse_since(args.since) if args.since else None,
    )
    try:
        hits = search(conn, args.query, make_embedder(), filters=filters, k=args.k, context=args.context)
    except EmbeddingError as error:
        notify("Slack corpus: embedding gateway down", error.detail)
        fail(
            f"embedding gateway unavailable: {error.detail}",
            help="sherpa corpus search <QUERY>   (retry once the gateway is reachable)",
        )

    payload = {"query": args.query, "count": len(hits), "results": render(hits)}
    if not hits:
        payload["results"] = []
        payload["note"] = f"no conversations matched {args.query!r}"
    emit(with_health(payload, conn, config, args.stale_ok), as_json=args.json)
    return 0


def cmd_with(args, config) -> int:
    conn = open_db(config.db)
    hits = conversation_with(conn, args.person, since=parse_since(args.since) if args.since else None)
    payload = {"person": args.person, "count": len(hits), "results": render(hits)}
    if not hits:
        payload["note"] = f"no conversations with {args.person!r} in the corpus"
    emit(with_health(payload, conn, config, args.stale_ok), as_json=args.json)
    return 0


def cmd_owed(args, config) -> int:
    conn = open_db(config.db)
    hits = sent_to(conn, args.person)
    payload = {
        "person": args.person,
        "count": len(hits),
        "results": render(hits),
        "note": "Read these and summarize what was promised — this is a filter, not a classifier.",
    }
    emit(with_health(payload, conn, config, args.stale_ok), as_json=args.json)
    return 0


def cmd_thread(args, config) -> int:
    conn = open_db(config.db)
    hit = thread_at(conn, args.permalink)
    if hit is None:
        fail(
            f"no message in the corpus at {args.permalink}",
            help="sherpa corpus sync",
        )
    emit({"result": render([hit])[0]}, as_json=args.json)
    return 0
```

In `build_parser()`, replace the four stub parsers added in Task 6 with these real ones. Each read subcommand takes `--stale-ok`:

```python
    search_parser = subparsers.add_parser("search", allow_abbrev=False)
    search_parser.add_argument("query")
    search_parser.add_argument("--from", dest="from_person", default=None)
    search_parser.add_argument("--mine", action="store_true")
    search_parser.add_argument("--since", default=None)
    search_parser.add_argument("--workspace", default=None)
    search_parser.add_argument("--channel", default=None)
    search_parser.add_argument("--k", type=int, default=5)
    search_parser.add_argument("--context", type=int, default=3)
    search_parser.add_argument("--stale-ok", dest="stale_ok", action="store_true")
    registry["search"] = search_parser

    with_parser = subparsers.add_parser("with", allow_abbrev=False)
    with_parser.add_argument("person")
    with_parser.add_argument("--since", default=None)
    with_parser.add_argument("--stale-ok", dest="stale_ok", action="store_true")
    registry["with"] = with_parser

    owed_parser = subparsers.add_parser("owed", allow_abbrev=False)
    owed_parser.add_argument("person")
    owed_parser.add_argument("--stale-ok", dest="stale_ok", action="store_true")
    registry["owed"] = owed_parser

    thread_parser = subparsers.add_parser("thread", allow_abbrev=False)
    thread_parser.add_argument("permalink")
    thread_parser.add_argument("--stale-ok", dest="stale_ok", action="store_true")
    registry["thread"] = thread_parser
```

Add `from datetime import datetime` to the imports, then register the four handlers in `main()`:

```python
    handlers = {
        "sync": cmd_sync,
        "status": cmd_status,
        "search": cmd_search,
        "with": cmd_with,
        "owed": cmd_owed,
        "thread": cmd_thread,
    }
```

- [ ] **Step 4: Make `sync` also chunk and embed**

Replace the body of the per-workspace loop in `cmd_sync` after `ingest_workspace` returns, before appending to `results`:

```python
        rechunk_all(conn, workspace.name)
        try:
            embedded = embed_pending(conn, make_embedder())
        except EmbeddingError as error:
            notify("Slack corpus: embedding gateway down", error.detail)
            fail(
                f"embedding gateway unavailable: {error.detail}",
                help="sherpa corpus sync   (retry once the gateway is reachable)",
            )
```

and include `"embedded": embedded` in the appended result dict.

- [ ] **Step 5: Run the full corpus test suite**

Run: `uv run --with pytest --with httpx --with sqlite-vec --with python-toon==0.1.3 --with pyyaml pytest tests/test_corpus_*.py -v`
Expected: all pass

- [ ] **Step 6: Run the repo's conformance suites**

Run: `uv run --with pytest --with pyyaml pytest tests/test_manifest.py tests/test_toon_conformance.py tests/test_tool_visibility.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add tools/corpus.py tests/test_corpus_cli.py
git commit -m "feat(corpus): search, with, owed and thread subcommands"
```

---

## Task 11: First real sync and cron install

This task runs against real Slack and real hardware. It is the only place the corpus is proven end-to-end; every prior task ran against fakes.

**Files:**
- Create: `~/.sherpa/corpus.toml` (not in the repo — it is user config)
- Create: `docs/corpus-setup.md`

- [ ] **Step 1: Create a Slack app and mint a user token**

In each workspace, at `https://api.slack.com/apps` → Create New App → From scratch. Under **OAuth & Permissions**, add these **User Token Scopes**:

```
channels:history   channels:read
groups:history     groups:read
im:history         im:read
mpim:history       mpim:read
users:read         search:read
```

Install to workspace, copy the **User OAuth Token** (`xoxp-…`).

```bash
sherpa vault_manager set SLACK_USER_TOKEN_GRAVITATE xoxp-...
```

- [ ] **Step 2: Mint a LiteLLM key for embeddings**

```bash
sherpa litellm_keys create --alias corpus --models ollama/nomic-embed-text:latest
sherpa vault_manager set LITELLM_KEY_CORPUS <key>
```

- [ ] **Step 3: Write the config**

```bash
cat > ~/.sherpa/corpus.toml <<'TOML'
db = "~/.sherpa/corpus.db"
stale_after_hours = 24

[workspace.gravitate]
token_key = "SLACK_USER_TOKEN_GRAVITATE"
TOML
```

- [ ] **Step 4: First sync, DMs only, to prove the pipeline before the long backfill**

```bash
sherpa corpus sync --budget 120
sherpa corpus status
```

Expected: non-zero message count, `unembedded: 0`. If `status` exits 2 with warnings, read them — that is the design working.

- [ ] **Step 5: Verify the two motivating queries by hand**

```bash
sherpa corpus owed "Hunter"
sherpa corpus search "gravi cli" --from Hunter
```

Expected: real conversations with permalinks. **If these return nothing useful, stop and report it** — that is the signal that reranking or a different chunk size is needed, and it is the only honest place to learn it.

- [ ] **Step 6: Run the full backfill**

```bash
sherpa corpus sync --budget 3600
sherpa corpus sync --budget 3600   # repeat until status reports no pending tiers
sherpa corpus status
```

- [ ] **Step 7: Install the cron entry**

```bash
( crontab -l 2>/dev/null; echo "*/15 * * * * cd $HOME && /usr/bin/env sherpa corpus sync --budget 600 >> $HOME/.sherpa/corpus.log 2>&1" ) | crontab -
crontab -l | grep corpus
```

Cron mail plus the `sherpa notify` push means a failed sync is visible two ways.

- [ ] **Step 8: Write the setup doc**

Create `docs/corpus-setup.md` recording: the Slack scopes above, the vault keys, the config shape, the cron line, and how to add a second workspace (new app, new `SLACK_USER_TOKEN_<NAME>`, new `[workspace.<name>]` block, then `sherpa corpus sync --workspace <name>`).

- [ ] **Step 9: Commit**

```bash
git add docs/corpus-setup.md
git commit -m "docs(corpus): slack app scopes, vault keys, and cron setup"
```

---

## Self-Review Notes

**Spec coverage check:**

| Spec section | Task |
|---|---|
| Data model | 1 |
| Multi-workspace config + vault tokens + auth | 2, 11 |
| Slack client, rate limits | 3 |
| ID→name resolution | 4 |
| Ingest scope, tiers, cursors, thread sweep, edits/deletes, budget | 5 |
| Chunking, script format, contextual header | 7 |
| Embeddings, local gateway, content-hash skip | 8 |
| Hybrid retrieval, filters before fusion, RRF, context expansion, dedupe | 9 |
| Query surface (`search`/`with`/`owed`/`thread`/`sync`/`status`) | 6, 10 |
| Fail loudly: gateway, token, stale warning, non-zero exits | 6, 8, 10 |
| Cron | 11 |
| Testing strategy | every task; real-corpus retrieval assertions in 11 Step 5 |

**Deferred by design, per the spec:** reranking, LLM-written topic headers, and non-Slack sources (transcripts, screenshots, Gmail). No task implements them.

**Known follow-up:** `_knn_ranked` inlines the allowed-ID list into the SQL. Above roughly 10k candidate chunks after filtering, that hits SQLite's variable limit and needs a temp table instead. Fine at current scale; revisit when `status` reports chunk counts in that range.
