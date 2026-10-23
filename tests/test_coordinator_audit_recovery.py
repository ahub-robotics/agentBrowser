"""Replay a recorded missing-replacement failure without APIs or browser input."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_agent import page
from test_coordinator_recovery import GOAL, browser_for, contract, ready_agent
from test_supervisor_policy import ToolModel
from test_supervisor_policy_v2 import tool_call

from agentrun import SupervisedAgent
from agentrun.supervised import SupervisorLimits
from agentrun.supervisor_contract import validate_contract
from agentrun.supervisor_policy import SupervisorPolicy
from AgentBrowser import agent as worker_module
from AgentBrowser import model


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No model endpoints"))))
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=AssertionError("No Jev before approved audit")))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text endpoint")))


def assert_no_work(agent):
    assert agent.state["pending_delegation"] is None
    assert agent.state["delegation_sequence"] == 0
    assert agent.store.attempts() == []
    agent.browser.act.assert_not_called()
    worker_module.choose.assert_not_called()
    worker_module.field_text.assert_not_called()


def test_recorded_second_audit_without_replacement_gets_revision_then_independent_approval(tmp_path, capsys):
    fixture = json.loads((Path(__file__).parent / "fixtures/audit_missing_revision.json").read_text())
    recorded = [item["response"] for item in fixture["responses"]]
    assert recorded[-1]["approved"] is False and recorded[-1]["replacement_contract"] is None
    # These last two responses are explicit offline stubs, not claims about Luna.
    revised = deepcopy(recorded[1]["replacement_contract"])
    model_task = next(task for task in revised["tasks"] if task["id"] == "model")
    model_task["success_criteria"] = ["The selected model XC60 is applied after confirmation in the model panel."]
    replies = [tool_call("initialize_plan" if index == 0 else "audit_plan", {
        key: value for key, value in reply.items() if key not in {"contract_version", "phase"}
    }) for index, reply in enumerate(recorded)]
    replies += [tool_call("initialize_plan", {"contract": revised}), tool_call("audit_plan", {"approved": True})]
    fake = ToolModel(responses=replies)
    policy = SupervisorPolicy(model=fake)

    with SupervisedAgent(page()["url"], fixture["goal"], supervisor=policy, browser=browser_for(),
                         run_id="recorded-audit", state_dir=tmp_path, trace=False) as agent:
        for _ in range(3):
            agent._decide()
            assert_no_work(agent)
        # This is where the recorded run stopped. No plan has been approved.
        assert agent.state["stop_reason"] is None
        assert agent.state["contract_phase"] == "initialize"
        assert agent.state["audit_replacements"] == 2
        assert agent.state["plan_revision"]["problems"] == recorded[-1]["problems"]
        assert len(agent.state["audit_history"]) == 2
        assert agent.store.load_state()["plan_revision"] == agent.state["plan_revision"]

        agent._decide()
        assert_no_work(agent)
        assert agent.state["contract_phase"] == "audit"
        assert agent.state["plan_revision"] is None
        assert agent.state["contract"] == validate_contract(revised, fixture["goal"])
        revision_input = json.loads(fake.messages_seen[3][1].content)
        assert revision_input["contract"] == recorded[1]["replacement_contract"]
        assert revision_input["plan_revision"]["problems"] == recorded[-1]["problems"]

        agent._decide()
        assert_no_work(agent)
        assert agent.state["contract_phase"] == "review"
        assert agent.state["supervisor_calls"] == fake.calls == 5
        assert agent.state["supervisor_phase_calls"] == {"initialize": 2, "audit": 3}
        audit_input = json.loads(fake.messages_seen[4][1].content)
        assert audit_input["contract"] == agent.state["contract"]
        assert audit_input["audit_history"][-1]["problems"] == recorded[-1]["problems"]
        assert "page" not in audit_input and "evidence" not in audit_input
        assert policy.http_attempts == 0
    output = capsys.readouterr().out
    assert "Criterios de «model»: The selected model XC60 is applied" in output


def reject_without_replacement(payload):
    if payload["phase"] == "initialize":
        return {"contract": contract()}
    return {"approved": False, "problems": ["Clarify the visible result."], "replacement_contract": None}


def test_pending_plan_revision_survives_resume_and_does_not_reserve_another_slot(tmp_path):
    with ready_agent(tmp_path, supervisor=reject_without_replacement, trace=False) as agent:
        agent._decide()
        agent._decide()
        saved = agent.store.load_state()
        assert saved["contract_phase"] == "initialize"
        assert saved["audit_replacements"] == 1
        assert_no_work(agent)

    seen = []

    def supervisor(payload):
        seen.append(deepcopy(payload))
        return {"contract": contract()} if payload["phase"] == "initialize" else {"approved": True}

    with SupervisedAgent.resume("recovery", state_dir=tmp_path, browser=browser_for(),
                                supervisor=supervisor, trace=False) as resumed:
        resumed._decide()
        assert seen[0]["plan_revision"] == saved["plan_revision"]
        assert seen[0]["contract"] == saved["contract"]
        assert resumed.state["audit_replacements"] == 1
        assert resumed.state["contract_phase"] == "audit"
        resumed._decide()
        assert resumed.state["contract_phase"] == "review"
        assert_no_work(resumed)


def test_invalid_revision_retains_previous_contract_and_pending_audit_feedback(tmp_path):
    with ready_agent(tmp_path, supervisor=reject_without_replacement, trace=False) as agent:
        agent._decide()
        agent._decide()
        original = deepcopy(agent.state["contract"])
        pending = deepcopy(agent.state["plan_revision"])
        invalid = contract()
        invalid["tasks"][0]["goal_quote"] = "A quotation that is not in the goal"
        agent.supervisor = lambda _: {"contract": invalid}
        agent._decide()
        assert agent.state["contract"] == original
        assert agent.state["plan_revision"] == pending
        assert agent.state["audit_replacements"] == 1
        assert agent.state["validation_retries"] == 1
        assert_no_work(agent)
        agent.supervisor = lambda _: {"contract": contract()}
        agent._decide()
        assert agent.state["contract_phase"] == "audit"
        assert agent.state["plan_revision"] is None
        assert agent.state["audit_replacements"] == 1
        assert_no_work(agent)


def test_missing_replacement_repair_cannot_bypass_global_model_budget(tmp_path):
    with ready_agent(tmp_path, supervisor=reject_without_replacement, trace=False,
                     limits=SupervisorLimits(max_supervisor_calls=2)) as agent:
        result = list(agent.run())[-1]
        assert result["stop_reason"] == "supervisor_budget"
        assert agent.state["supervisor_calls"] == 2
        assert agent.state["plan_revision"] is not None
        assert agent.state["contract"] == validate_contract(contract(), GOAL)
        assert_no_work(agent)
