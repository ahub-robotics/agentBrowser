"""Opt-in state migrations preserve journal evidence and gate further input."""

import json
import sqlite3

import pytest
from test_supervisor_store import page

from agentrun.supervisor_store import RunStore, UnresolvedAttemptError


def legacy_state():
    return {
        "run_id": "run", "worker": {"history": [{"step": 1}], "model_calls": 17},
        "requirements": [{"id": "all", "status": "unknown"}],
        "evidence": {"observed:sent": {"value": "Observed confirmation"}},
        "items": {"registered": {"status": "completed"}}, "elapsed_seconds": 37.5,
        "status": "waiting_feedback", "unknown_future_field": {"retain": [1, 2]},
    }


def attempt(store, item, *, rejected=False):
    observed = page()
    identifier = store.reserve_attempt("delegation", item, observed["actions"][0], observed)
    if rejected:
        store.mark_rejected(identifier)
    else:
        store.mark_dispatched(identifier, {"step": 1})
        store.mark_observed(identifier, observed, {"step": 1})
    return identifier


def test_reading_legacy_state_does_not_migrate_or_create_backup(tmp_path):
    state = legacy_state()
    path = tmp_path / "state.sqlite"
    with RunStore(path, "run") as store:
        store.save_state(state)
        assert store.load_state() == state
    with RunStore(path, "run") as store:
        assert store.load_state() == state
    assert list(tmp_path.glob("*.bak")) == []


def test_opt_in_migration_keeps_all_data_and_backs_up_committed_wal(tmp_path):
    state = legacy_state()
    path = tmp_path / "state.sqlite"
    with RunStore(path, "run") as store:
        orphan = attempt(store, None)
        missing = attempt(store, "missing")
        attempt(store, "registered")
        attempt(store, None, rejected=True)
        store.save_delegation("delegation", {"instruction": "Observe"}, {"status": "claimed_done"})
        store.save_state(state)
        store._connection.execute("CREATE TABLE preserved_checkpoint (data TEXT)")
        store._connection.execute("INSERT INTO preserved_checkpoint VALUES ('retain-checkpoint')")
        records = store.attempts()
        migrated = store.load_state(migrate=True)
        assert all(migrated[key] == value for key, value in state.items())
        assert migrated["contract_version"] == 2
        assert migrated["migration_from_version"] == 1
        assert migrated["migration_pending"] is True
        assert migrated["migration_orphan_attempts"] == [orphan, missing]
        backup = tmp_path / migrated["migration_backup"]
        assert backup.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(backup) as copied:
            assert json.loads(copied.execute("SELECT state_json FROM supervisor_runs").fetchone()[0]) == state
            assert copied.execute("SELECT data FROM preserved_checkpoint").fetchone() == ("retain-checkpoint",)
            assert len(copied.execute("SELECT id FROM supervisor_attempts").fetchall()) == 4
            assert copied.execute("SELECT result_json FROM supervisor_delegations").fetchone()[0]
        assert store.attempts() == records
        with pytest.raises(UnresolvedAttemptError, match="Migrated state"):
            attempt(store, "next")
        assert store.attempts() == records
        assert store.load_state(migrate=True) == migrated
    with RunStore(path, "run") as store:
        assert store.load_state(migrate=True) == migrated
        assert len(list(tmp_path.glob("*.bak"))) == 1
        migrated["migration_pending"] = False
        store.save_state(migrated)
        attempt(store, "next")


def test_backup_failure_rolls_back_migration_and_keeps_legacy_state(tmp_path, monkeypatch):
    state = legacy_state()
    with RunStore(tmp_path / "state.sqlite", "run") as store:
        store.save_state(state)

        def fail_backup(version):
            raise OSError("Simulated backup failure")

        monkeypatch.setattr(store, "_backup_contract", fail_backup)
        with pytest.raises(OSError, match="backup failure"):
            store.load_state(migrate=True)
        assert store.load_state() == state
        assert store._connection.in_transaction is False


@pytest.mark.parametrize("version", [0, 3, "2", True])
def test_unknown_contract_is_rejected_without_changing_state(tmp_path, version):
    state = {**legacy_state(), "contract_version": version}
    with RunStore(tmp_path / "state.sqlite", "run") as store:
        store.save_state(state)
        with pytest.raises(ValueError, match="contract version"):
            store.load_state(migrate=True)
        assert store.load_state() == state
    assert list(tmp_path.glob("*.bak")) == []


def test_fresh_v2_state_needs_no_migration_and_preserves_unresolved_attempt_gate(tmp_path):
    state = {**legacy_state(), "contract_version": 2}
    with RunStore(tmp_path / "state.sqlite", "run") as store:
        store.save_state(state)
        assert store.load_state(migrate=True) == state
        observed = page()
        store.reserve_attempt("delegation", None, observed["actions"][0], observed)
        with pytest.raises(UnresolvedAttemptError):
            attempt(store, "next")
    assert list(tmp_path.glob("*.bak")) == []
