"""Replay run 86dc5ad's literal-quote failure without models or browser input."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_agent import page
from test_coordinator_quote_recovery import assert_no_work
from test_coordinator_recovery import browser_for
from test_goal_quote_provenance import SCOPES, entry_at
from test_observability_transport import event_payload, records
from test_supervisor_contract import GOAL, contract
from test_supervisor_policy import ToolModel
from test_supervisor_policy_v2 import tool_call

from agentrun import SupervisedAgent
from agentrun.supervisor_contract import ContractError, repair_goal_quote_punctuation, validate_contract
from agentrun.supervisor_policy import SupervisorPolicy
from AgentBrowser import agent as worker_module
from AgentBrowser import model
from AgentBrowser.observability import TraceConfig


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No paid APIs"))))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text API")))
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=AssertionError("No Jev before approved audit")))


@pytest.mark.parametrize("response_index", [0, 1])
@pytest.mark.parametrize("phase", ["initialize", "audit"])
def test_recorded_quote_is_repaired_before_audit_without_a_model_retry(tmp_path, capsys, response_index, phase):
    fixture = json.loads((Path(__file__).parent / "fixtures/plan_quote_trailing_comma.json").read_text())
    raw = fixture["responses"][response_index]["response"]["contract"]
    before = deepcopy(raw)
    corrected = deepcopy(raw)
    corrected["loops"][0]["discovery"]["goal_quote"] = raw["loops"][0]["discovery"]["goal_quote"][:-1]
    expected = validate_contract(corrected, fixture["goal"])
    # Both initial plans are recorded replies. Audits here are explicit offline stubs.
    if phase == "initialize":
        replies = [tool_call("initialize_plan", {"contract": raw})]
    else:
        replies = [tool_call("initialize_plan", {"contract": corrected}), tool_call("audit_plan", {
            "approved": False, "problems": ["Clarify the plan."], "replacement_contract": raw,
        })]
    replies.append(tool_call("audit_plan", {"approved": True}))
    fake = ToolModel(responses=replies)
    policy = SupervisorPolicy(model=fake)
    with SupervisedAgent(page()["url"], fixture["goal"], supervisor=policy, browser=browser_for(),
                         state_dir=tmp_path / "state", run_id="recorded-quote",
                         trace=TraceConfig(directory=tmp_path / "traces")) as agent:
        for _ in range(len(replies) - 1):
            agent._decide()
            assert_no_work(agent)
        assert agent.state["stop_reason"] is None
        assert agent.state["contract"] == expected
        assert agent.state["contract_phase"] == "audit"
        assert agent.state["validation_retries"] == 0
        assert "validation_error" not in agent.state
        assert agent.store.load_state()["contract"] == expected
        repaired = [e for e in records(agent.trace) if e["event"] == "coordinator.plan_quote_repaired"]
        assert len(repaired) == 1 and repaired[0]["phase"] == phase
        repair_payload = event_payload(agent.trace, repaired[0])
        assert repair_payload == {"contract": expected, "repairs": [{
            "field": "loops[0].discovery.goal_quote",
            "original": before["loops"][0]["discovery"]["goal_quote"],
            "replacement": expected["loops"][0]["discovery"]["goal_quote"],
        }]}
        applied = [e for e in records(agent.trace) if e["event"] == "coordinator.review_applied"][-1]
        raw_response = event_payload(agent.trace, applied)["response"]
        assert raw_response["contract" if phase == "initialize" else "replacement_contract"] == before

        agent._decide()
        assert_no_work(agent)
        assert agent.state["contract_phase"] == "review"
        assert agent.state["supervisor_calls"] == fake.calls == len(replies)
        assert policy.http_attempts == agent.state["supervisor_http_attempts"] == 0
        assert json.loads(fake.messages_seen[-1][1].content)["contract"] == expected
    assert raw == before
    output = capsys.readouterr().out
    assert "Cita del objetivo corregida localmente" in output
    assert "loops[0].discovery.goal_quote" in output
    assert "pendiente de auditoría" in output


@pytest.mark.parametrize(("path", "field"), SCOPES)
def test_unique_word_boundary_repair_covers_every_quote_scope_without_rewriting_work(path, field):
    raw = contract()
    entry_at(raw, path)["goal_quote"] = "Inspect every result,"
    before = deepcopy(raw)
    result, repairs = repair_goal_quote_punctuation(raw, GOAL)
    expected = deepcopy(before)
    entry_at(expected, path)["goal_quote"] = "Inspect every result"
    assert result == expected and raw == before
    assert repairs == [{"field": field, "original": "Inspect every result,", "replacement": "Inspect every result"}]
    # Scope coverage and all other checks still belong to the strict validator.
    if path[0] != "requirements":
        assert validate_contract(result, GOAL) == result


@pytest.mark.parametrize(("goal", "quote"), [
    (GOAL, "Find books. return to the list.,"),  # Joined fragments.
    (GOAL, "Inspect all results,"),  # Paraphrase.
    (GOAL, "Inspect\u00a0every result,"),  # Changed internal whitespace.
    (GOAL, "Find books!"),  # Replaced punctuation, not an invented suffix.
    ("Inspect every result and inspect every result again.", "every result,"),  # Ambiguous source.
    ("Inspect every resulting page.", "Inspect every result,"),  # A word prefix.
    ("Inspect incorrect entries.", "correct entries,"),  # A word suffix.
    ("Use 90.000 euros maximum.", "9.000 euros,"),  # Wrong amount.
    ("Use 90.000 euros maximum.", "000 euros,"),  # Suffix of a number.
    ("Use 50.000 euros maximum.", "Use 50."),  # Prefix of a number.
    ("Use 50 maximum.", "Use 50,"),  # No editing punctuation attached to digits.
])
def test_ambiguous_or_substantive_quote_errors_still_fail_strict_validation(goal, quote):
    raw = contract()
    raw["tasks"][0]["goal_quote"] = quote
    before = deepcopy(raw)
    result, repairs = repair_goal_quote_punctuation(raw, goal)
    assert repairs == [] and result == before and raw == before
    with pytest.raises(ContractError):
        validate_contract(result, goal)


def test_a_literal_quote_is_never_trimmed():
    raw = contract()
    result, repairs = repair_goal_quote_punctuation(raw, GOAL)
    assert repairs == [] and result == raw


def test_punctuation_repair_does_not_bypass_other_invalid_plan_fields(tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures/plan_quote_trailing_comma.json").read_text())
    raw = fixture["responses"][0]["response"]["contract"]
    raw["tasks"][0]["depends_on"] = [raw["tasks"][0]["id"]]
    with SupervisedAgent(page()["url"], fixture["goal"], supervisor=lambda _: {"contract": raw},
                         browser=browser_for(), state_dir=tmp_path, run_id="invalid-plan", trace=False) as agent:
        agent._decide()
        assert agent.state["contract"] is None
        assert "cycle" in agent.state["validation_error"]["reason"]
        assert agent.state["validation_error"]["rejected_decision"]["contract"] == raw
        assert agent.state["validation_retries"] == 1
        assert_no_work(agent)
