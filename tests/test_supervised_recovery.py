"""Coordinator recovery uses real durable state and never paid models or Chrome."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import decision, page

from agentrun import supervised as supervised_module
from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from agentrun.supervised import SupervisorLimits
from agentrun.supervisor_store import RunLockedError
from AgentBrowser import agent as worker_module
from AgentBrowser.browser import fingerprint


def browser_for(observed=None, *, target="tab"):
    observed = observed or page()
    return Mock(
        target=target, observe=Mock(side_effect=lambda **_kwargs: deepcopy(observed)),
        fresh=Mock(return_value=True),
    )


def response(action, payload, **updates):
    result = {
        "action": action,
        "requirements": [{
            "id": "goal", "goal_quote": payload["goal"], "description": "Complete the requested outcome",
            "status": "unknown", "evidence_refs": [],
        }],
    }
    if action == "delegate":
        result["task"] = {
            "id": "subtask", "instruction": "Click the observed Go button",
            "success_criteria": ["The requested outcome is visibly confirmed"], "item_ref": None,
        }
    result.update(updates)
    return result


def choice(operation="CLICK", selected="e3", target="2"):
    return {**decision(selected), "operation": operation, "target": target}


@pytest.mark.parametrize("failure", ["during_input", "after_input"])
def test_uncertain_browser_attempt_survives_restart_without_replay(tmp_path, monkeypatch, failure):
    observed = page()
    browser = browser_for(observed)
    if failure == "during_input":
        browser.act.side_effect = RuntimeError("Connection dropped after possible input")
    else:
        def observe(**_kwargs):
            if browser.act.call_count:
                raise RuntimeError("Cannot read postcondition")
            return deepcopy(observed)
        browser.observe.side_effect = observe
    choose = Mock(return_value=choice())
    monkeypatch.setattr(worker_module, "choose", choose)
    supervisor = Mock(spec=[], side_effect=lambda payload: response("delegate", payload))
    with SupervisedAgent(observed["url"], "Complete goal", run_id="uncertain", state_dir=tmp_path,
                         supervisor=supervisor, browser=browser) as agent:
        states = list(agent.run())
        assert states[-1]["status"] == "waiting_feedback"
        assert states[-1]["stop_reason"] == "uncertain_action"
        before = states[-1]["metrics"]
        uncertain = agent.store.attempts(unresolved_only=True)
        assert len(uncertain) == 1
        assert uncertain[0]["state"] == ("started" if failure == "during_input" else "dispatched")
    browser.act.assert_called_once()
    browser.close.assert_not_called()
    resumed_browser = browser_for(observed)
    resumed_supervisor = Mock(spec=[], side_effect=lambda payload: response("delegate", payload))
    with SupervisedAgent.resume("uncertain", state_dir=tmp_path, feedback="Continue safely",
                                supervisor=resumed_supervisor, browser=resumed_browser) as resumed:
        last = list(resumed.run())[-1]
        assert last["status"] == "waiting_feedback"
        assert last["stop_reason"] == "uncertain_action"
        assert last["metrics"]["actions"] == before["actions"] == 1
        assert last["metrics"]["jev_calls"] == before["jev_calls"] == 1
        assert len(resumed.store.attempts(unresolved_only=True)) == 1
    resumed_browser.act.assert_not_called()
    assert choose.call_count == 1


def test_waiting_feedback_resume_without_reply_does_not_attach_or_call_models(tmp_path, monkeypatch):
    browser = browser_for()
    choose = Mock()
    monkeypatch.setattr(worker_module, "choose", choose)
    supervisor = Mock(
        spec=[], side_effect=lambda payload: response("feedback", payload, question="Choose the result scope"),
    )
    with SupervisedAgent(page()["url"], "Complete goal", run_id="feedback", state_dir=tmp_path,
                         supervisor=supervisor, browser=browser) as agent:
        original = list(agent.run())[-1]
        assert original["status"] == "waiting_feedback"
    reconnect = browser_for()
    idle_supervisor = Mock()
    with SupervisedAgent.resume("feedback", state_dir=tmp_path, supervisor=idle_supervisor,
                                browser=reconnect) as resumed:
        assert list(resumed.run())[-1]["metrics"] == original["metrics"]
        assert resumed.state["question"] == "Choose the result scope"
    reconnect.observe.assert_not_called()
    reconnect.act.assert_not_called()
    idle_supervisor.assert_not_called()
    choose.assert_not_called()


@pytest.mark.parametrize(("limit", "expected"), [
    ({"max_supervisor_calls": 1}, "supervisor_budget"),
    ({"max_decisions": 1}, "decision_budget"),
    ({"max_actions": 1}, "action_budget"),
])
def test_global_budgets_survive_feedback_and_restart(tmp_path, monkeypatch, limit, expected):
    browser = browser_for()
    if "max_actions" in limit:
        choose = Mock(side_effect=[choice(), choice("DONE", "DONE", None)])
    else:
        choose = Mock(return_value=choice("BLOCKED", "BLOCKED", None))
    monkeypatch.setattr(worker_module, "choose", choose)
    supervisor = Mock(spec=[], side_effect=lambda payload: response("delegate", payload))
    with SupervisedAgent(page()["url"], "Complete goal", run_id="budget", state_dir=tmp_path,
                         limits=SupervisorLimits(**limit), supervisor=supervisor, browser=browser) as agent:
        before = list(agent.run())[-1]
        assert before["stop_reason"] == expected
    resumed_browser = browser_for()
    resumed_supervisor = Mock(spec=[], side_effect=lambda payload: response("delegate", payload))
    previous_decisions = choose.call_count
    with SupervisedAgent.resume("budget", state_dir=tmp_path, feedback="Keep going",
                                supervisor=resumed_supervisor, browser=resumed_browser) as resumed:
        after = list(resumed.run())[-1]
        assert after["stop_reason"] == expected
        for key in ("actions", "jev_calls", "text_calls", "candidate_windows"):
            assert after["metrics"][key] == before["metrics"][key]
    if expected == "supervisor_budget":
        resumed_supervisor.assert_not_called()
    resumed_browser.act.assert_not_called()
    assert choose.call_count == previous_decisions


def test_malformed_action_cannot_partly_commit_valid_requirement_updates(tmp_path):
    def malformed(payload):
        ref = payload["current_evidence_refs"][0]
        return response("delegate", payload, task=None, plan=["Should not persist"], requirements=[{
            "id": "goal", "goal_quote": payload["goal"], "description": "Changed requirement",
            "status": "satisfied", "evidence_refs": [ref],
        }])
    with SupervisedAgent(page()["url"], "Complete goal", run_id="atomic", state_dir=tmp_path,
                         supervisor=malformed, browser=browser_for()) as agent:
        last = list(agent.run())[-1]
        assert last["stop_reason"] == "supervisor_invalid_response"
        assert agent.state["requirements"] == []
        assert agent.state["plan"] == []
        assert agent.store.load_state()["requirements"] == []
        assert agent.store.attempts() == []


def test_invalid_decision_cannot_durably_reconcile_an_uncertain_attempt(tmp_path):
    with SupervisedAgent(page()["url"], "Complete goal", run_id="reconcile", state_dir=tmp_path,
                         supervisor=Mock(), browser=browser_for()) as agent:
        observed = page()
        attempt = agent.store.reserve_attempt("interrupted", None, observed["actions"][2], observed)
        agent.state["attempt_observations"][attempt] = agent.state["observation_seq"]
        agent._record_page(observed)

        def malformed(payload):
            return response(
                "delegate", payload, task=None,
                reconcile_attempts=[{"attempt_id": attempt, "evidence_refs": payload["current_evidence_refs"][:1]}],
            )
        agent.supervisor = malformed
        last = list(agent.run())[-1]
        assert last["stop_reason"] == "supervisor_invalid_response"
        assert [entry["id"] for entry in agent.store.attempts(unresolved_only=True)] == [attempt]
        assert agent.state["requirements"] == []


def test_item_cannot_be_completed_without_visiting_its_observed_destination(tmp_path, monkeypatch):
    observed = page()
    observed["actions"].append({
        "id": "item", "node": 30, "kind": "click", "label": "Item 1", "role": "link",
        "href": "https://example.test/item/1", "href_truncated": False,
    })
    observed["fingerprint"] = fingerprint(observed)
    browser = browser_for(observed)
    monkeypatch.setattr(worker_module, "choose", Mock(return_value=choice("DONE", "DONE", None)))

    def supervisor(payload):
        item_ref = next(fact["ref"] for fact in payload["evidence"] if fact.get("href"))
        loop = {
            "id": "items", "goal_quote": payload["goal"], "description": "Visit one result",
            "item_refs": [item_ref], "exhausted": False, "evidence_refs": [],
        }
        if not payload["items"]:
            return response("delegate", payload, loops=[loop], task={
                "id": "visit", "instruction": "Visit the observed result",
                "success_criteria": ["Its detail page is visible"], "item_ref": item_ref,
            })
        refs = payload["current_evidence_refs"][:1]
        return response("finish", payload, evidence_refs=refs, loops=[{
            **loop, "exhausted": True, "evidence_refs": refs,
        }], requirements=[{
            "id": "goal", "goal_quote": payload["goal"], "description": "Visit one result",
            "status": "satisfied", "evidence_refs": refs,
        }], item_reviews=[{
            "item_id": next(iter(payload["items"])), "status": "completed", "evidence_refs": refs,
        }])

    with SupervisedAgent(observed["url"], "Visit one result", run_id="visit", state_dir=tmp_path,
                         supervisor=supervisor, browser=browser) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "unverified"
        assert last["stop_reason"] == "supervisor_invalid_response"
        assert all(item["status"] != "completed" for item in agent.state["items"].values())
    browser.act.assert_not_called()


def test_completed_run_cannot_be_reopened_by_feedback(tmp_path):
    def finish(payload):
        refs = payload["current_evidence_refs"][:1]
        return response("finish", payload, evidence_refs=refs, requirements=[{
            "id": "goal", "goal_quote": payload["goal"], "description": "Observed current page",
            "status": "satisfied", "evidence_refs": refs,
        }])
    with SupervisedAgent(page()["url"], "Observe current page", run_id="done", state_dir=tmp_path,
                         supervisor=finish, browser=browser_for()) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "done"
    resumed_browser, supervisor = browser_for(), Mock()
    with SupervisedAgent.resume("done", state_dir=tmp_path, feedback="Thanks",
                                supervisor=supervisor, browser=resumed_browser) as resumed:
        assert list(resumed.run())[-1]["status"] == "done"
    resumed_browser.observe.assert_not_called()
    resumed_browser.act.assert_not_called()
    supervisor.assert_not_called()


def test_different_runs_cannot_control_the_same_browser_target(tmp_path):
    with SupervisedAgent(page()["url"], "Complete goal", run_id="first", state_dir=tmp_path,
                         supervisor=Mock(), browser=browser_for()) as first:
        with pytest.raises(RunLockedError):
            SupervisedAgent(page()["url"], "Another goal", run_id="second", state_dir=tmp_path,
                            supervisor=Mock(), browser=browser_for())
        assert first.state["target"] == "tab"


def test_resume_never_adopts_a_replacement_target(tmp_path):
    supervisor = Mock(spec=[], side_effect=lambda payload: response("feedback", payload, question="Provide scope"))
    with SupervisedAgent(page()["url"], "Complete goal", run_id="identity", state_dir=tmp_path,
                         supervisor=supervisor, browser=browser_for()) as agent:
        list(agent.run())
    replacement = browser_for(target="different-tab")
    with pytest.raises(ValueError, match="original browser target"):
        SupervisedAgent.resume("identity", state_dir=tmp_path, feedback="Continue",
                               supervisor=Mock(), browser=replacement)
    replacement.act.assert_not_called()
    replacement.close.assert_not_called()


@pytest.mark.parametrize("result_saved", [False, True])
def test_pending_delegation_is_reconciled_on_resume_without_reexecuting_it(tmp_path, monkeypatch, result_saved):
    choose = Mock()
    monkeypatch.setattr(worker_module, "choose", choose)
    with SupervisedAgent(page()["url"], "Complete goal", run_id="pending", state_dir=tmp_path,
                         supervisor=lambda payload: response("delegate", payload), browser=browser_for()) as agent:
        agent._decide()
        pending = agent.state["pending_delegation"]
        result = agent._worker_result("claimed_done", "worker_returned")
        if result_saved:
            # Simulates termination after the tool committed its result but
            # before the parent graph committed the cleared delegation.
            agent.store.save_delegation(pending["id"], pending["input"], result)
    resumed_browser = browser_for()

    def supervisor(payload):
        return response("feedback", payload, question="Inspect the saved partial result")

    with SupervisedAgent.resume("pending", state_dir=tmp_path, supervisor=supervisor,
                                browser=resumed_browser) as resumed:
        assert resumed.state["pending_delegation"] is None
        saved = resumed.store.get_delegation(pending["id"])["result"]
        assert resumed.state["last_result"] == saved
        if result_saved:
            assert saved == result
        else:
            assert saved["status"] == "interrupted"
            assert saved["stop_reason"] == "process_interrupted"
        assert list(resumed.run())[-1]["status"] == "waiting_feedback"
    choose.assert_not_called()
    resumed_browser.act.assert_not_called()


def test_initialization_without_an_original_target_remains_unavailable_on_resume(tmp_path, monkeypatch):
    browser_factory = Mock(side_effect=RuntimeError("Could not create a browser target"))
    monkeypatch.setattr(supervised_module, "Browser", browser_factory)
    with pytest.raises(RuntimeError, match="Could not create"):
        SupervisedAgent(page()["url"], "Complete goal", run_id="no-target", state_dir=tmp_path, supervisor=Mock())
    supervisor = Mock()
    with SupervisedAgent.resume("no-target", state_dir=tmp_path, supervisor=supervisor) as resumed:
        last = list(resumed.run())[-1]
        assert last["status"] == "browser_unavailable"
        assert last["metrics"]["supervisor_calls"] == 0
    assert browser_factory.call_count == 1
    browser_factory.attach.assert_not_called()
    supervisor.assert_not_called()


def test_target_lock_spans_state_directories_and_failed_claim_does_not_disconnect_owner(tmp_path):
    browser = browser_for(target="cross-directory-owner")
    with SupervisedAgent(page()["url"], "Complete goal", run_id="first", state_dir=tmp_path / "one",
                         supervisor=Mock(), browser=browser):
        with pytest.raises(RunLockedError):
            SupervisedAgent(page()["url"], "Complete goal", run_id="second", state_dir=tmp_path / "two",
                            supervisor=Mock(), browser=browser)
        browser.disconnect.assert_not_called()
        browser.close.assert_not_called()
