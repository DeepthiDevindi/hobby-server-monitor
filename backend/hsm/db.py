"""SQLite persistence (stdlib sqlite3). Every query is parameterized.

Schema changes are numbered migrations tracked in PRAGMA user_version, so
`python -m hsm.init_db` takes an empty machine to the current schema and
re-running it is a no-op.

Two processes use this file: the web API (users, sessions, audit, ...) and
the collector (only `containers` reconciliation). WAL mode + busy_timeout
make that safe.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
        -- invited: admin added the email, never signed in; active: has signed in
        status TEXT NOT NULL DEFAULT 'invited' CHECK (status IN ('invited', 'active')),
        -- NULL quota = unlimited (default for admins)
        quota_cpus INTEGER CHECK (quota_cpus IS NULL OR quota_cpus >= 0),
        quota_memory_mib INTEGER CHECK (quota_memory_mib IS NULL OR quota_memory_mib >= 0),
        quota_disk_gib INTEGER CHECK (quota_disk_gib IS NULL OR quota_disk_gib >= 0),
        invited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at INTEGER NOT NULL,
        last_login_at INTEGER
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,           -- sha256 of the cookie token
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,           -- absolute expiry
        last_seen_at INTEGER NOT NULL          -- for the idle timeout
    );
    CREATE INDEX idx_sessions_user ON sessions(user_id);
    -- One row per LXD instance, keyed by LXD's own volatile.uuid so a rename
    -- keeps owner/assignments, and a *new* container that reuses a deleted
    -- one's name does not inherit them.
    CREATE TABLE containers (
        uuid TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        cpus INTEGER,                          -- allocated limits (cache of LXD config)
        memory_mib INTEGER,
        disk_gib REAL,
        status TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL
    );
    CREATE INDEX idx_containers_owner ON containers(owner_id);
    CREATE TABLE assignments (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        container_uuid TEXT NOT NULL REFERENCES containers(uuid) ON DELETE CASCADE,
        granted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, container_uuid)
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        actor_id INTEGER,                      -- no FK: the trail outlives the user
        actor_email TEXT,
        action TEXT NOT NULL,
        target TEXT,
        detail TEXT,
        ip TEXT
    );
    CREATE INDEX idx_audit_ts ON audit_log(ts);
    """,
]


def hash_token(token: str) -> str:
    # Only hashes are stored: a copied DB file does not contain live sessions.
    return hashlib.sha256(token.encode()).hexdigest()


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN")
        try:
            for stmt in _statements(sql):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {number}")  # int from enumerate, not input
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _statements(script: str) -> Iterable[str]:
    # executescript() would auto-commit; splitting keeps a migration atomic.
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    for stmt in "\n".join(lines).split(";"):
        if stmt.strip():
            yield stmt


