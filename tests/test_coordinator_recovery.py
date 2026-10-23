"""Public v2 recovery preserves mutations, atomic reviews and technical stop states."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import decision, page

from agentrun.supervised import LegacySupervisedAgent, SupervisedAgent, SupervisorLimits
from AgentBrowser import agent as worker_module
from AgentBrowser import model
from AgentBrowser.browser import fingerprint

GOAL = "Click the observed Go button once and verify the result."
CRITERION = "The result is visibly confirmed"


def contract(*, loop=False):
    template = {
        "id": "click-once", "instruction": GOAL, "success_criteria": [CRITERION],
        "requirement_ids": ["result"], "goal_quote": GOAL,
    }
    result = {
        "version": 2, "requirements": [{
            "id": "result", "goal_quote": GOAL, "description": CRITERION,
            "status": "unknown", "evidence_refs": [],
        }], "tasks": [], "loops": [], "execution_constraints": ["Never repeat a completed input."],
    }
    if loop:
        result["loops"] = [{
            "id": "results", "goal_quote": GOAL, "description": "One observed result",
            "requirement_ids": ["result"], "depends_on": [], "limit": 1, "order": "observed",
            "discovery": {**template, "id": "discover"}, "steps": [template],
        }]
    else:
        result["tasks"] = [{**template, "depends_on": []}]
    return result


def browser_for(observed=None):
    observed = observed or page()
    return Mock(target="recovery-tab", fresh=Mock(return_value=True),
                observe=Mock(side_effect=lambda **_: deepcopy(observed)))


class V2Supervisor:
    """A phase-specific callable using the same strict validation as real tools."""

    def __init__(self, review=None, *, plan=None):
        self.review = review or (lambda _: {"next_action": "continue"})
        self.plan = plan or contract()
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(deepcopy(payload))
        assert payload["contract_version"] == 2
        if payload["phase"] == "initialize":
            return {"contract_version": 2, "phase": "initialize", "contract": self.plan}
        if payload["phase"] == "audit":
            return {"contract_version": 2, "phase": "audit", "approved": True}
        return {"contract_version": 2, "phase": "review", **self.review(payload)}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No paid APIs"))))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text API")))
    monkeypatch.setattr(worker_module, "choose", Mock(return_value={
        **decision("DONE"), "operation": "DONE", "target": None,
    }))


def ready_agent(tmp_path, supervisor=None, browser=None, **kwargs):
    return SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, run_id="recovery",
                           supervisor=supervisor or V2Supervisor(), browser=browser or browser_for(), **kwargs)


def initialize(agent):
    agent._decide()
    agent._decide()
    assert agent.state["contract_phase"] == "review"


def accepted_result(payload):
    last = payload.get("last_result")
    if not last:
        return {"next_action": "continue"}
    refs = [fact["ref"] for fact in payload["evidence"] if fact.get("kind") == "page"][:1]
    return {
        "next_action": "continue",
        "requirement_updates": [{"id": "result", "status": "satisfied", "evidence_refs": refs}],
        "subtask_review": {"delegation_id": last["delegation_id"], "status": "satisfied", "evidence_refs": refs},
        "reconcile_attempts": [{"attempt_id": item["id"], "evidence_refs": refs}
                               for item in payload["unresolved_attempts"]],
    }


def test_interruption_after_input_resumes_without_repeating_the_mutation(tmp_path, monkeypatch):
    observed = page()
    browser = browser_for(observed)

    def interrupt_after_input(*_, **__):
        observed.update(text="Result visibly confirmed", title="Complete")
        observed["fingerprint"] = fingerprint(observed)
        raise KeyboardInterrupt()

    browser.act.side_effect = interrupt_after_input
    choose = Mock(return_value={**decision("e3"), "operation": "CLICK", "target": "2"})
    monkeypatch.setattr(worker_module, "choose", choose)
    with ready_agent(tmp_path, browser=browser) as agent:
        with pytest.raises(KeyboardInterrupt):
            list(agent.run())
        attempt = agent.store.attempts(unresolved_only=True)[0]
        assert attempt["state"] == "started"
        pending = deepcopy(agent.state["pending_delegation"])
        before = agent.snapshot()["metrics"]
    browser.act.assert_called_once()
    reconnect = browser_for(observed)
    with SupervisedAgent.resume("recovery", state_dir=tmp_path, browser=reconnect,
                                supervisor=V2Supervisor(accepted_result)) as resumed:
        assert resumed.state["pending_delegation"] is None
        assert resumed.state["last_result"]["delegation_id"] == pending["id"]
        result = list(resumed.run())[-1]
        assert result["status"] == "done"
        assert result["metrics"]["jev_calls"] == before["jev_calls"]
        assert resumed.store.attempts(unresolved_only=True) == []
        assert len(resumed.store.attempts()) == 1
    reconnect.act.assert_not_called()
    assert choose.call_count == 1


def test_crash_after_atomic_review_before_dispatch_does_not_replay_confirmed_input(tmp_path, monkeypatch):
    observed = page()
    browser = browser_for(observed)

    def uncertain_input(*_, **__):
        observed.update(text="Result visibly confirmed", title="Complete")
        observed["fingerprint"] = fingerprint(observed)
        raise RuntimeError("Input completed but transport confirmation was lost")

    browser.act.side_effect = uncertain_input
    choose = Mock(return_value={**decision("e3"), "operation": "CLICK", "target": "2"})
    monkeypatch.setattr(worker_module, "choose", choose)
    with ready_agent(tmp_path, browser=browser, supervisor=V2Supervisor(accepted_result)) as agent:
        dispatch = agent._dispatch_review

        def crash_before_dispatch(review, payload):
            if review["reconcile_attempts"]:
                # Assert durability before run() cleanup can perform another save.
                stored = agent.store.load_state()
                assert "click-once" in stored["task_runs"]
                assert stored["active_work"] is None
                assert stored["requirements"][0]["status"] == "satisfied"
                assert stored["worker"]["model_calls"] == 1
                assert agent.store.attempts(unresolved_only=True) == []
                raise SystemExit("Simulated exit after atomic review commit")
            return dispatch(review, payload)

        monkeypatch.setattr(agent, "_dispatch_review", crash_before_dispatch)
        with pytest.raises(SystemExit, match="atomic review commit"):
            list(agent.run())
        records = agent.store.attempts()
        assert len(records) == 1 and records[0]["state"] == "resolved"
        assert agent.state["pending_delegation"] is None
    reconnect = browser_for(observed)
    with SupervisedAgent.resume("recovery", state_dir=tmp_path, browser=reconnect,
                                supervisor=V2Supervisor(accepted_result)) as resumed:
        result = list(resumed.run())[-1]
        assert result["status"] == "done"
        assert result["metrics"]["jev_calls"] == result["metrics"]["actions"] == 1
        assert resumed.state["delegation_sequence"] == 1
        assert resumed.store.attempts() == records
    browser.act.assert_called_once()
    reconnect.act.assert_not_called()
    assert choose.call_count == 1


@pytest.mark.parametrize("failure", ["during_input", "after_input"])
def test_uncertain_effect_blocks_further_input_across_resume(tmp_path, monkeypatch, failure):
    observed = page()
    browser = browser_for(observed)
    if failure == "during_input":
        browser.act.side_effect = RuntimeError("Transport lost after possible input")
    else:
        def observe(**_):
            if browser.act.call_count:
                raise RuntimeError("Postcondition unavailable")
            return deepcopy(observed)

        browser.observe.side_effect = observe
    choose = Mock(return_value={**decision("e3"), "operation": "CLICK", "target": "2"})
    monkeypatch.setattr(worker_module, "choose", choose)
    with ready_agent(tmp_path, browser=browser) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == ("unverified" if failure == "during_input" else "blocked")
        assert result["stop_reason"] == ("uncertain_action" if failure == "during_input" else "observation_unavailable")
        assert result["question"] == ""
        uncertain = agent.store.attempts(unresolved_only=True)
        assert len(uncertain) == 1
        assert uncertain[0]["state"] == ("started" if failure == "during_input" else "dispatched")
        before = result["metrics"]
    browser.act.assert_called_once()
    reconnect = browser_for(observed)
    with SupervisedAgent.resume("recovery", state_dir=tmp_path, browser=reconnect,
                                supervisor=V2Supervisor()) as resumed:
        result = list(resumed.run())[-1]
        assert result["status"] == "unverified"
        assert result["stop_reason"] == "uncertain_action"
        assert result["metrics"]["jev_calls"] == before["jev_calls"] == 1
        assert result["metrics"]["actions"] == before["actions"] == 1
        assert resumed.store.attempts(unresolved_only=True) == uncertain
    reconnect.act.assert_not_called()
    assert choose.call_count == 1


@pytest.mark.parametrize("saved_result", [False, True])
def test_pending_delegation_is_reviewed_instead_of_replayed_on_resume(tmp_path, saved_result):
    with ready_agent(tmp_path) as agent:
        initialize(agent)
        agent._decide()
        pending = deepcopy(agent.state["pending_delegation"])
        agent._record_page(page())
        if saved_result:
            result = agent._worker_result("claimed_done")
            agent.store.save_delegation(pending["id"], pending["input"], result)
    reconnect = browser_for()
    with SupervisedAgent.resume("recovery", state_dir=tmp_path, browser=reconnect,
                                supervisor=V2Supervisor(accepted_result)) as resumed:
        assert resumed.state["pending_delegation"] is None
        assert resumed.state["last_result"]["status"] == ("claimed_done" if saved_result else "interrupted")
        assert list(resumed.run())[-1]["status"] == "done"
        assert resumed.state["delegation_sequence"] == 1
    reconnect.act.assert_not_called()
    worker_module.choose.assert_not_called()


@pytest.mark.parametrize("reads_fail", [False, True])
def test_unverified_step_gets_only_two_read_only_recoveries(tmp_path, reads_fail):
    with ready_agent(tmp_path) as agent:
        initialize(agent)
        agent._decide()
        agent._delegate()
        before = agent.browser.observe.call_count
        if reads_fail:
            agent.browser.observe.side_effect = RuntimeError("Read unavailable")
        result = list(agent.run())[-1]
        assert agent.state["read_recoveries"] == 2
        assert agent.browser.observe.call_count - before == 2
        assert result["status"] == ("blocked" if reads_fail else "unverified")
        assert result["stop_reason"] == ("observation_unavailable" if reads_fail else "step_not_verified")
        assert result["question"] == ""
        agent.browser.act.assert_not_called()
        assert worker_module.choose.call_count == 1


def test_rejected_repair_cannot_commit_valid_deltas_or_attempt_reconciliation(tmp_path):
    with ready_agent(tmp_path) as agent:
        initialize(agent)
        agent._decide()
        observed = page()
        pending = agent.state["pending_delegation"]
        attempt = agent.store.reserve_attempt(pending["id"], None, observed["actions"][2], observed)
        agent.state["attempt_observations"][attempt] = agent.state["observation_seq"]
        agent._record_page(observed)
        agent.state["last_result"] = agent._worker_result("interrupted")
        agent.state["pending_delegation"] = None

        def invalid_repair(payload):
            ref = payload["current_evidence_refs"][0]
            return {
                "next_action": "repair", "repair": {
                    "instruction": "Try a different route", "success_criteria": ["Weaker replacement criterion"],
                }, "requirement_updates": [{"id": "result", "status": "satisfied", "evidence_refs": [ref]}],
                "reconcile_attempts": [{"attempt_id": attempt, "evidence_refs": [ref]}],
            }

        agent.supervisor = V2Supervisor(invalid_repair)
        result = list(agent.run())[-1]
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert agent.state["requirements"][0]["status"] == "unknown"
        assert agent.store.load_state()["requirements"][0]["status"] == "unknown"
        assert [item["id"] for item in agent.store.attempts(unresolved_only=True)] == [attempt]
        assert agent.state["delegation_sequence"] == 1
        agent.browser.act.assert_not_called()


def test_runtime_rejection_of_loop_completion_rolls_back_other_deltas(tmp_path):
    def invalid_completion(payload):
        refs = payload["current_evidence_refs"][:1]
        return {
            "next_action": "continue",
            "requirement_updates": [{"id": "result", "status": "satisfied", "evidence_refs": refs}],
            "loop_reviews": [{"loop_id": "results", "exhausted": True, "evidence_refs": refs}],
        }

    with ready_agent(tmp_path, V2Supervisor(invalid_completion, plan=contract(loop=True))) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "unverified"
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert agent.state["requirements"][0]["status"] == "unknown"
        assert agent.state["loops"]["results"]["exhausted"] is False
        assert agent.store.attempts() == []
        agent.browser.act.assert_not_called()


@pytest.mark.parametrize("failure", ["provider", "invalid_response", "budget"])
def test_technical_supervisor_failures_do_not_ask_the_user(tmp_path, failure):
    def review(_):
        if failure == "provider":
            raise model.ProviderError("request", status_code=400)
        return {"unexpected": "invalid"}

    limits = SupervisorLimits(max_supervisor_calls=1) if failure == "budget" else SupervisorLimits()
    with ready_agent(tmp_path, V2Supervisor(review), limits=limits) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == ("unverified" if failure == "invalid_response" else "blocked")
        assert result["stop_reason"] == {
            "provider": "supervisor_provider_error", "invalid_response": "supervisor_invalid_response",
            "budget": "supervisor_budget",
        }[failure]
        assert result["question"] == ""
        agent.browser.act.assert_not_called()


def test_worker_provider_failure_preserves_diagnostic_and_has_technical_status(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=model.ProviderError(
        "request", status_code=400, diagnostic={"component": "jev", "request_bytes": 123},
    )))

    def review(payload):
        if payload.get("last_result"):
            return {"next_action": "stop", "stop_reason": "Native provider rejected its request"}
        return {"next_action": "continue"}

    with ready_agent(tmp_path, V2Supervisor(review)) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "blocked"
        assert result["stop_reason"] == "worker_provider_error"
        assert result["question"] == ""
        assert agent.state["last_result"]["provider_error"]["provider_http_status"] == 400
        assert agent.state["last_result"]["provider_error"]["provider_diagnostic"]["request_bytes"] == 123
        assert worker_module.choose.call_count == 1
        agent.browser.act.assert_not_called()


def test_legacy_orphan_effect_is_migrated_and_stopped_before_new_input(tmp_path):
    with LegacySupervisedAgent(page()["url"], GOAL, run_id="legacy", state_dir=tmp_path,
                               browser=browser_for(), supervisor=Mock()) as legacy:
        observed = page()
        attempt = legacy.store.reserve_attempt("old-delegation", None, observed["actions"][2], observed)
        legacy.store.mark_dispatched(attempt, {"step": 1, "operation": "CLICK"})
        legacy.store.mark_observed(attempt, observed, {"step": 1, "operation": "CLICK"})
        records = legacy.store.attempts()
    reconnect = browser_for()
    with SupervisedAgent.resume("legacy", state_dir=tmp_path, supervisor=V2Supervisor(),
                                browser=reconnect) as resumed:
        result = list(resumed.run())[-1]
        assert result["status"] == "unverified"
        assert result["stop_reason"] == "migration_unreconciled"
        assert resumed.state["contract_version"] == 2
        assert resumed.state["migration_pending"] is True
        assert resumed.state["migration_orphan_attempts"] == [attempt]
        assert resumed.store.attempts() == records
        assert (tmp_path / "legacy" / resumed.state["migration_backup"]).exists()
    reconnect.act.assert_not_called()
    worker_module.choose.assert_not_called()
