"""A durable task queue built on Python's standard library and SQLite.

The queue stores every task in a single SQLite database file.  All state
transitions happen inside ``BEGIN IMMEDIATE`` transactions, so several
processes (each with its own :class:`TaskQueue` / connection) can safely race
for the same task and exactly one of them wins.

Lease decisions never read the wall clock.  ``claim``, ``ack`` and ``fail``
receive ``now`` from the caller instead, which makes lease expiry testable with
simulated time.  ``now`` may be a number of seconds (``int`` or ``float``) or a
:class:`datetime.datetime`; datetimes are converted with
:meth:`datetime.datetime.timestamp`.

Task states::

    pending  -> waiting to be claimed (or re-queued after a failure)
    running  -> currently leased by one worker
    done     -> acknowledged by the worker that held the lease
    failed   -> permanently failed (attempts exhausted)

A task is executable when it is ``pending`` and its ``available_at`` (if set)
is in the past, or when its lease has expired.  Each successful claim consumes
one attempt.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, NamedTuple

__all__ = ["TaskQueue", "Claim", "TaskRecord"]

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        seq              INTEGER PRIMARY KEY AUTOINCREMENT,
        id               TEXT    NOT NULL UNIQUE,
        key              TEXT    NOT NULL UNIQUE,
        payload          TEXT    NOT NULL,
        status           TEXT    NOT NULL
                             CHECK (status IN ('pending', 'running', 'done', 'failed')),
        attempts         INTEGER NOT NULL DEFAULT 0,
        max_attempts     INTEGER NOT NULL,
        available_at     REAL,
        lease_token      TEXT,
        worker_id        TEXT,
        lease_expires_at REAL,
        created_at       REAL    NOT NULL,
        updated_at       REAL    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_claimable"
    " ON tasks (status, available_at, seq)",
)


class Claim(NamedTuple):
    """The result of a successful :meth:`TaskQueue.claim` call.

    It is a named tuple (``task_id, payload, lease_token``), so it supports
    attribute access, tuple unpacking and indexing.  String keys are accepted
    as well for convenience, e.g. ``claim["task_id"]``.
    """

    task_id: str
    payload: Any
    lease_token: str

    def __getitem__(self, item):
        if isinstance(item, str):
            alias = _CLAIM_KEYS.get(item)
            if alias is None:
                raise KeyError(item)
            return getattr(self, alias)
        return tuple.__getitem__(self, item)

    def keys(self):
        return ("task_id", "payload", "lease_token")

    def items(self):
        return tuple((name, getattr(self, name)) for name in self.keys())

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    @property
    def id(self):
        return self.task_id

    @property
    def token(self):
        return self.lease_token


_CLAIM_KEYS = {
    "task_id": "task_id",
    "id": "task_id",
    "payload": "payload",
    "lease_token": "lease_token",
    "token": "lease_token",
}


class TaskRecord(dict):
    """Dict returned by :meth:`TaskQueue.get`, also readable as attributes."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _to_epoch(value: Any) -> float:
    """Convert a caller supplied ``now`` into seconds since the epoch."""

    if isinstance(value, datetime.datetime):
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("now must be a number of seconds or a datetime")
    seconds = float(value)
    if not math.isfinite(seconds):
        raise ValueError("now must be finite")
    return seconds


def _positive_seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number of seconds")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return seconds


def _encode_payload(payload: Any) -> str:
    """Serialize ``payload`` deterministically so equal content compares equal."""

    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except TypeError as exc:
        raise TypeError("payload must be JSON serialisable") from exc


