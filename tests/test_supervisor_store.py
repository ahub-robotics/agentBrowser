"""A crashed browser mutation remains uncertain across supervisor restarts."""

import multiprocessing
import os
import sqlite3

import pytest

from agentrun.supervisor_store import RunLockedError, RunStore, UnresolvedAttemptError


def page(url="https://example.test/item/1"):
    return {
        "url": url, "title": "Observed item", "text": "Ready", "fingerprint": "before",
        "actions": [{"id": "send", "node": 1, "kind": "click", "role": "button", "label": "Send"}],
    }


def reserve(store, delegation="delegate-1"):
    observed = page()
    return store.reserve_attempt(delegation, observed["url"], observed["actions"][0], observed)


def try_lock(path, run_id, target_id, queue):
    try:
        with RunStore(path, run_id) as store:
            if target_id:
                store.bind_target(target_id)
        queue.put("acquired")
    except RunLockedError:
        queue.put("locked")


def test_persists_only_serializable_state_and_preserves_all_counters(tmp_path):
    path = tmp_path / "runs.sqlite"
    state = {"goal": "Each result", "calls": {"jev": 7, "supervisor": 2}, "pending": ["item-2"]}
    with RunStore(path, "run-1") as store:
        assert store.load_state() is None
        store.save_state(state)
        with pytest.raises(TypeError):
            store.save_state({"browser": object()})
        with pytest.raises(ValueError, match="credentials"):
            store.save_state({"config": {"SUPERVISOR_API_KEY": "never-write-this"}})
        with pytest.raises(ValueError):
            store.save_state({"elapsed": float("nan")})
        assert store.load_state() == state
    with RunStore(path, "run-1") as reopened:
        assert reopened.load_state() == state
    with RunStore(path, "other-run") as unrelated:
        assert unrelated.load_state() is None
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / ".supervisor-locks").st_mode & 0o777 == 0o700
    assert b"never-write-this" not in path.read_bytes()


@pytest.mark.parametrize("dispatched", [False, True])
def test_crash_before_observation_never_allows_a_new_subtask_to_retry(tmp_path, dispatched):
    path = tmp_path / "runs.sqlite"
    with RunStore(path, "run-1") as store:
        attempt = reserve(store)
        if dispatched:
            store.mark_dispatched(attempt, {"step": 1, "kind": "click"})
    with RunStore(path, "run-1") as resumed:
        uncertain = resumed.attempts(unresolved_only=True)
        assert len(uncertain) == 1
        assert uncertain[0]["id"] == attempt
        assert uncertain[0]["state"] == ("dispatched" if dispatched else "started")
        with pytest.raises(UnresolvedAttemptError):
            reserve(resumed, delegation="new-instruction")
        with pytest.raises(ValueError):
            resumed.reconcile(attempt, [])
        resumed.reconcile(attempt, ["observed:success"])
        assert resumed.attempts(unresolved_only=True) == []
        assert resumed.attempts()[0]["evidence_refs"] == ["observed:success"]
        assert reserve(resumed, delegation="next-item") != attempt


def test_attempt_is_durable_before_return_and_state_changes_cannot_erase_uncertainty(tmp_path):
    path = tmp_path / "runs.sqlite"
    with RunStore(path, "run-1") as store:
        attempt = reserve(store)
        with sqlite3.connect(path) as reader:
            saved = reader.execute("SELECT state FROM supervisor_attempts WHERE id=?", (attempt,)).fetchone()
            assert saved == ("started",)
        store.save_state({"status": "ready", "subtask": "new-plan"})
        assert store.attempts(unresolved_only=True)[0]["id"] == attempt
        with pytest.raises(ValueError, match="started to observed"):
            store.mark_observed(attempt, page(), {})
        store.mark_dispatched(attempt, {"step": 1})
        with pytest.raises(ValueError, match="dispatched to rejected"):
            store.mark_rejected(attempt)
        store.mark_observed(attempt, page("https://example.test/success"), {"step": 1, "page_changed": True})
        assert store.attempts(unresolved_only=True) == []
        record = store.attempts()[0]
        assert record["state"] == "observed"
        assert record["url"] == "https://example.test/item/1"
        assert record["after"]["url"] == "https://example.test/success"
        store.mark_observed(attempt, page(), {})
        assert store.attempts()[0] == record
        with pytest.raises(ValueError):
            store.mark_dispatched(attempt, {})


