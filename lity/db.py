"""SQLite (WAL) — the single source of truth. Async via aiosqlite."""

import json
from pathlib import Path

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES threads(id),
    kind TEXT NOT NULL DEFAULT 'sub',          -- main | sub | agent
    title TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    role TEXT NOT NULL,                        -- user | assistant | event | tool
    content TEXT NOT NULL,
    tool_name TEXT,
    attachment TEXT,                           -- JSON {path, name, mime} or NULL
    tokens INTEGER NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 0,      -- folded into summary, out of context
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TABLE IF NOT EXISTS summaries (
    thread_id INTEGER PRIMARY KEY REFERENCES threads(id),
    content TEXT NOT NULL,
    covers_until_message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'project',      -- user | project | feedback | reference
    content TEXT NOT NULL,
    source_thread_id INTEGER,
    embedding BLOB,                            -- float32 vector (local model2vec model)
    archived INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT,                     -- usage stats: feed recall ranking
    recall_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL DEFAULT '*',           -- owning agent, or '*' for all
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    content TEXT NOT NULL,                     -- the distilled procedure
    uses INTEGER NOT NULL DEFAULT 0,
    source_task_id INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(name, description, content, content='skills', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts(rowid, name, description, content) VALUES (new.id, new.name, new.description, new.content);
END;
CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, content) VALUES ('delete', old.id, old.name, old.description, old.content);
END;
CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, content) VALUES ('delete', old.id, old.name, old.description, old.content);
    INSERT INTO skills_fts(rowid, name, description, content) VALUES (new.id, new.name, new.description, new.content);
END;

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    thread_id INTEGER NOT NULL REFERENCES threads(id),          -- the sub-agent's own thread
    parent_thread_id INTEGER NOT NULL REFERENCES threads(id),   -- where the result goes
    status TEXT NOT NULL DEFAULT 'queued',     -- queued | running | waiting_user | blocked
                                               --   | done | failed | cancelled
    task TEXT NOT NULL,
    result TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                        -- timer | cron
    spec TEXT NOT NULL,
    prompt TEXT NOT NULL,
    thread_id INTEGER NOT NULL DEFAULT 1,
    next_run TEXT,
    last_run TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shopping_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shopping_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES shopping_lists(id),
    item TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_shopping_items_list ON shopping_items(list_id);

CREATE TABLE IF NOT EXISTS qtimers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                        -- timer | alarm
    label TEXT NOT NULL DEFAULT '',
    fires_at TEXT NOT NULL,                    -- UTC, %Y-%m-%d %H:%M:%S
    thread_id INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | ringing | done
                                               --   | cancelled | missed
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,                      -- tiny persistent flags
    value TEXT NOT NULL                        --   (e.g. reflection.last_date)
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',     -- active | done | dropped
    review_at TEXT,                            -- UTC; when due, a system event
                                               --   wakes the kernel to act on it
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',          -- kernel | utility | task | ...
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_time ON llm_usage(created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    level INTEGER NOT NULL,
    task_id INTEGER,
    thread_id INTEGER,
    run_id TEXT,                               -- set when bridged from a Hermes run
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | approved | always | denied | expired
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        try:  # migration for DBs created before the attachment column existed
            await self.conn.execute("ALTER TABLE messages ADD COLUMN attachment TEXT")
        except aiosqlite.OperationalError:
            pass
        try:  # migration: approvals.run_id (Hermes executor bridge)
            await self.conn.execute("ALTER TABLE approvals ADD COLUMN run_id TEXT")
        except aiosqlite.OperationalError:
            pass
        for ddl in (  # migration: memory embeddings + recall usage stats
                "ALTER TABLE memories ADD COLUMN embedding BLOB",
                "ALTER TABLE memories ADD COLUMN last_recalled_at TEXT",
                "ALTER TABLE memories ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0"):
            try:
                await self.conn.execute(ddl)
            except aiosqlite.OperationalError:
                pass
        # backfill FTS for messages that predate the messages_fts table
        counts = await self.fetchone(
            "SELECT (SELECT COUNT(*) FROM messages) AS m, (SELECT COUNT(*) FROM messages_fts) AS f")
        if counts["m"] and not counts["f"]:
            await self.conn.execute(
                "INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages")
        row = await self.fetchone("SELECT id FROM threads WHERE id=1")
        if not row:
            await self.execute(
                "INSERT INTO threads(id, parent_id, kind, title) VALUES (1, NULL, 'main', 'Home')"
            )
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    # ── thin helpers ──────────────────────────────────────────────────────
    async def execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    async def fetchone(self, sql: str, params: tuple = ()):
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        cur = await self.conn.execute(sql, params)
        return await cur.fetchall()

    async def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    async def set_kv(self, key: str, value: str):
        await self.execute(
            "INSERT INTO kv(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # ── domain helpers ────────────────────────────────────────────────────
    async def add_message(self, thread_id: int, role: str, content: str,
                          tool_name: str | None = None, tokens: int = 0,
                          attachment: str | None = None) -> int:
        return await self.execute(
            "INSERT INTO messages(thread_id, role, content, tool_name, tokens, attachment) "
            "VALUES (?,?,?,?,?,?)",
            (thread_id, role, content, tool_name, tokens, attachment),
        )

    async def create_thread(self, title: str, kind: str = "sub",
                            parent_id: int | None = None) -> int:
        return await self.execute(
            "INSERT INTO threads(parent_id, kind, title) VALUES (?,?,?)",
            (parent_id, kind, title),
        )

    async def reset_context(self, thread_id: int) -> int:
        """Fresh start: fold EVERYTHING out of the kernel's working context —
        every message becomes collapsed and the rolling summary is dropped.
        Nothing is deleted: history stays in this table (and FTS search); only
        what the kernel sees next turn is a clean slate."""
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE thread_id=? AND collapsed=0",
            (thread_id,))
        await self.execute(
            "UPDATE messages SET collapsed=1 WHERE thread_id=? AND collapsed=0",
            (thread_id,))
        await self.execute("DELETE FROM summaries WHERE thread_id=?", (thread_id,))
        return int(row["n"])

    async def recent_messages(self, thread_id: int, limit: int):
        rows = await self.fetchall(
            "SELECT * FROM messages WHERE thread_id=? AND collapsed=0 ORDER BY id DESC LIMIT ?",
            (thread_id, limit),
        )
        return list(reversed(rows))


def row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
