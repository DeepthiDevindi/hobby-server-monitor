"""SQLite persistence. Every query is parameterized; no SQL is built from input."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    created_at INTEGER NOT NULL,
    last_login_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    container TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, container)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    actor_id INTEGER,
    actor_email TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_assign_container ON assignments(container);
"""


def hash_token(token: str) -> str:
    # Only hashes are stored, so a leaked DB file does not leak live sessions.
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    """One shared connection. All access happens on the event-loop thread (async
    handlers only); the lock is a guard in case a sync caller ever appears."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    # ---- users -------------------------------------------------------------
    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE email = ?", (email,))

    def create_user(self, email: str, role: str = "user", name: str = "") -> dict[str, Any]:
        cur = self._run(
            "INSERT INTO users (email, name, role, created_at) VALUES (?, ?, ?, ?)",
            (email.lower(), name, role, int(time.time())),
        )
        return self.get_user(cur.lastrowid)  # type: ignore[return-value]

    def record_login(self, email: str, name: str, bootstrap_email: str) -> dict[str, Any]:
        """Upsert on verified login. The bootstrap email is always admin;
        everyone else is created as a plain user with zero assignments."""
        email = email.lower()
        user = self.get_user_by_email(email) or self.create_user(email, name=name)
        role = "admin" if bootstrap_email and email == bootstrap_email else user["role"]
        self._run(
            "UPDATE users SET name = ?, role = ?, last_login_at = ? WHERE id = ?",
            (name, role, int(time.time()), user["id"]),
        )
        return self.get_user(user["id"])  # type: ignore[return-value]

    def set_role(self, user_id: int, role: str) -> bool:
        return self._run("UPDATE users SET role = ? WHERE id = ?", (role, user_id)).rowcount > 0

    def delete_user(self, user_id: int) -> bool:
        return self._run("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0

    def count_admins(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'")["n"]  # type: ignore[index]

    def list_users(self) -> list[dict[str, Any]]:
        users = self._all("SELECT id, email, name, role, created_at, last_login_at FROM users ORDER BY email")
        by_user: dict[int, list[str]] = {}
        for row in self._all("SELECT user_id, container FROM assignments ORDER BY container"):
            by_user.setdefault(row["user_id"], []).append(row["container"])
        for u in users:
            u["containers"] = by_user.get(u["id"], [])
        return users

    # ---- sessions ----------------------------------------------------------
    def create_session(self, token: str, user_id: int, csrf: str, ttl_seconds: int) -> None:
        now = int(time.time())
        self._run("DELETE FROM sessions WHERE expires_at <= ?", (now,))  # opportunistic cleanup
        self._run(
            "INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (hash_token(token), user_id, csrf, now, now + ttl_seconds),
        )

    def get_session(self, token: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (hash_token(token), int(time.time())),
        )

    def delete_session(self, token: str) -> None:
        self._run("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))

    def delete_user_sessions(self, user_id: int) -> None:
        self._run("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # ---- assignments -------------------------------------------------------
    def assign(self, user_id: int, container: str) -> None:
        self._run(
            "INSERT OR IGNORE INTO assignments (user_id, container, created_at) VALUES (?, ?, ?)",
            (user_id, container, int(time.time())),
        )

    def unassign(self, user_id: int, container: str) -> bool:
        return self._run(
            "DELETE FROM assignments WHERE user_id = ? AND container = ?", (user_id, container)
        ).rowcount > 0

    def is_assigned(self, user_id: int, container: str) -> bool:
        return self._one(
            "SELECT 1 AS ok FROM assignments WHERE user_id = ? AND container = ?", (user_id, container)
        ) is not None

    def assigned_containers(self, user_id: int) -> set[str]:
        return {r["container"] for r in self._all("SELECT container FROM assignments WHERE user_id = ?", (user_id,))}

    def drop_container(self, container: str) -> None:
        self._run("DELETE FROM assignments WHERE container = ?", (container,))

    # ---- audit -------------------------------------------------------------
    def audit(
        self,
        action: str,
        actor: dict[str, Any] | None = None,
        target: str | None = None,
        detail: str | None = None,
        ip: str | None = None,
    ) -> None:
        self._run(
            "INSERT INTO audit_log (ts, actor_id, actor_email, action, target, detail, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), actor and actor["id"], actor and actor["email"], action, target, detail, ip),
        )

    def list_audit(self, limit: int, before_id: int | None) -> list[dict[str, Any]]:
        if before_id:
            return self._all("SELECT * FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        return self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
