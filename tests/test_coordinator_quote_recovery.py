"""A rejected plan carries precise, durable feedback into one task-level correction."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_coordinator_recovery import GOAL, browser_for, contract, ready_agent

from agentrun import SupervisedAgent
from agentrun.supervisor_contract import validate_contract
from AgentBrowser import agent as worker_module
from AgentBrowser import model


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No paid APIs"))))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text API")))
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=AssertionError("No Jev before approved audit")))


def plan():
    return validate_contract(contract(), GOAL)


def invalid_plan():
    result = plan()
    # Both pieces come from the goal, but omitting "once" makes this noncontiguous.
    result["tasks"][0]["goal_quote"] = "Click the observed Go button and verify the result."
    return result


def browser_for_test(tmp_path):
    browser = browser_for()
    browser.target = "quote-recovery-" + tmp_path.name
    return browser


def assert_no_work(agent):
    assert agent.state["pending_delegation"] is None
    assert agent.state["delegation_sequence"] == 0
    assert agent.store.attempts() == []
    agent.browser.act.assert_not_called()
    worker_module.choose.assert_not_called()
    worker_module.field_text.assert_not_called()


def test_initialization_correction_receives_rejected_plan_and_path_before_any_browser_work(tmp_path):
    rejected = invalid_plan()
    payloads = []

    def supervisor(payload):
        payloads.append(deepcopy(payload))
        if payload["phase"] == "audit":
            return {"approved": True}
        return {"contract": rejected if len(payloads) == 1 else plan()}

    with ready_agent(tmp_path, supervisor=supervisor, browser=browser_for_test(tmp_path), trace=False) as agent:
        agent._decide()
        feedback = deepcopy(agent.state["validation_error"])
        assert feedback["phase"] == "initialize"
        assert "tasks[0].goal_quote" in feedback["reason"]
        assert "initialize_plan" in feedback["instruction"]
        assert feedback["browser_action_executed"] is False
        assert feedback["rejected_decision"] == {
            "contract_version": 2, "phase": "initialize", "contract": rejected,
        }
        assert agent.state["contract"] is None
        assert agent.state["validation_retries"] == 1
        assert agent.store.load_state()["validation_error"] == feedback
        assert_no_work(agent)

        # The correction receives an independent copy of the rejected response.
        rejected["tasks"][0]["instruction"] = "A later mutation of the fake provider result"
        agent._decide()
        assert payloads[1]["validation_error"] == feedback
        assert agent.state["contract"] == plan()
        assert agent.state["contract_phase"] == "audit"
        assert "validation_error" not in agent.state
        assert agent.state["validation_retries"] == 0
        assert agent.state["supervisor_calls"] == 2
        assert_no_work(agent)

        agent._decide()
        assert agent.state["contract_phase"] == "review"
        assert [payload["phase"] for payload in payloads] == ["initialize", "initialize", "audit"]
        assert_no_work(agent)


def test_same_invalid_quote_stops_after_the_single_correction(tmp_path):
    payloads = []

    def supervisor(payload):
        payloads.append(deepcopy(payload))
        return {"contract": invalid_plan()}

    with ready_agent(tmp_path, supervisor=supervisor, browser=browser_for_test(tmp_path), trace=False) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "unverified"
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert result["question"] == ""
        assert len(payloads) == agent.state["supervisor_calls"] == 2
        assert payloads[1]["validation_error"]["rejected_decision"]["contract"] == invalid_plan()
        assert agent.state["contract"] is None
        assert agent.store.load_state()["status"] == "unverified"
        assert_no_work(agent)


def test_invalid_audit_replacement_keeps_current_contract_and_returns_rejected_response(tmp_path):
    replacement = invalid_plan()
    replacement["tasks"][0]["instruction"] = "Click Go once and inspect the visible result."
    rejected = {
        "approved": False, "problems": ["Clarify the expected postcondition."],
        "replacement_contract": replacement,
    }
    corrected = deepcopy(rejected)
    corrected["replacement_contract"]["tasks"][0]["goal_quote"] = GOAL
    payloads = []

    def supervisor(payload):
        payloads.append(deepcopy(payload))
        if payload["phase"] == "initialize":
            return {"contract": plan()}
        return rejected if len(payloads) == 2 else corrected

    with ready_agent(tmp_path, supervisor=supervisor, browser=browser_for_test(tmp_path), trace=False) as agent:
        agent._decide()
        original = deepcopy(agent.state["contract"])
        agent._decide()
        feedback = deepcopy(agent.state["validation_error"])
        assert feedback["phase"] == "audit"
        assert "tasks[0].goal_quote" in feedback["reason"]
        assert "audit_plan" in feedback["instruction"]
        assert feedback["rejected_decision"] == {"contract_version": 2, "phase": "audit", **rejected}
        assert agent.state["contract"] == original
        assert agent.state["contract_phase"] == "audit"
        assert agent.state["audit_replacements"] == 0
        saved = agent.store.load_state()
        assert saved["validation_error"] == feedback and saved["contract"] == original
        assert_no_work(agent)

        agent._decide()
        assert payloads[2]["validation_error"] == feedback
        assert payloads[2]["contract"] == original
        assert agent.state["contract"] == corrected["replacement_contract"]
        assert agent.state["contract_phase"] == "audit", "A replacement still needs its own approved audit"
        assert agent.state["audit_replacements"] == 1
        assert agent.state["supervisor_calls"] == 3
        assert "validation_error" not in agent.state
        assert_no_work(agent)


def test_rejected_quote_feedback_survives_resume_without_resetting_correction_allowance(tmp_path):
    browser = browser_for_test(tmp_path)
    with ready_agent(tmp_path, supervisor=lambda _: {"contract": invalid_plan()}, browser=browser,
                     trace=False) as agent:
        agent._decide()
        feedback = deepcopy(agent.state["validation_error"])
        assert agent.store.load_state()["validation_error"] == feedback
        assert_no_work(agent)

    payloads = []

    def supervisor(payload):
        payloads.append(deepcopy(payload))
        return {"contract": invalid_plan()}

    reconnect = browser_for_test(tmp_path)
    with SupervisedAgent.resume("recovery", state_dir=tmp_path, supervisor=supervisor,
                                browser=reconnect, trace=False) as resumed:
        assert resumed.state["validation_error"] == feedback
        assert resumed.state["validation_retries"] == 1
        result = list(resumed.run())[-1]
        assert payloads[0]["validation_error"] == feedback
        assert len(payloads) == 1
        assert resumed.state["supervisor_calls"] == 2
        assert result["status"] == "unverified"
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert_no_work(resumed)
