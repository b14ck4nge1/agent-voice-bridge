"""SQLite persistence for approval requests, decisions and the audit log.

One connection per operation keeps this safe to call from FastAPI's threadpool and
from the event loop. All writes that change a request's state go through
:meth:`Store.transition`, which re-checks the state machine inside a transaction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_voice_bridge.approvals.state_machine import InvalidTransition, check_transition
from agent_voice_bridge.models import ApprovalState
from agent_voice_bridge.security.redaction import redact

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS approval_requests (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    session_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    project_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_input_redacted TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT,
    turn_id TEXT,
    risk TEXT NOT NULL,
    risk_reasons TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    call_attempts INTEGER NOT NULL DEFAULT 0,
    active_call_id TEXT,
    decision TEXT,
    decision_message TEXT,
    UNIQUE (agent, session_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_requests_state ON approval_requests (state);

CREATE TABLE IF NOT EXISTS decisions (
    nonce TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES approval_requests (id),
    decision TEXT NOT NULL,
    call_id TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS used_totp_counters (counter INTEGER PRIMARY KEY, used_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    request_id TEXT,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("Store needs a file path; each operation opens its own connection")
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # executescript() manages its own transaction, so it runs outside _conn().
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        finally:
            conn.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                conn.close()

    # -- requests -----------------------------------------------------------

    def insert_request(self, row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert a request; on an idempotency-key collision return the existing row."""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM approval_requests WHERE agent = ? AND session_id = ? AND idempotency_key = ?",
                (row["agent"], row["session_id"], row["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                return dict(existing), False
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            sql = f"INSERT INTO approval_requests ({cols}) VALUES ({marks})"  # noqa: S608 - internal column names
            conn.execute(sql, tuple(row.values()))
            self._audit(conn, row["id"], "agent", "created", {"state": row["state"]})
            return row, True

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(r) if r else None

    def list_requests(self, states: list[ApprovalState]) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in states)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM approval_requests WHERE state IN ({marks}) ORDER BY created_at",  # noqa: S608
                tuple(s.value for s in states),
            ).fetchall()
            return [dict(r) for r in rows]

    def transition(
        self,
        request_id: str,
        target: ApprovalState,
        *,
        actor: str,
        event: str,
        details: dict[str, Any] | None = None,
        expect: set[ApprovalState] | None = None,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically move a request to ``target``. Raises InvalidTransition or KeyError."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            current = ApprovalState(row["state"])
            if expect is not None and current not in expect:
                raise InvalidTransition(current, target)
            if current != target:
                check_transition(current, target)
            fields = {"state": target.value, "updated_at": iso(utcnow()), **(updates or {})}
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE approval_requests SET {assignments} WHERE id = ?",  # noqa: S608
                (*fields.values(), request_id),
            )
            self._audit(
                conn,
                request_id,
                actor,
                event,
                {"from": current.value, "to": target.value, **(details or {})},
            )
            updated = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(updated)

    # -- decisions ------------------------------------------------------------

    def get_decision(self, nonce: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM decisions WHERE nonce = ?", (nonce,)).fetchone()
            return dict(r) if r else None

    def record_decision(
        self,
        *,
        request_id: str,
        nonce: str,
        decision: str,
        call_id: str,
        message: str | None,
        target: ApprovalState,
        totp_counter: int | None,
    ) -> dict[str, Any]:
        """Store a decision and apply the terminal transition in one transaction."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            current = ApprovalState(row["state"])
            check_transition(current, target)
            if totp_counter is not None:
                # PRIMARY KEY makes a reused code fail with IntegrityError.
                conn.execute(
                    "INSERT INTO used_totp_counters (counter, used_at) VALUES (?, ?)",
                    (totp_counter, iso(utcnow())),
                )
            now = iso(utcnow())
            conn.execute(
                "INSERT INTO decisions (nonce, request_id, decision, call_id, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (nonce, request_id, decision, call_id, message, now),
            )
            conn.execute(
                "UPDATE approval_requests SET state = ?, decision = ?, decision_message = ?, updated_at = ? "
                "WHERE id = ?",
                (target.value, decision, message, now, request_id),
            )
            self._audit(
                conn,
                request_id,
                "voice",
                "decision",
                {
                    "from": current.value,
                    "to": target.value,
                    "decision": decision,
                    "call_id": call_id,
                    "second_factor": totp_counter is not None,
                    "message": message,
                },
            )
            updated = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(updated)

    def totp_counter_used(self, counter: int) -> bool:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT 1 FROM used_totp_counters WHERE counter = ?", (counter,)
            ).fetchone()
            return r is not None

    # -- audit ----------------------------------------------------------------

    def audit(
        self, request_id: str | None, actor: str, event: str, details: dict[str, Any]
    ) -> None:
        with self._conn() as conn:
            self._audit(conn, request_id, actor, event, details)

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        request_id: str | None,
        actor: str,
        event: str,
        details: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log (ts, request_id, actor, event, details) VALUES (?, ?, ?, ?, ?)",
            (iso(utcnow()), request_id, actor, event, json.dumps(redact(details), default=str)),
        )

    def audit_events(self, request_id: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if request_id is None:
                rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE request_id = ? ORDER BY seq", (request_id,)
                ).fetchall()
            return [dict(r) | {"details": json.loads(r["details"])} for r in rows]
