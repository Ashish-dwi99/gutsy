from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PERSONAL_FIELDS = (
    "full_name",
    "email",
    "phone",
    "date_of_birth",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
)
WORKSTREAM_STATUSES = ("active", "waiting", "completed", "cancelled")
WORKSTREAM_LIMIT = 100
WORKSTREAMS_RECALLED = 8
MEMORY_LIMIT = 200
VAULT_KINDS = ("login",)
GRANT_WINDOW_SECONDS = 20 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  chat_id TEXT NOT NULL, brain TEXT NOT NULL, session_id TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY (chat_id, brain));
CREATE TABLE IF NOT EXISTS personal_info (field TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, text TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workstreams (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL,
  revision INTEGER NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL,
  rule TEXT NOT NULL, next_run_at REAL, status TEXT NOT NULL, last_run_at REAL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trusted_tools (tool TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, action TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS vault_items (
  handle TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, origins TEXT NOT NULL,
  hint TEXT NOT NULL, created_at REAL NOT NULL);
"""


class StoreError(ValueError):
    """A request the store refuses; the message is safe to show the model."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class LineStore:
    """Everything the line remembers, in one SQLite file.

    The daemon and the MCP server (a child of the brain) open the same file;
    WAL mode lets both read while one writes.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    # kv -----------------------------------------------------------------
    def get_kv(self, key: str, default: str = "") -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    # sessions -----------------------------------------------------------
    def session_id(self, chat_id: str, brain: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT session_id FROM sessions WHERE chat_id=? AND brain=?", (chat_id, brain)
            ).fetchone()
        return row["session_id"] if row else None

    def save_session(self, chat_id: str, brain: str, session_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)",
                (chat_id, brain, session_id, time.time()),
            )

    def clear_sessions(self, chat_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))

    # personal info ------------------------------------------------------
    def personal_info(self) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute("SELECT field, value FROM personal_info").fetchall()
        values = {row["field"]: row["value"] for row in rows}
        return {name: values[name] for name in PERSONAL_FIELDS if name in values}

    def update_personal_info(self, updates: dict[str, str | None]) -> dict[str, str]:
        unknown = sorted(set(updates) - set(PERSONAL_FIELDS))
        if unknown:
            raise StoreError(f"unknown personal info fields: {', '.join(unknown)}")
        with self._connect() as db:
            for name, value in updates.items():
                if value is None or not str(value).strip():
                    db.execute("DELETE FROM personal_info WHERE field=?", (name,))
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO personal_info VALUES (?, ?, ?)",
                        (name, str(value).strip(), time.time()),
                    )
        return self.personal_info()

    # memories -----------------------------------------------------------
    def memories(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id, text FROM memories ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def remember(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise StoreError("memory text is empty")
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if count >= MEMORY_LIMIT:
                raise StoreError(f"memory is full ({MEMORY_LIMIT}); forget an obsolete one first")
            item = {"id": _id("mem"), "text": text}
            db.execute("INSERT INTO memories VALUES (?, ?, ?)", (item["id"], text, time.time()))
        return item

    def forget_memory(self, memory_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount > 0

    # workstreams --------------------------------------------------------
    def workstreams(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM workstreams"
        if not include_closed:
            query += " WHERE status IN ('active', 'waiting')"
        with self._connect() as db:
            rows = db.execute(query + " ORDER BY updated_at DESC").fetchall()
        return [_workstream(row) for row in rows]

    def save_workstream(
        self,
        *,
        title: str,
        status: str,
        body: dict[str, Any],
        workstream_id: str | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        if status not in WORKSTREAM_STATUSES:
            raise StoreError(f"status must be one of {', '.join(WORKSTREAM_STATUSES)}")
        if not title.strip():
            raise StoreError("a workstream needs a title")
        now = time.time()
        with self._connect() as db:
            if workstream_id is None:
                count = db.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0]
                if count >= WORKSTREAM_LIMIT:
                    raise StoreError("workstreams are full; ask the owner which obsolete one to forget")
                workstream_id = _id("ws")
                db.execute(
                    "INSERT INTO workstreams VALUES (?, ?, ?, ?, 1, ?)",
                    (workstream_id, title.strip(), status, json.dumps(body), now),
                )
            else:
                row = db.execute("SELECT revision FROM workstreams WHERE id=?", (workstream_id,)).fetchone()
                if row is None:
                    raise StoreError(f"no workstream {workstream_id}")
                if revision != row["revision"]:
                    raise StoreError(
                        f"workstream {workstream_id} is at revision {row['revision']}; "
                        "read it again and merge before saving"
                    )
                db.execute(
                    "UPDATE workstreams SET title=?, status=?, body=?, revision=revision+1, updated_at=? WHERE id=?",
                    (title.strip(), status, json.dumps(body), now, workstream_id),
                )
            saved = db.execute("SELECT * FROM workstreams WHERE id=?", (workstream_id,)).fetchone()
        return _workstream(saved)

    def forget_workstream(self, workstream_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM workstreams WHERE id=?", (workstream_id,)).rowcount > 0

    # schedules ----------------------------------------------------------
    def schedules(self, *, chat_id: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM schedules", ()
        if chat_id is not None:
            query, params = query + " WHERE chat_id=?", (chat_id,)
        with self._connect() as db:
            rows = db.execute(query + " ORDER BY created_at", params).fetchall()
        return [_schedule(row) for row in rows]

    def add_schedule(
        self, *, chat_id: str, title: str, prompt: str, rule: dict[str, Any], next_run_at: float
    ) -> dict[str, Any]:
        schedule_id = _id("sch")
        with self._connect() as db:
            db.execute(
                "INSERT INTO schedules VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?)",
                (schedule_id, chat_id, title, prompt, json.dumps(rule), next_run_at, time.time()),
            )
        return self.schedule(schedule_id)

    def schedule(self, schedule_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise StoreError(f"no schedule {schedule_id}")
        return _schedule(row)

    def update_schedule(self, schedule_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"title", "prompt", "rule", "next_run_at", "status", "last_run_at"}
        if set(changes) - allowed:
            raise StoreError(f"cannot change {sorted(set(changes) - allowed)}")
        if "rule" in changes:
            changes["rule"] = json.dumps(changes["rule"])
        self.schedule(schedule_id)
        with self._connect() as db:
            for key, value in changes.items():
                db.execute(f"UPDATE schedules SET {key}=? WHERE id=?", (value, schedule_id))
        return self.schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,)).rowcount > 0

    def due_schedules(self, now: float) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM schedules WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=?"
                " ORDER BY next_run_at",
                (now,),
            ).fetchall()
        return [_schedule(row) for row in rows]

    # trusted tools ------------------------------------------------------
    def is_trusted_tool(self, tool: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM trusted_tools WHERE tool=?", (tool,)).fetchone() is not None

    def trust_tool(self, tool: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO trusted_tools VALUES (?, ?)", (tool, time.time()))

    # approval grants ------------------------------------------------------
    def add_grant(self, action: str, details: str, now: float) -> str:
        """Record an owner approval; it opens payment pages to the brain for a short window."""
        grant_id = _id("grant")
        with self._connect() as db:
            db.execute("INSERT INTO grants VALUES (?, ?, ?, ?)", (grant_id, action, details, now))
        return grant_id

    def has_active_grant(self, now: float) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM grants WHERE created_at > ?", (now - GRANT_WINDOW_SECONDS,)).fetchone()
        return row is not None

    # forgetting -----------------------------------------------------------
    def forget_everything(self) -> None:
        """Erase what the line knows about the owner. The vault and schedules stay; they are managed separately."""
        with self._connect() as db:
            for table in ("personal_info", "memories", "workstreams", "sessions", "grants", "trusted_tools"):
                db.execute(f"DELETE FROM {table}")

    # vault metadata (secrets live in the keychain) ----------------------
    def vault_items(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM vault_items ORDER BY created_at").fetchall()
        return [_vault_item(row) for row in rows]

    def vault_item(self, handle: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM vault_items WHERE handle=?", (handle,)).fetchone()
        if row is None:
            raise StoreError(f"no vault item {handle}")
        return _vault_item(row)

    def add_vault_item(self, *, kind: str, label: str, origins: list[str], hint: str) -> dict[str, Any]:
        if kind not in VAULT_KINDS:
            raise StoreError(f"vault kind must be one of {', '.join(VAULT_KINDS)}")
        handle = _id(kind)
        with self._connect() as db:
            db.execute(
                "INSERT INTO vault_items VALUES (?, ?, ?, ?, ?, ?)",
                (handle, kind, label, json.dumps(origins), hint, time.time()),
            )
        return self.vault_item(handle)

    def remove_vault_item(self, handle: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM vault_items WHERE handle=?", (handle,)).rowcount > 0


def _workstream(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        **json.loads(row["body"]),
    }


def _schedule(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "title": row["title"],
        "prompt": row["prompt"],
        "rule": json.loads(row["rule"]),
        "next_run_at": row["next_run_at"],
        "status": row["status"],
        "last_run_at": row["last_run_at"],
    }


def _vault_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "handle": row["handle"],
        "kind": row["kind"],
        "label": row["label"],
        "origins": json.loads(row["origins"]),
        "hint": row["hint"],
    }
