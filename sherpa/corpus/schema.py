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
