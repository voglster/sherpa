"""Table definitions for the corpus database.

Migrations are append-only: to change the schema, add a statement to the end of
MIGRATIONS and bump SCHEMA_VERSION. PRAGMA user_version records how many have
been applied, so a partially-migrated database picks up where it stopped.
"""

from __future__ import annotations

EMBED_DIM = 768

MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS messages (
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

    CREATE INDEX IF NOT EXISTS messages_channel_ts ON messages (workspace, channel_id, ts);
    CREATE INDEX IF NOT EXISTS messages_thread     ON messages (workspace, channel_id, thread_ts);
    CREATE INDEX IF NOT EXISTS messages_mine       ON messages (is_me, ts);
    CREATE INDEX IF NOT EXISTS messages_author     ON messages (author_name, ts);

    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5 (
        id UNINDEXED, text, author_name, channel_name,
        content = 'messages', content_rowid = 'rowid'
    );

    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts (rowid, id, text, author_name, channel_name)
        VALUES (new.rowid, new.id, new.text, new.author_name, new.channel_name);
    END;

    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, id, text, author_name, channel_name)
        VALUES ('delete', old.rowid, old.id, old.text, old.author_name, old.channel_name);
    END;

    CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, id, text, author_name, channel_name)
        VALUES ('delete', old.rowid, old.id, old.text, old.author_name, old.channel_name);
        INSERT INTO messages_fts (rowid, id, text, author_name, channel_name)
        VALUES (new.rowid, new.id, new.text, new.author_name, new.channel_name);
    END;

    CREATE TABLE IF NOT EXISTS channels (
        workspace  TEXT NOT NULL,
        id         TEXT NOT NULL,
        name       TEXT NOT NULL,
        kind       TEXT NOT NULL,
        is_member  INTEGER NOT NULL DEFAULT 0,
        member_ids TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (workspace, id)
    );

    CREATE TABLE IF NOT EXISTS users (
        workspace    TEXT NOT NULL,
        id           TEXT NOT NULL,
        name         TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        is_me        INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace, id)
    );

    CREATE TABLE IF NOT EXISTS sync_state (
        workspace  TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        last_ts    REAL NOT NULL DEFAULT 0,
        tier       INTEGER NOT NULL DEFAULT 3,
        status     TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | failed
        detail     TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace, channel_id)
    );

    CREATE TABLE IF NOT EXISTS chunks (
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

    CREATE INDEX IF NOT EXISTS chunks_channel  ON chunks (workspace, channel_id, start_ts);
    CREATE INDEX IF NOT EXISTS chunks_pending  ON chunks (embedded);
    CREATE UNIQUE INDEX IF NOT EXISTS chunks_range ON chunks (workspace, channel_id, msg_first_id, msg_last_id);

    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5 (
        chunk_id UNINDEXED, header, text, participants
    );

    CREATE TABLE IF NOT EXISTS sync_runs (
        id         INTEGER PRIMARY KEY,
        workspace  TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at   REAL,
        status     TEXT NOT NULL,   -- running | ok | failed
        detail     TEXT NOT NULL DEFAULT ''
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS thread_sweeps (
        workspace  TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        thread_ts  REAL NOT NULL,
        last_swept REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace, channel_id, thread_ts)
    );

    CREATE INDEX IF NOT EXISTS thread_sweeps_order ON thread_sweeps (workspace, channel_id, last_swept);
    """,
    """
    CREATE TABLE IF NOT EXISTS backfill_state (
        workspace  TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        oldest_ts  REAL,                         -- NULL until the first page lands
        complete   INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace, channel_id)
    );
    """,
]

# Indexes over added columns. Applied after COLUMN_ADDITIONS on every open, for
# the same reason those are not migrations: a migration may be replayed before
# the column it indexes exists.
LATE_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS messages_thread_raw"
    " ON messages (workspace, channel_id, thread_ts_raw)",
]

# Added columns, applied idempotently on every open.
#
# `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS` form, so it cannot live in
# MIGRATIONS: those must survive being replayed from user_version 0 against a
# database that already has the tables. Each entry names its table, its column
# definition, and the statement that fills the column in for rows written before
# it existed -- run only in the same pass that adds the column.
#
# A Slack ts is a string with exactly six fractional digits and the API matches
# it literally: `str(float("1789151200.000100"))` is "1789151200.0001", which
# addresses no message at all. The REAL columns stay -- they are what ordering
# and range queries want -- and these carry the exact string for every value
# that is ever handed back to Slack.
COLUMN_ADDITIONS: list[tuple[str, str, str, str | None]] = [
    ("messages", "ts_raw", "TEXT NOT NULL DEFAULT ''",
     "UPDATE messages SET ts_raw = CAST(ts AS TEXT)"),
    ("messages", "thread_ts_raw", "TEXT NOT NULL DEFAULT ''",
     "UPDATE messages SET thread_ts_raw = CAST(thread_ts AS TEXT) WHERE thread_ts IS NOT NULL"),
    ("sync_state", "last_ts_raw", "TEXT NOT NULL DEFAULT ''", None),
    ("backfill_state", "oldest_ts_raw", "TEXT NOT NULL DEFAULT ''", None),
    ("thread_sweeps", "thread_ts_raw", "TEXT NOT NULL DEFAULT ''",
     "UPDATE thread_sweeps SET thread_ts_raw = CAST(thread_ts AS TEXT)"),
    ("thread_sweeps", "swept", "INTEGER NOT NULL DEFAULT 0",
     "UPDATE thread_sweeps SET swept = 1"),
]

SCHEMA_VERSION = len(MIGRATIONS)