class Database:
    """Thin query layer over one connection, shared by the async handlers of
    a single process (the lock guards calls made from worker threads)."""

    def __init__(self, path: str) -> None:
        self.conn = connect(path)
        # Re-entrant: queries issued inside `with db.transaction()` re-acquire it.
        self._lock = threading.RLock()
        migrate(self.conn)

    def close(self) -> None:
        self.conn.close()

    def all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, args)

    def transaction(self):
        return _Tx(self)

    # ---- users -------------------------------------------------------------
    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE email = ?", (email.lower(),))

    def invite_user(self, email: str, role: str, invited_by: int | None, quota: dict[str, int | None]) -> dict[str, Any]:
        cur = self.run(
            "INSERT INTO users (email, role, status, quota_cpus, quota_memory_mib, quota_disk_gib, invited_by, created_at)"
            " VALUES (?, ?, 'invited', ?, ?, ?, ?, ?)",
            (email.lower(), role, quota.get("cpus"), quota.get("memory_mib"), quota.get("disk_gib"),
             invited_by, int(time.time())),
        )
        return self.get_user(cur.lastrowid)  # type: ignore[return-value]

    def mark_login(self, user_id: int, name: str, role: str | None = None) -> None:
        self.run(
            "UPDATE users SET name = ?, status = 'active', last_login_at = ?, role = COALESCE(?, role) WHERE id = ?",
            (name, int(time.time()), role, user_id),
        )

    def set_role(self, user_id: int, role: str) -> None:
        self.run("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    def set_quota(self, user_id: int, cpus: int | None, memory_mib: int | None, disk_gib: int | None) -> None:
        self.run(
            "UPDATE users SET quota_cpus = ?, quota_memory_mib = ?, quota_disk_gib = ? WHERE id = ?",
            (cpus, memory_mib, disk_gib, user_id),
        )

    def delete_user(self, user_id: int) -> None:
        self.run("DELETE FROM users WHERE id = ?", (user_id,))  # sessions/assignments cascade

    def list_users(self) -> list[dict[str, Any]]:
        users = self.all("SELECT * FROM users ORDER BY email")
        grants: dict[int, list[str]] = {}
        for r in self.all(
            "SELECT a.user_id, c.name FROM assignments a JOIN containers c ON c.uuid = a.container_uuid ORDER BY c.name"
        ):
            grants.setdefault(r["user_id"], []).append(r["name"])
        alloc = {r["owner_id"]: r for r in self.allocations()}
        for u in users:
            u["containers"] = grants.get(u["id"], [])
            a = alloc.get(u["id"], {})
            u["allocated"] = {k: a.get(k, 0) or 0 for k in ("cpus", "memory_mib", "disk_gib")}
        return users

    # ---- sessions ----------------------------------------------------------
    def create_session(self, token: str, user_id: int, csrf: str, ttl: int) -> None:
        now = int(time.time())
        self.run("DELETE FROM sessions WHERE expires_at <= ?", (now,))  # opportunistic cleanup
        self.run(
            "INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (hash_token(token), user_id, csrf, now, now + ttl, now),
        )

    def get_session(self, token: str, idle_seconds: int) -> dict[str, Any] | None:
        now = int(time.time())
        return self.one(
            "SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ? AND last_seen_at > ?",
            (hash_token(token), now, now - idle_seconds),
        )

    def touch_session(self, token: str) -> None:
        self.run("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (int(time.time()), hash_token(token)))

    def delete_session(self, token: str) -> None:
        self.run("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))

    def delete_user_sessions(self, user_id: int) -> None:
        self.run("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # ---- containers --------------------------------------------------------
    def container_by_name(self, name: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM containers WHERE name = ?", (name,))

    def list_containers(self) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM containers ORDER BY name")

    def record_container(self, uuid: str, name: str, owner_id: int | None, created_by: int | None,
                         cpus: int | None, memory_mib: int | None, disk_gib: float | None) -> None:
        now = int(time.time())
        self.run(
            "INSERT INTO containers (uuid, name, owner_id, cpus, memory_mib, disk_gib, status, created_by, created_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'Stopped', ?, ?, ?)"
            " ON CONFLICT(uuid) DO UPDATE SET name = excluded.name, owner_id = excluded.owner_id,"
            " cpus = excluded.cpus, memory_mib = excluded.memory_mib, disk_gib = excluded.disk_gib",
            (uuid, name, owner_id, cpus, memory_mib, disk_gib, created_by, now, now),
        )

    def update_limits(self, uuid: str, cpus: int | None, memory_mib: int | None, disk_gib: float | None) -> None:
        self.run("UPDATE containers SET cpus = ?, memory_mib = ?, disk_gib = ? WHERE uuid = ?",
                 (cpus, memory_mib, disk_gib, uuid))

    def set_owner(self, uuid: str, owner_id: int | None) -> None:
        self.run("UPDATE containers SET owner_id = ? WHERE uuid = ?", (owner_id, uuid))

    def forget_container(self, uuid: str) -> None:
        self.run("DELETE FROM containers WHERE uuid = ?", (uuid,))  # assignments cascade

    def allocations(self) -> list[dict[str, Any]]:
        """Sum of allocated limits per owner (what a quota measures)."""
        return self.all(
            "SELECT owner_id, COALESCE(SUM(cpus), 0) AS cpus, COALESCE(SUM(memory_mib), 0) AS memory_mib,"
            " COALESCE(SUM(disk_gib), 0) AS disk_gib, COUNT(*) AS containers FROM containers GROUP BY owner_id"
        )

    def allocation_of(self, owner_id: int, exclude_uuid: str | None = None) -> dict[str, float]:
        row = self.one(
            "SELECT COALESCE(SUM(cpus), 0) AS cpus, COALESCE(SUM(memory_mib), 0) AS memory_mib,"
            " COALESCE(SUM(disk_gib), 0) AS disk_gib FROM containers WHERE owner_id = ? AND uuid IS NOT ?",
            (owner_id, exclude_uuid),
        )
        return dict(row or {})

    # ---- assignments -------------------------------------------------------
    def assign(self, user_id: int, uuid: str, granted_by: int | None) -> None:
        self.run(
            "INSERT OR IGNORE INTO assignments (user_id, container_uuid, granted_by, created_at) VALUES (?, ?, ?, ?)",
            (user_id, uuid, granted_by, int(time.time())),
        )

    def unassign(self, user_id: int, uuid: str) -> bool:
        return self.run("DELETE FROM assignments WHERE user_id = ? AND container_uuid = ?", (user_id, uuid)).rowcount > 0

    def can_access(self, user_id: int, name: str) -> bool:
        """Assigned OR owner. Resolved by name -> uuid on every call."""
        return self.one(
            "SELECT 1 AS ok FROM containers c LEFT JOIN assignments a"
            " ON a.container_uuid = c.uuid AND a.user_id = ?"
            " WHERE c.name = ? AND (a.user_id IS NOT NULL OR c.owner_id = ?)",
            (user_id, name, user_id),
        ) is not None

    def accessible_names(self, user_id: int) -> set[str]:
        rows = self.all(
            "SELECT c.name FROM containers c LEFT JOIN assignments a ON a.container_uuid = c.uuid AND a.user_id = ?"
            " WHERE a.user_id IS NOT NULL OR c.owner_id = ?",
            (user_id, user_id),
        )
        return {r["name"] for r in rows}

    # ---- audit -------------------------------------------------------------
    def audit(self, action: str, actor: dict[str, Any] | None = None, target: str | None = None,
              detail: str | None = None, ip: str | None = None) -> None:
        self.run(
            "INSERT INTO audit_log (ts, actor_id, actor_email, action, target, detail, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), actor and actor["id"], actor and actor["email"], action, target, detail, ip),
        )

    def list_audit(self, limit: int, before_id: int | None) -> list[dict[str, Any]]:
        if before_id:
            return self.all("SELECT * FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        return self.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))


class _Tx:
    """`with db.transaction():` -> BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def __enter__(self) -> Database:
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, *_: Any) -> None:
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
