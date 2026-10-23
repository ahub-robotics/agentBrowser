"""One real LangChain tool call per coordinator phase, with only offline models."""

import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage
from test_supervisor_contract import GOAL, contract
from test_supervisor_policy import ToolModel

from agentrun.supervisor_policy import AUDIT_INSTRUCTIONS, V2_INSTRUCTIONS, SupervisorPolicy
from AgentBrowser.model import ProviderError


def tool_call(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "intent", "type": "tool_call"}])


def payload(phase, **fields):
    return {"contract_version": 2, "phase": phase, "goal": GOAL, **fields}


def test_three_phases_expose_one_tool_each_without_legacy_next_item_choice():
    calls = [
        ("initialize", "initialize_plan", {"contract": contract()}),
        ("audit", "audit_plan", {"approved": True, "problems": [], "replacement_contract": None}),
        ("review", "review_progress", {"next_action": "continue", "discovered": [
            {"loop_id": "books", "item_refs": ["observed-book-link"]},
        ]}),
    ]
    model = ToolModel(responses=[tool_call(name, fields) for _, name, fields in calls])
    policy = SupervisorPolicy(model=model)
    for phase, name, fields in calls:
        result = policy.invoke(payload(phase))
        assert result["contract_version"] == 2 and result["phase"] == phase
        assert [tool.name for tool in model.tools_seen] == [name]
        assert model.tools_seen[0].args_schema.model_json_schema()["additionalProperties"] is False
        assert model.messages_seen[-1][0].content == (AUDIT_INSTRUCTIONS if phase == "audit" else V2_INSTRUCTIONS)
        for key, value in fields.items():
            assert result[key] == value
    assert model.calls == 3
    assert model.bind_options == [{"tool_choice": "required"}] * 3
    assert "task" not in result and "action" not in result and "requirements" not in result


def test_audit_receives_full_plan_and_repair_context_without_browser_review_payload():
    model = ToolModel(responses=[tool_call("audit_plan", {"approved": True})])
    source = payload("audit", contract=contract(), feedback=[{"answer": "Keep the original scope."}],
                     audit_history=[{"correction": 1, "problems": ["Missing return step."]}],
                     validation_error={"reason": "Invalid goal quote", "rejected_decision": {"approved": False}},
                     page={"text": "UNTRUSTED_PAGE_SENTINEL"}, evidence=[{"ref": "UNRELATED_FACT"}],
                     observed_links=[{"href": "https://example.test"}], available_actions=["continue", "observe"])
    before = deepcopy(source)
    SupervisorPolicy(model=model).invoke(source)
    sent = json.loads(model.messages_seen[0][1].content)
    assert sent == {key: source[key] for key in (
        "contract_version", "phase", "goal", "contract", "feedback", "audit_history", "validation_error",
    )}
    assert model.messages_seen[0][0].content == AUDIT_INSTRUCTIONS
    assert source == before


@pytest.mark.parametrize(("phase", "name", "fields"), [
    ("initialize", "audit_plan", {"approved": True}),
    ("audit", "initialize_plan", {"contract": contract()}),
    ("review", "delegate_to_jev", {"task": {"instruction": "Open another book"}}),
    ("review", "finish_run", {"evidence_refs": ["fact"]}),
    ("review", "review_progress", {"next_action": "continue", "task": {"id": "chosen-next-item"}}),
    ("review", "review_progress", {"next_action": "continue", "requirements": contract()["requirements"]}),
    ("audit", "audit_plan", {"approved": "true"}),
    ("initialize", "initialize_plan", {"contract": {**contract(), "selector": "a.book"}}),
])
def test_wrong_phase_legacy_or_malformed_tool_is_rejected_without_implicit_retry(phase, name, fields):
    model = ToolModel(responses=[tool_call(name, fields)])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke(payload(phase))
    assert model.calls == 1


@pytest.mark.parametrize("response", [
    AIMessage(content="Continue with the next item."),
    AIMessage(content="", tool_calls=[
        {"name": "review_progress", "args": {"next_action": "continue"}, "id": "one", "type": "tool_call"},
        {"name": "review_progress", "args": {"next_action": "observe"}, "id": "two", "type": "tool_call"},
    ]),
])
def test_v2_free_text_and_parallel_tool_calls_never_dispatch(response):
    model = ToolModel(responses=[response])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke(payload("review"))
    assert model.calls == 1


def test_review_next_actions_reflect_current_availability_and_refresh_between_calls():
    model = ToolModel(responses=[tool_call("review_progress", {"next_action": "observe"})] * 2)
    policy = SupervisorPolicy(model=model)
    for actions in (["continue", "observe", "inspect"], ["continue", "observe"]):
        result = policy.invoke(payload("review", available_actions=actions))
        assert result["next_action"] == "observe"
        schema = model.tools_seen[0].args_schema.model_json_schema()
        assert schema["properties"]["next_action"]["enum"] == actions
    assert model.calls == 2


def test_unavailable_review_action_cannot_dispatch_even_if_model_ignores_schema():
    model = ToolModel(responses=[tool_call("review_progress", {"next_action": "inspect"})])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke(payload("review", available_actions=["continue", "observe"]))
    assert model.calls == 1


def test_technical_errors_cannot_become_user_feedback():
    fields = {"next_action": "feedback", "feedback": {
        "question": "Should I try again?", "feedback_reason": "provider_error", "missing_information": "",
        "blocked_requirement_ids": ["inspect"], "user_intervention": "Confirm whether I should continue.",
    }}
    model = ToolModel(responses=[tool_call("review_progress", fields)])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke(payload("review"))
    assert model.calls == 1


def test_feedback_reason_must_also_be_available_in_current_state():
    fields = {"next_action": "feedback", "feedback": {
        "question": "Resolve the uncertain effect.", "feedback_reason": "uncertain_effect",
        "blocked_requirement_ids": ["inspect"], "user_intervention": "Confirm the outcome unavailable to observation.",
    }}
    model = ToolModel(responses=[tool_call("review_progress", fields)])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke(payload("review", feedback_reasons=["missing_user_information"]))
    assert model.calls == 1


def test_rejected_audit_returns_replacement_but_does_not_imply_approval():
    model = ToolModel(responses=[tool_call("audit_plan", {
        "approved": False, "problems": ["Missing a requested return to the list."], "replacement_contract": contract(),
    })])
    raw = payload("audit", contract=contract())
    before = deepcopy(raw)
    result = SupervisorPolicy(model=model).invoke(raw)
    assert result["approved"] is False and result["replacement_contract"] == contract()
    assert raw == before


def test_review_tool_preserves_redirect_reference_without_selecting_or_changing_item():
    fields = {"next_action": "continue", "subtask_review": {
        "delegation_id": "run:4", "status": "satisfied", "evidence_refs": ["detail-fact"],
        "destination_ref": "observed-redirect-url",
    }}
    model = ToolModel(responses=[tool_call("review_progress", fields)])
    result = SupervisorPolicy(model=model).invoke(payload("review"))
    assert result["subtask_review"] == fields["subtask_review"]
    assert "task" not in result and "item_id" not in result
    assert model.calls == 1


@pytest.mark.parametrize("fields", [{"contract_version": 3}, {"contract_version": True}, {"phase": "unknown"}])
def test_unknown_contract_or_phase_fails_before_model_call(fields):
    model = ToolModel(responses=[])
    with pytest.raises(ValueError):
        SupervisorPolicy(model=model).invoke({**payload("review"), **fields})
    assert model.calls == 0
