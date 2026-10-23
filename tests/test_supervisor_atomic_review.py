"""A review cannot durably resolve an input without its completed-step state."""

import json
import multiprocessing
import os
import sqlite3

import pytest
from test_supervisor_store import reserve

from agentrun.supervisor_store import RunStore


def crash_review(path, *, after_commit):
    with RunStore(path, "run") as store:
        attempt = store.attempts(unresolved_only=True)[0]["id"]
        if not after_commit:
            original = store._change_attempt_in_transaction

            def terminate_before_commit(*args, **kwargs):
                original(*args, **kwargs)
                os._exit(23)

            store._change_attempt_in_transaction = terminate_before_commit
        store.save_state_with_reconciliations(
            {"completed_step": True, "worker": {"model_calls": 4}},
            [{"attempt_id": attempt, "evidence_refs": ["observed:confirmation"]}],
        )
        os._exit(23)


@pytest.mark.parametrize("dispatched", [False, True])
def test_atomic_review_preserves_state_journal_and_idempotence_after_restart(tmp_path, dispatched):
    path = tmp_path / "state.sqlite"
    completed = {"completed_steps": {"submit": {"evidence_refs": ["observed:confirmation"]}},
                 "worker": {"model_calls": 4}, "elapsed_seconds": 12.5}
    with RunStore(path, "run") as store:
        store.save_state({"completed_steps": {}})
        attempt = reserve(store)
        if dispatched:
            store.mark_dispatched(attempt, {"step": 1, "operation": "CLICK"})
        reconciliations = [{"attempt_id": attempt, "evidence_refs": ["observed:confirmation", "observed:confirmation"]}]
        store.save_state_with_reconciliations(completed, reconciliations)
        record = store.attempts()[0]
        assert record["state"] == "resolved" and record["evidence_refs"] == ["observed:confirmation"]
        assert record["history"] == ({"step": 1, "operation": "CLICK"} if dispatched else None)
        store.save_state_with_reconciliations(completed, reconciliations)
        assert store.attempts()[0] == record
    with RunStore(path, "run") as reopened:
        assert reopened.load_state() == completed
        assert reopened.attempts(unresolved_only=True) == []
        assert reopened.attempts()[0] == record


@pytest.mark.parametrize("second", ["missing", "rejected", "other_run"])
def test_second_reconciliation_failure_rolls_back_state_and_first_attempt(tmp_path, second):
    path = tmp_path / "state.sqlite"
    other = "missing"
    if second == "other_run":
        with RunStore(path, "other") as unrelated:
            other = reserve(unrelated)
    before = {"completed_steps": {}, "worker": {"model_calls": 8}}
    with RunStore(path, "run") as store:
        store.save_state(before)
        if second == "rejected":
            other = reserve(store)
            store.mark_rejected(other)
        attempt = reserve(store)
        records = store.attempts()
        with pytest.raises((ValueError, KeyError)):
            store.save_state_with_reconciliations(
                {"completed_steps": {"submit": True}},
                [{"attempt_id": attempt, "evidence_refs": ["observed:confirmation"]},
                 {"attempt_id": other, "evidence_refs": ["observed:confirmation"]}],
            )
        assert store.load_state() == before
        assert store.attempts() == records
        assert store._connection.in_transaction is False
    with RunStore(path, "run") as reopened:
        assert reopened.load_state() == before
        assert reopened.attempts() == records


def test_external_reader_cannot_see_resolved_attempt_without_completed_state(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    with RunStore(path, "run") as store:
        store.save_state({"complete": False})
        attempt = reserve(store)
        original = store._change_attempt_in_transaction
        snapshots = []

        def inspect_uncommitted_change(*args, **kwargs):
            original(*args, **kwargs)
            with sqlite3.connect(path) as reader:
                state = json.loads(reader.execute("SELECT state_json FROM supervisor_runs").fetchone()[0])
                status = reader.execute("SELECT state FROM supervisor_attempts WHERE id=?", (attempt,)).fetchone()[0]
                snapshots.append((state, status))

        monkeypatch.setattr(store, "_change_attempt_in_transaction", inspect_uncommitted_change)
        store.save_state_with_reconciliations(
            {"complete": True}, [{"attempt_id": attempt, "evidence_refs": ["observed:confirmation"]}],
        )
        assert snapshots == [({"complete": False}, "started")]
        assert store.load_state() == {"complete": True}
        assert store.attempts()[0]["state"] == "resolved"


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_exit_reopens_only_a_complete_old_or_new_review(tmp_path, after_commit):
    path = tmp_path / "state.sqlite"
    with RunStore(path, "run") as store:
        store.save_state({"completed_step": False, "worker": {"model_calls": 4}})
        attempt = reserve(store)
    process = multiprocessing.get_context("spawn").Process(
        target=crash_review, args=(path,), kwargs={"after_commit": after_commit},
    )
    process.start()
    process.join(15)
    assert process.exitcode == 23
    with RunStore(path, "run") as reopened:
        assert reopened.load_state() == {"completed_step": after_commit, "worker": {"model_calls": 4}}
        record = reopened.attempts()[0]
        assert record["id"] == attempt
        assert record["state"] == ("resolved" if after_commit else "started")
        assert record["evidence_refs"] == (["observed:confirmation"] if after_commit else [])


@pytest.mark.parametrize("refs", [[], [""], ["x" * 1025], "observed:confirmation", [False]])
def test_invalid_reconciliation_proof_cannot_change_state_or_attempt(tmp_path, refs):
    with RunStore(tmp_path / "state.sqlite", "run") as store:
        store.save_state({"complete": False})
        attempt = reserve(store)
        with pytest.raises(ValueError, match="evidence references"):
            store.save_state_with_reconciliations(
                {"complete": True}, [{"attempt_id": attempt, "evidence_refs": refs}],
            )
        assert store.load_state() == {"complete": False}
        assert store.attempts()[0]["state"] == "started"


def test_atomic_review_retains_secret_validation_and_allows_an_empty_delta(tmp_path):
    with RunStore(tmp_path / "state.sqlite", "run") as store:
        store.save_state({"complete": False})
        attempt = reserve(store)
        with pytest.raises(ValueError, match="credentials"):
            store.save_state_with_reconciliations(
                {"api_key": "NEVER_PERSIST"}, [{"attempt_id": attempt, "evidence_refs": ["observed:confirmation"]}],
            )
        assert store.load_state() == {"complete": False}
        assert store.attempts()[0]["state"] == "started"
        store.save_state_with_reconciliations({"reviewed": True}, [])
        assert store.load_state() == {"reviewed": True}
        assert store.attempts()[0]["state"] == "started"