def test_rejected_before_input_releases_gate_and_scopes_are_recorded(tmp_path):
    with RunStore(tmp_path / "runs.sqlite", "run-1") as store:
        observed = page()
        action = {**observed["actions"][0], "context": {"id": "panel", "heading": "Contact"}}
        attempt = store.reserve_attempt("one", "item-1", action, observed, text="Sensitive generated input")
        record = store.attempts()[0]
        assert record["scope"] == action["context"]
        assert record["item_key"] == "item-1"
        assert len(record["action_key"]) == 64
        assert "Sensitive generated input" not in str(record)
        store.mark_rejected(attempt)
        assert store.attempts(unresolved_only=True) == []
        reserve(store)


def test_unknown_attempt_and_other_run_attempt_cannot_be_modified(tmp_path):
    path = tmp_path / "runs.sqlite"
    with RunStore(path, "one") as store:
        attempt = reserve(store)
    with RunStore(path, "two") as store:
        with pytest.raises(KeyError):
            store.mark_rejected(attempt)
        assert store.attempts() == []


def test_delegation_ids_are_idempotent_and_cannot_hide_changed_input_or_result(tmp_path):
    path = tmp_path / "runs.sqlite"
    instruction = {"instruction": "Open item", "criteria": ["Detail is visible"]}
    with RunStore(path, "run-1") as store:
        assert store.get_delegation("one") is None
        store.save_delegation("one", instruction)
        assert store.get_delegation("one") == {"id": "one", "input": instruction, "result": None}
        store.save_delegation("one", instruction, {"status": "yielded"})
        store.save_delegation("one", dict(reversed(list(instruction.items()))), {"status": "yielded"})
        with pytest.raises(ValueError, match="different input"):
            store.save_delegation("one", {"instruction": "Send now"})
        with pytest.raises(ValueError, match="cannot be overwritten"):
            store.save_delegation("one", instruction, {"status": "done"})
    with RunStore(path, "run-1") as reopened:
        assert reopened.get_delegation("one")["result"] == {"status": "yielded"}
        reopened.save_delegation("one", instruction)
        assert reopened.get_delegation("one")["result"] == {"status": "yielded"}


@pytest.mark.parametrize(("other_run", "target"), [("same", None), ("different", "target-1")])
def test_process_lock_excludes_duplicate_run_or_target_owner(tmp_path, other_run, target):
    path = tmp_path / "runs.sqlite"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    with RunStore(path, "same") as store:
        store.bind_target("target-1")
        process = context.Process(target=try_lock, args=(path, other_run, target, queue))
        process.start()
        process.join(15)
        assert process.exitcode == 0
        assert queue.get(timeout=1) == "locked"
    with RunStore(path, "same") as resumed:
        resumed.bind_target("target-1")
        with pytest.raises(ValueError, match="different browser target"):
            resumed.bind_target("other-target")


def test_original_target_is_retained_after_disconnect(tmp_path):
    path = tmp_path / "runs.sqlite"
    with RunStore(path, "one") as store:
        store.bind_target("original")
        store.bind_target("original")
    with RunStore(path, "one") as resumed:
        with pytest.raises(ValueError, match="original browser target"):
            resumed.bind_target("replacement")
        resumed.bind_target("original")


def test_unknown_schema_version_does_not_reset_saved_data_or_retain_lock(tmp_path):
    path = tmp_path / "runs.sqlite"
    with RunStore(path, "one") as store:
        store.save_state({"goal": "retain this"})
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE supervisor_schema SET version=99")
    with pytest.raises(ValueError, match="schema version"):
        RunStore(path, "one")
    with sqlite3.connect(path) as connection:
        assert "retain this" in connection.execute("SELECT state_json FROM supervisor_runs").fetchone()[0]
        connection.execute("UPDATE supervisor_schema SET version=1")
    with RunStore(path, "one") as reopened:
        assert reopened.load_state() == {"goal": "retain this"}


def test_sqlite_checkpointer_is_persistent_and_separate_from_attempt_transactions(tmp_path):
    from langgraph.graph import END, START, StateGraph

    path = tmp_path / "runs.sqlite"
    config = {"configurable": {"thread_id": "one"}}
    with RunStore(path, "one") as store:
        graph = StateGraph(dict)
        graph.add_node("work", lambda state: {**state, "progress": 1})
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        compiled = graph.compile(checkpointer=store.checkpointer)
        assert compiled.invoke({"goal": "observe"}, config, durability="sync")["progress"] == 1
        reserve(store)
    with RunStore(path, "one") as resumed:
        assert resumed.checkpointer.get_tuple(config).checkpoint["channel_values"]["__root__"]["progress"] == 1
        assert len(resumed.attempts(unresolved_only=True)) == 1


def test_close_is_idempotent_and_closed_store_cannot_be_used(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite", "one")
    store.close()
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.load_state()
