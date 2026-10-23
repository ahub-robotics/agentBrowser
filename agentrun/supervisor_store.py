"""Durable supervisor state and browser-attempt journal.

Checkpoints can replay a graph node. They cannot prove whether browser input
was sent before a crash. The attempt journal deliberately leaves that ambiguity
visible and prevents any further input until the caller reconciles evidence.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from AgentBrowser.evidence import evidence_view

SCHEMA_VERSION = 1
STATE_CONTRACT_VERSION = 2
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
_SECRET_KEYS = {
    "api_key", "authorization", "access_token", "refresh_token", "client_secret",
}
_ACTION_FIELDS = {
    "id", "kind", "node", "option", "label", "role", "value", "current_value",
    "href", "href_truncated", "context", "delta", "entry_id", "history_entry_id",
    "history_entry", "current_history_entry", "history_current_url",
    "checked", "selected", "pressed", "expanded", "submits_form",
}


class RunLockedError(RuntimeError):
    """Another owner currently controls the run or browser target."""


class UnresolvedAttemptError(RuntimeError):
    """A previous input may have happened and must not be repeated."""


def _json(value, *, limit=MAX_RECORD_BYTES):
    """Validate before serializing, never pickle live objects or credentials."""
    def validate(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TypeError("Persisted JSON object keys must be strings")
                normalized = key.lower()
                if normalized in _SECRET_KEYS or normalized.endswith("_api_key"):
                    raise ValueError("Provider credentials must not be persisted")
                validate(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise TypeError("Persisted state must contain JSON values only")

    validate(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("Persisted record exceeds its size limit")
    return encoded


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{name} must be a nonempty string of at most 4096 characters")
    return value


def _bounded(value, *, depth=0):
    """Bound observed evidence, making every truncation explicit."""
    if depth > 8:
        return {"omitted": "depth_limit"}
    if isinstance(value, str):
        return value if len(value) <= 8192 else {"prefix": value[:8192], "truncated": True}
    if isinstance(value, dict):
        items = list(value.items())
        result = {key: _bounded(child, depth=depth + 1) for key, child in items[:128]}
        if len(items) > 128:
            result["_omitted_keys"] = len(items) - 128
        return result
    if isinstance(value, (list, tuple)):
        result = [_bounded(child, depth=depth + 1) for child in value[:128]]
        if len(value) > 128:
            result.append({"omitted_items": len(value) - 128})
        return result
    return value


def _page_evidence(page):
    if not isinstance(page, dict):
        raise TypeError("An observed page must be a dictionary")
    return {
        "url": page.get("url", ""),
        "fingerprint": page.get("fingerprint"),
        "facts": _bounded(evidence_view(page)),
    }


class RunStore:
    """One exclusively owned run, sharing SQLite with a LangGraph checkpointer."""

    def __init__(self, path, run_id, *, lock_dir=None):
        from langgraph.checkpoint.sqlite import SqliteSaver

        self.path = Path(path).expanduser().absolute()
        self.run_id = _identifier(run_id, "run_id")
        self._mutex = threading.RLock()
        self._locks = []
        self._target_id = None
        self._closed = False
        self._connection = None
        self._checkpoint_connection = None
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_dir = Path(lock_dir) if lock_dir is not None else self.path.parent / ".supervisor-locks"
        self._lock_dir.mkdir(mode=0o700, exist_ok=True, parents=True)
        if self._lock_dir.is_symlink():
            raise ValueError("The supervisor lock directory must not be a symlink")
        self._lock_dir.chmod(0o700)
        try:
            self._acquire_lock(f"run:{self.path.resolve()}:{self.run_id}")
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            self._connection = self._connect()
            self._initialize_schema()
            # Checkpointer writes must not share a transaction with tool-side
            # journal writes. Both connections use SQLite's writer locking.
            self._checkpoint_connection = self._connect()
            self.checkpointer = SqliteSaver(self._checkpoint_connection)
        except BaseException:
            self.close()
            raise

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _acquire_lock(self, identity):
        lock_path = self._lock_dir / f"{_hash(identity)}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise RunLockedError("This run or browser target already has an active controller") from error
        except BaseException:
            os.close(descriptor)
            raise
        self._locks.append(descriptor)

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("The supervisor store is closed")

    @contextmanager
    def _transaction(self):
        with self._mutex:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def _initialize_schema(self):
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS supervisor_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "version INTEGER NOT NULL)"
            )
            rows = connection.execute("SELECT version FROM supervisor_schema").fetchall()
            if rows and (len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION):
                raise ValueError("Unsupported supervisor database schema version")
            if not rows:
                # Existing journal tables without a version cannot safely be
                # treated as a new database.
                existing = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                    "('supervisor_runs','supervisor_attempts','supervisor_delegations')"
                ).fetchone()
                if existing:
                    raise ValueError("Supervisor database schema version is missing")
                connection.execute("INSERT INTO supervisor_schema VALUES (1, ?)", (SCHEMA_VERSION,))
            connection.execute(
                "CREATE TABLE IF NOT EXISTS supervisor_runs (run_id TEXT PRIMARY KEY, state_json TEXT, "
                "target_id TEXT, updated_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS supervisor_attempts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "delegation_id TEXT NOT NULL, item_key TEXT, action_key TEXT NOT NULL, state TEXT NOT NULL, "
                "record_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS supervisor_attempts_run ON supervisor_attempts(run_id, created_at)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS supervisor_delegations (run_id TEXT NOT NULL, id TEXT NOT NULL, "
                "input_json TEXT NOT NULL, result_json TEXT, updated_at REAL NOT NULL, PRIMARY KEY(run_id,id))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO supervisor_runs(run_id,updated_at) VALUES (?,?)",
                (self.run_id, time.time()),
            )

    def bind_target(self, target_id):
        """Exclusively bind the original target; never silently adopt another."""
        target_id = _identifier(target_id, "target_id")
        with self._mutex:
            self._ensure_open()
            if self._target_id is not None:
                if self._target_id != target_id:
                    raise ValueError("This run is already bound to a different browser target")
                return
            row = self._connection.execute(
                "SELECT target_id FROM supervisor_runs WHERE run_id=?", (self.run_id,),
            ).fetchone()
            if row["target_id"] is not None and row["target_id"] != target_id:
                raise ValueError("Resume must use the original browser target")
            self._acquire_lock(f"target:{target_id}")
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE supervisor_runs SET target_id=?, updated_at=? WHERE run_id=?",
                    (target_id, time.time(), self.run_id),
                )
            self._target_id = target_id

    def save_state(self, state):
        if not isinstance(state, dict):
            raise TypeError("Run state must be a dictionary")
        payload = _json(state, limit=MAX_STATE_BYTES)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE supervisor_runs SET state_json=?,updated_at=? WHERE run_id=?",
                (payload, time.time(), self.run_id),
            )

    def save_state_with_reconciliations(self, state, reconciliations):
        """Commit reviewed outcomes and attempt acknowledgements together.

        The coordinator validates outcome semantics and reference provenance
        before calling this method. A crash can expose either the old state and
        uncertain attempts or the new state and resolved attempts, never a mix.
        """
        if not isinstance(state, dict):
            raise TypeError("Run state must be a dictionary")
        payload = _json(state, limit=MAX_STATE_BYTES)
        if not isinstance(reconciliations, list):
            raise TypeError("Reconciliations must be a list")
        updates = []
        for reconciliation in reconciliations:
            if not isinstance(reconciliation, dict) or set(reconciliation) != {"attempt_id", "evidence_refs"}:
                raise ValueError("A reconciliation requires an attempt_id and evidence_refs")
            attempt_id = _identifier(reconciliation["attempt_id"], "attempt_id")
            refs = self._reconciliation_refs(reconciliation["evidence_refs"])
            updates.append((attempt_id, refs))
        with self._transaction() as connection:
            connection.execute(
                "UPDATE supervisor_runs SET state_json=?,updated_at=? WHERE run_id=?",
                (payload, time.time(), self.run_id),
            )
            for attempt_id, refs in updates:
                self._change_attempt_in_transaction(
                    connection, attempt_id, "resolved", allowed={"started", "dispatched"},
                    updates={"evidence_refs": refs},
                )

    @staticmethod
    def _runtime_state(state):
        return isinstance(state, dict) and {"run_id", "worker", "requirements", "evidence"} <= state.keys()

    def _backup_contract(self, version):
        """Snapshot all SQLite tables, including WAL contents, before changing state."""
        backup = self.path.with_name(
            f"{self.path.name}.contract-v{version}-{uuid.uuid4().hex}.bak",
        )
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        # A second read connection sees the last committed snapshot while our
        # IMMEDIATE transaction excludes writers. Backing up the write connection
        # itself would wait on its own transaction.
        source = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return backup.name

    def load_state(self, *, migrate=False):
        with self._mutex:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT state_json FROM supervisor_runs WHERE run_id=?", (self.run_id,),
            ).fetchone()
            state = json.loads(row["state_json"]) if row and row["state_json"] is not None else None
            if not migrate or not self._runtime_state(state):
                return state
            with self._transaction() as connection:
                # Re-read under the writer lock; no journal/table is discarded.
                row = connection.execute(
                    "SELECT state_json FROM supervisor_runs WHERE run_id=?", (self.run_id,),
                ).fetchone()
                state = json.loads(row["state_json"])
                version = state.get("contract_version", 1)
                if type(version) is not int or version not in {1, STATE_CONTRACT_VERSION}:
                    raise ValueError("Unsupported supervisor state contract version")
                if version == STATE_CONTRACT_VERSION:
                    return state
                items = state.get("items", {})
                if not isinstance(items, dict):
                    raise ValueError("Legacy supervisor items must be a dictionary")
                orphans = [row["id"] for row in connection.execute(
                    "SELECT id,item_key FROM supervisor_attempts WHERE run_id=? AND state != 'rejected' "
                    "ORDER BY created_at,id", (self.run_id,),
                ) if row["item_key"] not in items]
                state.update(
                    contract_version=STATE_CONTRACT_VERSION, migration_from_version=version,
                    migration_pending=True, migration_orphan_attempts=orphans,
                )
                # Validate before creating the backup; neither failure can partly
                # update the authoritative state or its browser-attempt journal.
                _json(state, limit=MAX_STATE_BYTES)
                state["migration_backup"] = self._backup_contract(version)
                payload = _json(state, limit=MAX_STATE_BYTES)
                connection.execute(
                    "UPDATE supervisor_runs SET state_json=?,updated_at=? WHERE run_id=?",
                    (payload, time.time(), self.run_id),
                )
                return state

    def reserve_attempt(self, delegation_id, item_key, action, page, text=None):
        """Commit an attempt before browser input; unresolved input blocks all new attempts."""
        delegation_id = _identifier(delegation_id, "delegation_id")
        if item_key is not None:
            item_key = _identifier(item_key, "item_key")
        if not isinstance(action, dict):
            raise TypeError("The observed action must be a dictionary")
        if text is not None and not isinstance(text, str):
            raise TypeError("Browser text must be a string or None")
        observed = _bounded({key: value for key, value in action.items() if key in _ACTION_FIELDS})
        before = _page_evidence(page)
        scope = _bounded(action.get("context", {}))
        # Text is used for identity, but not duplicated in the attempt journal.
        # Its observed result belongs in independently gathered page evidence.
        text_hash = _hash(text) if text is not None else None
        key = _hash(_json({
            "item_key": item_key, "url": before["url"], "scope": scope,
            "action": observed, "text_sha256": text_hash,
        }))
        attempt_id = uuid.uuid4().hex
        record = {
            "id": attempt_id, "delegation_id": delegation_id, "item_key": item_key,
            "action_key": key, "state": "started", "url": before["url"], "scope": scope,
            "action": observed, "before": before, "after": None, "history": None, "evidence_refs": [],
            "text_sha256": text_hash,
        }
        payload = _json(record)
        now = time.time()
        with self._transaction() as connection:
            saved = connection.execute(
                "SELECT state_json FROM supervisor_runs WHERE run_id=?", (self.run_id,),
            ).fetchone()
            state = json.loads(saved["state_json"]) if saved and saved["state_json"] is not None else None
            if isinstance(state, dict) and state.get("migration_pending"):
                raise UnresolvedAttemptError("Migrated state needs observed reconciliation before more input")
            unresolved = connection.execute(
                "SELECT id FROM supervisor_attempts WHERE run_id=? AND state IN ('started','dispatched') LIMIT 1",
                (self.run_id,),
            ).fetchone()
            if unresolved:
                raise UnresolvedAttemptError(
                    f"Attempt {unresolved['id']} needs observed reconciliation before more input"
                )
            connection.execute(
                "INSERT INTO supervisor_attempts VALUES (?,?,?,?,?,?,?,?,?)",
                (attempt_id, self.run_id, delegation_id, item_key, key, "started", payload, now, now),
            )
        return attempt_id

    def _change_attempt(self, attempt_id, state, *, allowed, updates=None):
        with self._transaction() as connection:
            self._change_attempt_in_transaction(
                connection, attempt_id, state, allowed=allowed, updates=updates,
            )

    def _change_attempt_in_transaction(self, connection, attempt_id, state, *, allowed, updates=None):
        row = connection.execute(
            "SELECT state,record_json FROM supervisor_attempts WHERE id=? AND run_id=?",
            (attempt_id, self.run_id),
        ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        if row["state"] == state:
            return  # A repeated journal acknowledgement is harmless.
        if row["state"] not in allowed:
            raise ValueError(f"Cannot change attempt from {row['state']} to {state}")
        record = json.loads(row["record_json"])
        record.update(updates or {})
        record["state"] = state
        connection.execute(
            "UPDATE supervisor_attempts SET state=?,record_json=?,updated_at=? WHERE id=? AND run_id=?",
            (state, _json(record), time.time(), attempt_id, self.run_id),
        )

    def mark_dispatched(self, attempt_id, history_entry):
        self._change_attempt(
            attempt_id, "dispatched", allowed={"started"}, updates={"history": _bounded(history_entry)},
        )

    def mark_observed(self, attempt_id, page, history_entry):
        self._change_attempt(
            attempt_id, "observed", allowed={"dispatched"},
            updates={"after": _page_evidence(page), "history": _bounded(history_entry)},
        )

    def mark_rejected(self, attempt_id):
        """Only a caller that knows no input was sent may reject a started attempt."""
        self._change_attempt(attempt_id, "rejected", allowed={"started"})

    def attempts(self, unresolved_only=False):
        with self._mutex:
            self._ensure_open()
            query = "SELECT record_json FROM supervisor_attempts WHERE run_id=?"
            if unresolved_only:
                query += " AND state IN ('started','dispatched')"
            query += " ORDER BY created_at,id"
            return [json.loads(row["record_json"]) for row in self._connection.execute(query, (self.run_id,))]

    @staticmethod
    def _reconciliation_refs(evidence_refs):
        if (not isinstance(evidence_refs, list) or not 1 <= len(evidence_refs) <= 128
                or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 1024 for ref in evidence_refs)):
            raise ValueError("Reconciliation requires nonempty, bounded observed evidence references")
        return list(dict.fromkeys(evidence_refs))

    def reconcile(self, attempt_id, evidence_refs):
        """The supervisor must validate references and outcomes before calling this."""
        self._change_attempt(
            attempt_id, "resolved", allowed={"started", "dispatched"},
            updates={"evidence_refs": self._reconciliation_refs(evidence_refs)},
        )

    def save_delegation(self, id, input, result=None):
        id = _identifier(id, "delegation_id")
        input_json = _json(input)
        result_json = _json(result) if result is not None else None
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT input_json,result_json FROM supervisor_delegations WHERE run_id=? AND id=?", (self.run_id, id),
            ).fetchone()
            if row:
                if row["input_json"] != input_json:
                    raise ValueError("A delegation id cannot be reused with different input")
                if row["result_json"] is not None:
                    if result_json is not None and row["result_json"] != result_json:
                        raise ValueError("A completed delegation result cannot be overwritten")
                    return
                if result_json is not None:
                    connection.execute(
                        "UPDATE supervisor_delegations SET result_json=?,updated_at=? WHERE run_id=? AND id=?",
                        (result_json, time.time(), self.run_id, id),
                    )
                return
            connection.execute(
                "INSERT INTO supervisor_delegations VALUES (?,?,?,?,?)",
                (self.run_id, id, input_json, result_json, time.time()),
            )

    def get_delegation(self, id):
        with self._mutex:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT input_json,result_json FROM supervisor_delegations WHERE run_id=? AND id=?", (self.run_id, id),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": id, "input": json.loads(row["input_json"]),
                "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
            }

    def close(self):
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            try:
                for connection in (self._checkpoint_connection, self._connection):
                    if connection is not None:
                        connection.close()
            finally:
                for descriptor in reversed(self._locks):
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)
                self._locks.clear()

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