class TaskQueue:
    """SQLite backed task queue.

    :param db_path: path of the SQLite database file (``":memory:"`` works for
        a throw-away queue, but note that every in-memory connection has its
        own database).
    """

    def __init__(self, db_path):
        self.db_path = os.fspath(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,  # autocommit; transactions are explicit
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.execute("PRAGMA busy_timeout = 30000")
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
                for statement in _SCHEMA:
                    self._conn.execute(statement)
        except BaseException:
            self._conn.close()
            self._conn = None
            raise

    # -- public API -----------------------------------------------------

    def enqueue(self, key, payload, max_attempts=3):
        """Submit a task and return its ID.

        ``key`` is idempotent: enqueueing the same key with the same payload
        returns the original task ID (whatever its state), while a different
        payload for an existing key raises :class:`ValueError`.

        ``max_attempts`` is the number of claims the task may consume before it
        becomes permanently failed.  ``payload`` must be JSON serialisable.
        """

        if not isinstance(key, str):
            raise TypeError("key must be a string")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be an integer >= 1")
        encoded = _encode_payload(payload)
        wall_now = time.time()

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id, payload FROM tasks WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                if row["payload"] == encoded:
                    return row["id"]
                raise ValueError(
                    f"task key {key!r} already exists with a different payload"
                )

            task_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO tasks"
                " (id, key, payload, status, attempts, max_attempts,"
                "  available_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 0, ?, NULL, ?, ?)",
                (
                    task_id,
                    key,
                    encoded,
                    PENDING,
                    max_attempts,
                    wall_now,
                    wall_now,
                ),
            )
            return task_id

    def claim(self, worker_id, now, lease_seconds):
        """Claim the earliest executable task.

        Returns a :class:`Claim` (task ID, payload and the new lease token), or
        ``None`` when no task is available.  A successful claim always takes a
        fresh token and counts as one attempt.  Expired leases are reclaimed
        here; a task whose attempts are exhausted by an expired lease becomes
        permanently failed.
        """

        if not isinstance(worker_id, str):
            raise TypeError("worker_id must be a string")
        now_ts = _to_epoch(now)
        lease = _positive_seconds(lease_seconds, "lease_seconds")

        with self._transaction() as conn:
            self._expire_leases(conn, now_ts)

            row = conn.execute(
                "SELECT seq, id, payload, attempts"
                " FROM tasks"
                " WHERE status = ?"
                "   AND attempts < max_attempts"
                "   AND (available_at IS NULL OR available_at <= ?)"
                " ORDER BY seq"
                " LIMIT 1",
                (PENDING, now_ts),
            ).fetchone()
            if row is None:
                return None

            token = uuid.uuid4().hex
            attempts = row["attempts"] + 1
            conn.execute(
                "UPDATE tasks"
                " SET status = ?, attempts = ?, lease_token = ?, worker_id = ?,"
                "     lease_expires_at = ?, available_at = NULL, updated_at = ?"
                " WHERE id = ?",
                (
                    RUNNING,
                    attempts,
                    token,
                    worker_id,
                    now_ts + lease,
                    now_ts,
                    row["id"],
                ),
            )
            return Claim(row["id"], json.loads(row["payload"]), token)

    def ack(self, task_id, lease_token, now):
        """Mark a task as done if its lease is still held by ``lease_token``.

        Returns ``True`` on success and ``False`` for unknown tasks, stale or
        expired tokens, and repeated acknowledgements.  A ``False`` return
        leaves the stored task unchanged.
        """

        now_ts = _to_epoch(now)

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, lease_token, lease_expires_at"
                " FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not _holds_lease(row, lease_token, now_ts):
                return False
            conn.execute(
                "UPDATE tasks"
                " SET status = ?, lease_token = NULL, worker_id = NULL,"
                "     lease_expires_at = NULL, available_at = NULL, updated_at = ?"
                " WHERE id = ?",
                (DONE, now_ts, task_id),
            )
            return True

    def fail(self, task_id, lease_token, now):
        """Report a failed attempt for a task held by ``lease_token``.

        When attempts remain the task is re-queued and becomes claimable
        immediately; otherwise it is marked permanently failed.  Returns
        ``True`` when the failure was recorded and ``False`` for unknown tasks,
        stale or expired tokens, and repeated reports, without changing state.
        """

        now_ts = _to_epoch(now)

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, lease_token, lease_expires_at, attempts,"
                "       max_attempts"
                " FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not _holds_lease(row, lease_token, now_ts):
                return False

            if row["attempts"] < row["max_attempts"]:
                status = PENDING
                available_at = now_ts
            else:
                status = FAILED
                available_at = None

            conn.execute(
                "UPDATE tasks"
                " SET status = ?, available_at = ?, lease_token = NULL,"
                "     worker_id = NULL, lease_expires_at = NULL, updated_at = ?"
                " WHERE id = ?",
                (status, available_at, now_ts, task_id),
            )
            return True

    def get(self, task_id):
        """Return a task's state as a dict, or ``None`` when it does not exist.

        The dict contains ``id``, ``key``, ``payload``, ``status``,
        ``attempts``, ``max_attempts``, ``worker_id``, ``available_at``,
        ``lease_expires_at``, ``created_at`` and ``updated_at``.
        """

        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT id, key, payload, status, attempts, max_attempts,"
                "       worker_id, available_at, lease_expires_at,"
                "       created_at, updated_at"
                " FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()

        if row is None:
            return None
        return TaskRecord(
            {
                "id": row["id"],
                "key": row["key"],
                "payload": json.loads(row["payload"]),
                "status": row["status"],
                "attempts": row["attempts"],
                "max_attempts": row["max_attempts"],
                "worker_id": row["worker_id"],
                "available_at": row["available_at"],
                "lease_expires_at": row["lease_expires_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    def close(self):
        """Close the underlying database connection."""

        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- internals ------------------------------------------------------

    def _ensure_open(self):
        if self._conn is None:
            raise RuntimeError("TaskQueue is closed")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @staticmethod
    def _expire_leases(conn, now_ts):
        """Resolve running tasks whose leases ended at or before ``now_ts``."""

        conn.execute(
            "UPDATE tasks"
            " SET status = ?, lease_token = NULL, worker_id = NULL,"
            "     lease_expires_at = NULL, available_at = NULL, updated_at = ?"
            " WHERE status = ?"
            "   AND lease_expires_at IS NOT NULL"
            "   AND lease_expires_at <= ?"
            "   AND attempts >= max_attempts",
            (FAILED, now_ts, RUNNING, now_ts),
        )
        conn.execute(
            "UPDATE tasks"
            " SET status = ?, available_at = ?, lease_token = NULL,"
            "     worker_id = NULL, lease_expires_at = NULL, updated_at = ?"
            " WHERE status = ?"
            "   AND lease_expires_at IS NOT NULL"
            "   AND lease_expires_at <= ?"
            "   AND attempts < max_attempts",
            (PENDING, now_ts, now_ts, RUNNING, now_ts),
        )


def _holds_lease(row, lease_token, now_ts):
    if row is None or row["status"] != RUNNING:
        return False
    if row["lease_token"] is None or row["lease_token"] != lease_token:
        return False
    expires_at = row["lease_expires_at"]
    return expires_at is not None and expires_at > now_ts
