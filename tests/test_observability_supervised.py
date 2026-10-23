"""Shared supervisor/worker traces and durable recovery without browser or APIs."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from test_agent import decision, page
from test_coordinator_recovery import GOAL, V2Supervisor, accepted_result, browser_for
from test_observability_transport import event_payload, records, trace_text

from agentrun.supervised import SupervisedAgent
from agentrun.supervisor_policy import SupervisorPolicy
from AgentBrowser import agent as worker_module
from AgentBrowser import model
from AgentBrowser.browser import fingerprint
from AgentBrowser.observability import TraceConfig, TraceContext

TARGET = "123e4567-e89b-42d3-a456-426614174000"


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No paid APIs"))))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text API")))


def browser(observed=None):
    result = browser_for(observed)
    result.target = TARGET
    return result


def click():
    return {**decision("e3"), "operation": "CLICK", "target": "2"}


def done():
    return {**decision("DONE"), "operation": "DONE", "target": None}


@pytest.mark.parametrize("verified", [False, True])
def test_shared_trace_distinguishes_worker_claim_from_verified_result(tmp_path, monkeypatch, verified):
    observed = page()
    fake = browser(observed)

    def input_effect(*_, **__):
        observed.update(text="Result visibly confirmed", title="Complete")
        observed["fingerprint"] = fingerprint(observed)

    fake.act.side_effect = input_effect
    choose = Mock(side_effect=[click(), done()])
    monkeypatch.setattr(worker_module, "choose", choose)
    reviewed_claim = []

    def review(payload):
        if not payload.get("last_result"):
            return {"next_action": "continue"}
        reviewed_claim.append(deepcopy(payload))
        assert payload["last_result"]["status"] == "claimed_done"
        assert payload["requirements"][0]["status"] == "unknown"
        existing = records(agent.trace)
        worker_result = next(event for event in existing if event["event"] == "delegation.completed")
        assert worker_result["status"] == "claimed_done"
        assert event_payload(agent.trace, worker_result)["status"] == "claimed_done"
        assert not any(event.get("status") == "done" and event["component"] == "coordinator"
                       for event in existing)
        return accepted_result(payload) if verified else {"next_action": "stop", "stop_reason": "Evidence insufficient"}

    with SupervisedAgent(page()["url"], GOAL, run_id="shared-run", state_dir=tmp_path / "state",
                         browser=fake, supervisor=V2Supervisor(review),
                         trace=TraceConfig(directory=tmp_path / "traces")) as agent:
        final = list(agent.run())[-1]
        trace = agent.trace
        assert agent.worker.trace is trace
        assert final["status"] == ("done" if verified else "unverified")
        assert final["requirements"][0]["status"] == ("satisfied" if verified else "unknown")
        attempts = agent.store.attempts()
    events = records(trace)
    assert reviewed_claim and fake.act.call_count == 1 and choose.call_count == 2
    assert {event["run_id"] for event in events} == {"shared-run"}
    assert {event["session_id"] for event in events} == {trace.session_id}
    calls = [event for event in events if event["event"] == "call.started"]
    assert sum(event["component"] == "jev" for event in calls) == final["metrics"]["jev_calls"] == 2
    assert sum(event["component"] == "supervisor" for event in calls) == final["metrics"]["supervisor_calls"]
    reserved = next(event for event in events if event["event"] == "attempt.reserved")
    mutations = [event for event in events if event["event"] == "browser.started"]
    assert len(mutations) == len(attempts) == 1
    assert mutations[0]["attempt_id"] == reserved["attempt_id"] == attempts[0]["id"]
    for name in ("attempt.dispatched", "attempt.observed", "browser.input_returned", "browser.observed"):
        assert next(event for event in events if event["event"] == name)["attempt_id"] == attempts[0]["id"]
    delegation = next(event for event in events if event["event"] == "delegation.started")
    assert reserved["delegation_id"] == delegation["delegation_id"]
    assert all(event["delegation_id"] == delegation["delegation_id"] for event in calls if event["component"] == "jev")
    reviews = [event for event in events if event["event"] == "coordinator.review_applied"]
    final_review = event_payload(trace, reviews[-1])
    assert final_review["requirements"][0]["status"] == ("satisfied" if verified else "unknown")
    if verified:
        assert final_review["requirements"][0]["evidence_refs"]
        assert final_review["task_runs"]["click-once"]["evidence_refs"]
        assert final_review["task_runs"]["click-once"]["delegation_id"] == delegation["delegation_id"]


def test_interrupt_resume_appends_session_reconciles_without_replaying_input(tmp_path, monkeypatch):
    observed = page()
    original = browser(observed)

    def interrupt_after_input(*_, **__):
        observed.update(text="Result visibly confirmed", title="Complete")
        observed["fingerprint"] = fingerprint(observed)
        raise KeyboardInterrupt()

    original.act.side_effect = interrupt_after_input
    choose = Mock(return_value=click())
    monkeypatch.setattr(worker_module, "choose", choose)
    with SupervisedAgent(page()["url"], GOAL, run_id="resumed-run", state_dir=tmp_path / "state",
                         browser=original, supervisor=V2Supervisor(),
                         trace=TraceConfig(directory=tmp_path / "traces")) as first:
        with pytest.raises(KeyboardInterrupt):
            list(first.run())
        original_trace = first.trace
        first_session = original_trace.session_id
        attempt = first.store.attempts(unresolved_only=True)[0]
        delegation_id = first.state["pending_delegation"]["id"]
        assert attempt["state"] == "started"
    original_events = records(original_trace)
    assert next(event for event in original_events if event["event"] == "browser.uncertain")["error_type"] == (
        "KeyboardInterrupt"
    )
    assert not any(event["event"] == "attempt.dispatched" for event in original_events)
    reconnect = browser(observed)
    with SupervisedAgent.resume("resumed-run", state_dir=tmp_path / "state", browser=reconnect,
                                supervisor=V2Supervisor(accepted_result)) as resumed:
        trace = resumed.trace
        assert trace.directory == original_trace.directory
        assert trace.run_id == original_trace.run_id and trace.session_id != first_session
        assert resumed.worker.trace is trace
        assert resumed.state["last_result"]["status"] == "interrupted"
        assert resumed.state["last_result"]["delegation_id"] == delegation_id
        final = list(resumed.run())[-1]
        assert final["status"] == "done"
        assert final["metrics"]["jev_calls"] == 1 and final["metrics"]["actions"] == 1
        assert resumed.store.attempts(unresolved_only=True) == []
        assert resumed.store.attempts()[0]["state"] == "resolved"
    assert original.act.call_count == choose.call_count == 1
    reconnect.act.assert_not_called()
    events = records(trace)
    second_events = [event for event in events if event["session_id"] == trace.session_id]
    assert any(event["event"] == "run.resumed" for event in second_events)
    assert not any(event["event"] == "browser.started" for event in second_events)
    assert not any(event["event"] == "call.started" and event["component"] == "jev" for event in second_events)
    reconciled = [event for event in second_events if event["event"] == "attempt.reconciled"]
    assert len(reconciled) == 1 and reconciled[0]["attempt_id"] == attempt["id"]
    assert reconciled[0]["evidence_refs"] and reconciled[0]["progress_revision"] > 0
    assert len([event for event in events if event["event"] == "attempt.reserved"]) == 1
    manifest = json.loads((trace.directory / "manifest.json").read_text())
    assert [session["session_id"] for session in manifest["sessions"]] == [first_session, trace.session_id]
    assert [session["resumed"] for session in manifest["sessions"]] == [False, True]


def test_supervisor_trace_secret_helpers_are_private_and_best_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_API_KEY", "configured-private-credential")
    sdk = ChatOpenAI(model="offline", api_key="injected-private-credential", max_retries=0)
    policy = SupervisorPolicy(model=sdk)
    assert policy.trace_secrets() == ("configured-private-credential", "injected-private-credential")
    failing_trace = Mock(register_secret=Mock(side_effect=RuntimeError("writer failed")))
    policy.register_trace_secrets(failing_trace)
    assert failing_trace.register_secret.call_count == 2
    trace = TraceContext(TraceConfig(directory=tmp_path), run_id="private")
    policy.register_trace_secrets(trace)
    with trace.call("supervisor", "review", payload={"goal": "echo injected-private-credential"}):
        pass
    assert "configured-private-credential" not in trace_text(trace)
    assert "injected-private-credential" not in trace_text(trace)
    assert not any("secret" in key for key in trace.info())


def test_injected_model_credential_reader_cannot_break_execution(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_API_KEY", "configured-private-credential")

    class ModelWithBrokenCredential:
        @property
        def openai_api_key(self):
            raise RuntimeError("Do not inspect client internals")

    policy = SupervisorPolicy(model=ModelWithBrokenCredential())
    assert policy.trace_secrets() == ("configured-private-credential",)


def test_injected_credentials_redact_initial_metadata_and_outer_supervisor_calls(tmp_path, monkeypatch):
    sdk = ChatOpenAI(model="offline", api_key="initial-injected-credential", max_retries=0)
    policy = SupervisorPolicy(model=sdk)
    with SupervisedAgent(page()["url"], GOAL + " initial-injected-credential", run_id="injected-private",
                         state_dir=tmp_path / "state", browser=browser(), supervisor=policy,
                         trace=TraceConfig(directory=tmp_path / "traces")) as agent:
        trace = agent.trace
        manifest = json.loads((trace.directory / "manifest.json").read_text())
        assert manifest["metadata"]["goal"] == GOAL + " [REDACTED]"
        # A replaced SDK credential must be registered before the outer logical
        # call captures the input; invoke's callback registration is too late.
        sdk.openai_api_key = SecretStr("rotated-injected-credential")
        invoke = Mock(return_value={"answer": "echo rotated-injected-credential"})
        monkeypatch.setattr(policy, "invoke", invoke)
        request = {"phase": "audit", "goal": "echo rotated-injected-credential"}
        assert agent._call_supervisor(request) == {"answer": "echo rotated-injected-credential"}
        invoke.assert_called_once_with(request)
    text = trace_text(trace)
    assert "initial-injected-credential" not in text and "rotated-injected-credential" not in text
    started = next(event for event in records(trace) if event["event"] == "call.started")
    assert event_payload(trace, started)["goal"] == "echo [REDACTED]"
