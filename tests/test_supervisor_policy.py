"""Exercise the real LangChain supervisor graph without a model endpoint."""

import json
from typing import Any
from unittest.mock import Mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from test_agent import page

from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from agentrun.supervisor_policy import INSTRUCTIONS, SupervisorDecision, SupervisorPolicy, supervisor_configuration
from AgentBrowser.model import ProviderError


class ToolModel(BaseChatModel):
    responses: list[AIMessage]
    calls: int = 0
    tools_seen: list[Any] = Field(default_factory=list)
    bind_options: list[dict[str, Any]] = Field(default_factory=list)
    messages_seen: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-supervisor"

    def bind_tools(self, tools, **kwargs):
        self.tools_seen = list(tools)
        self.bind_options.append(kwargs)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.messages_seen.append(messages)
        response = self.responses[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


def feedback():
    return {
        "action": "feedback", "requirements": [{
            "id": "goal", "goal_quote": "Find a book", "description": "Find the requested book",
            "status": "unknown", "evidence_refs": [],
        }], "question": "Which book?", "feedback_reason": "missing_user_information",
        "missing_information": "The title of the requested book.",
        "blocked_requirement_ids": ["goal"],
        "user_intervention": "The user must supply the book title; it is absent from the goal and page.",
    }


def feedback_intent():
    return {key: value for key, value in feedback().items() if key != "action"}


def test_exhausted_inspection_and_unavailable_recovery_reason_are_absent_from_tools():
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "request_feedback", "args": feedback_intent(), "id": "pause", "type": "tool_call",
    }])])
    result = SupervisorPolicy(model=model).invoke({
        "goal": "Find a book", "available_actions": ["delegate", "finish", "feedback"],
        "feedback_reasons": ["missing_user_information", "provider_error"],
    })
    assert result["action"] == "feedback"
    assert {tool.name for tool in model.tools_seen} == {"delegate_to_jev", "finish_run", "request_feedback"}
    tool = next(tool for tool in model.tools_seen if tool.name == "request_feedback")
    schema = tool.args_schema.model_json_schema()
    assert schema["properties"]["feedback_reason"]["enum"] == ["missing_user_information", "provider_error"]
    assert model.calls == 1


def test_tool_availability_refreshes_when_current_evidence_changes():
    response = AIMessage(content="", tool_calls=[{
        "name": "request_feedback", "args": feedback_intent(), "id": "pause", "type": "tool_call",
    }])
    model = ToolModel(responses=[response, response])
    policy = SupervisorPolicy(model=model)
    for inspect in (True, False):
        policy.invoke({
            "goal": "Find a book", "feedback_reasons": ["missing_user_information"],
            "available_actions": ["delegate", "finish", "feedback", *(["inspect"] if inspect else [])],
        })
        assert ("inspect_evidence" in {tool.name for tool in model.tools_seen}) is inspect
    assert model.calls == 2


@pytest.mark.parametrize("invalid", ["inspect", "recovery"])
def test_model_cannot_dispatch_unavailable_inspect_or_recovery_even_if_it_ignores_tools(invalid):
    args = ({"requirements": feedback()["requirements"]} if invalid == "inspect"
            else {**feedback_intent(), "feedback_reason": "recovery_exhausted"})
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "inspect_evidence" if invalid == "inspect" else "request_feedback",
        "args": args, "id": "unavailable", "type": "tool_call",
    }])])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke({
            "goal": "Find a book", "available_actions": ["delegate", "finish", "feedback"],
            "feedback_reasons": ["missing_user_information"],
        })
    assert model.calls == 1


def test_real_create_agent_runs_once_and_returns_typed_dispatch():
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[
        {"name": "request_feedback", "args": feedback_intent(), "id": "one", "type": "tool_call"},
    ])])
    policy = SupervisorPolicy(model=model)
    result = policy.invoke({"goal": "Find a book", "evidence": []})
    assert result["action"] == "feedback" and result["question"] == "Which book?"
    assert result["subtask_review"] is None
    assert model.calls == 1
    assert [tool.name for tool in model.tools_seen] == [
        "delegate_to_jev", "inspect_evidence", "finish_run", "request_feedback",
    ]
    assert model.bind_options == [{"tool_choice": "required"}]
    assert [message.type for message in model.messages_seen[0]] == ["system", "human"]
    assert model.messages_seen[0][0].content == INSTRUCTIONS
    assert result["feedback_reason"] == "missing_user_information"
    assert result["missing_information"] == "The title of the requested book."


def test_real_policy_returns_intermediate_subtask_review_without_completing_global_requirement():
    decision = {"requirements": feedback()["requirements"]}
    decision.update(
        subtask_review={
            "delegation_id": "run:1", "status": "satisfied", "evidence_refs": ["observed-search-results"],
        },
        task={
            "id": "open-book", "instruction": "Open the observed result for the requested book.",
            "success_criteria": ["The requested book's detail page is displayed."],
            "scope": "general",
        },
    )
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[
        {"name": "delegate_to_jev", "args": decision, "id": "review", "type": "tool_call"},
    ])])
    result = SupervisorPolicy(model=model).invoke({
        "goal": "Find a book",
        "last_result": {
            "delegation_id": "run:1", "status": "claimed_done",
            "task": {"instruction": "Search for the book", "success_criteria": ["Search results show the book"]},
        },
        "evidence": [{"ref": "observed-search-results", "kind": "control", "label": "Requested book"}],
    })
    assert result["subtask_review"] == decision["subtask_review"]
    assert result["requirements"][0]["status"] == "unknown"
    assert result["task"] == {**decision["task"], "item_ref": None, "loop_id": None}
    assert result["action"] == "delegate"
    assert model.calls == 1


@pytest.mark.parametrize("review", [
    {"delegation_id": "", "status": "satisfied", "evidence_refs": ["fact"]},
    {"delegation_id": "run:1", "status": "done", "evidence_refs": ["fact"]},
    {"delegation_id": "run:1", "status": "satisfied", "evidence_refs": ["fact"], "retry": True},
])
def test_malformed_subtask_review_cannot_dispatch_or_trigger_an_implicit_model_retry(review):
    decision = {**feedback_intent(), "subtask_review": review}
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[
        {"name": "request_feedback", "args": decision, "id": "review", "type": "tool_call"},
    ])])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke({"goal": "Find a book"})
    assert model.calls == 1


@pytest.mark.parametrize("response", [
    AIMessage(content="Done!"),
    AIMessage(content="", tool_calls=[
        {"name": "request_feedback", "args": feedback_intent(), "id": "one", "type": "tool_call"},
        {"name": "request_feedback", "args": feedback_intent(), "id": "two", "type": "tool_call"},
    ]),
    AIMessage(content="", tool_calls=[
        {"name": "finish_run", "args": {}, "id": "one", "type": "tool_call"},
    ]),
])
def test_free_text_parallel_or_invalid_tool_output_never_becomes_an_instruction(response):
    model = ToolModel(responses=[response])
    policy = SupervisorPolicy(model=model)
    with pytest.raises(ProviderError):
        policy.invoke({"goal": "Find a book"})
    assert model.calls == 1


@pytest.mark.parametrize(("name", "fields"), [
    ("delegate_to_jev", {}),
    ("delegate_to_jev", {"task": None}),
    ("finish_run", {}),
    ("finish_run", {"evidence_refs": []}),
    ("request_feedback", {"question": "Which book?"}),
    ("request_feedback", {"question": "", "feedback_reason": "missing_user_information"}),
    ("inspect_evidence", {"task": None}),
    ("inspect_evidence", {"evidence_offset": 120}),
    ("supervisor_step", {"action": "feedback"}),
])
def test_operation_specific_tool_rejects_incomplete_or_irrelevant_arguments_in_one_call(name, fields):
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
        "name": name, "args": {"requirements": feedback()["requirements"], **fields},
        "id": "invalid", "type": "tool_call",
    }])])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke({"goal": "Find a book"})
    assert model.calls == 1


@pytest.mark.parametrize(("name", "fields", "action"), [
    ("inspect_evidence", {}, "inspect"),
    ("finish_run", {"evidence_refs": ["f5d3ffa283ef3d4d:node:18"]}, "finish"),
])
def test_read_and_finish_intents_normalize_without_changing_evidence_refs(name, fields, action):
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
        "name": name, "args": {"requirements": feedback()["requirements"], **fields},
        "id": "dispatch", "type": "tool_call",
    }])])
    result = SupervisorPolicy(model=model).invoke({"goal": "Find a book"})
    assert result["action"] == action
    assert result["task"] is None
    assert result["evidence_refs"] == fields.get("evidence_refs", [])
    assert result["evidence_offset"] == 0
    assert model.calls == 1


def test_tool_schemas_require_only_their_operation_specific_arguments():
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "request_feedback", "args": feedback_intent(), "id": "feedback", "type": "tool_call",
    }])])
    SupervisorPolicy(model=model).invoke({"goal": "Find a book"})
    schemas = {tool.name: tool.args_schema.model_json_schema() for tool in model.tools_seen}
    common = {"requirements", "plan", "loops", "item_reviews", "reconcile_attempts", "subtask_review"}
    extra = {
        "delegate_to_jev": {"task"}, "inspect_evidence": set(), "finish_run": {"evidence_refs"},
        "request_feedback": {
            "question", "feedback_reason", "missing_information", "blocked_requirement_ids", "user_intervention",
        },
    }
    for name, schema in schemas.items():
        assert set(schema["properties"]) == common | extra[name]
        assert schema["additionalProperties"] is False
    assert "task" in schemas["delegate_to_jev"]["required"]
    assert "anyOf" not in schemas["delegate_to_jev"]["properties"]["task"]
    task_schema = schemas["delegate_to_jev"]["$defs"]["DelegateTask"]
    assert "scope" in task_schema["required"]
    assert task_schema["properties"]["scope"]["enum"] == ["general", "discover", "item"]
    assert "evidence_refs" in schemas["finish_run"]["required"]
    assert schemas["finish_run"]["properties"]["evidence_refs"]["minItems"] == 1
    assert {"question", "feedback_reason", "blocked_requirement_ids", "user_intervention"} <= set(
        schemas["request_feedback"]["required"],
    )


@pytest.mark.parametrize("missing_field", ["blocked_requirement_ids", "user_intervention"])
def test_real_feedback_tool_requires_a_user_only_blocker_before_pausing(missing_field):
    arguments = feedback_intent()
    arguments.pop(missing_field)
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "request_feedback", "args": arguments, "id": "unjustified", "type": "tool_call",
    }])])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke({"goal": "Find a book"})
    assert model.calls == 1


@pytest.mark.parametrize("scope_fields", [
    {},
    {"scope": "item"},
    {"scope": "item", "item_ref": "observed-link"},
    {"scope": "item", "loop_id": "results"},
    {"scope": "item", "item_ref": " ", "loop_id": "results"},
    {"scope": "item", "item_ref": "observed-link", "loop_id": " "},
])
def test_real_policy_rejects_missing_scope_or_unidentified_loop_item_without_retry(scope_fields):
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "delegate_to_jev", "id": "unidentified", "type": "tool_call", "args": {
            "requirements": feedback()["requirements"],
            "task": {
                "id": "next", "instruction": "Process the next result", "success_criteria": ["Result processed"],
                **scope_fields,
            },
        },
    }])])
    with pytest.raises(ProviderError):
        SupervisorPolicy(model=model).invoke({"goal": "Find a book"})
    assert model.calls == 1


@pytest.mark.parametrize("scope_fields", [
    {"scope": "discover"},
    {"scope": "item", "item_ref": "f5d3ffa283ef3d4d:node:18", "loop_id": "books"},
])
def test_delegate_scope_and_observed_identity_survive_tool_normalization(scope_fields):
    task = {
        "id": "next", "instruction": "Inspect the observed book result", "success_criteria": ["Book is observed"],
        **scope_fields,
    }
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "delegate_to_jev", "args": {"requirements": feedback()["requirements"], "task": task},
        "id": "next", "type": "tool_call",
    }])])
    result = SupervisorPolicy(model=model).invoke({"goal": "Find a book"})
    assert result["task"] == {"item_ref": None, "loop_id": None, **task}
    assert model.calls == 1


def test_injected_supervisor_subtask_without_scope_keeps_legacy_general_default():
    decision = SupervisorDecision.model_validate({
        "action": "delegate", "requirements": feedback()["requirements"], "task": {
            "id": "search", "instruction": "Find the requested book", "success_criteria": ["Book is found"],
        },
    })
    assert decision.task.scope == "general"
    assert decision.task.loop_id is None


def test_each_invocation_has_its_current_payload_and_system_prompt_without_previous_messages():
    response = AIMessage(content="", tool_calls=[{
        "name": "request_feedback", "args": feedback_intent(), "id": "feedback", "type": "tool_call",
    }])
    model = ToolModel(responses=[response, response])
    policy = SupervisorPolicy(model=model)
    payloads = [{"goal": "Find a book"}, {"goal": "Find a book", "last_result": {"status": "claimed_done"}}]
    for payload in payloads:
        policy.invoke(payload)
    for messages, payload in zip(model.messages_seen, payloads, strict=True):
        assert [message.type for message in messages] == ["system", "human"]
        assert messages[0].content == INSTRUCTIONS
        assert json.loads(messages[1].content) == payload
    assert model.calls == 2


def test_supervisor_setting_fallback_is_independent_and_does_not_change_worker(monkeypatch):
    for field in ("MODEL", "BASE_URL", "API_KEY", "REASONING"):
        monkeypatch.delenv("SUPERVISOR_" + field, raising=False)
        monkeypatch.delenv("VERIFICATION_" + field, raising=False)
    monkeypatch.setenv("TEXT_MODEL", "small-model")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test-only")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "none")
    monkeypatch.setenv("VERIFICATION_MODEL", "review-model")
    monkeypatch.setenv("SUPERVISOR_BASE_URL", "https://supervisor.test/v1/")
    config = supervisor_configuration()
    assert config == {
        "MODEL": "review-model", "BASE_URL": "https://supervisor.test/v1",
        "API_KEY": "test-only", "REASONING": "none",
    }


def test_real_policy_nested_in_durable_supervisor_does_not_inherit_checkpoint(tmp_path):
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[
        {"name": "request_feedback", "args": feedback_intent(), "id": "one", "type": "tool_call"},
    ])])
    browser = Mock(target="policy-tab", observe=Mock(side_effect=lambda **_: page()))
    with SupervisedAgent("https://example.test/", "Find a book", state_dir=tmp_path,
                         browser=browser, supervisor=SupervisorPolicy(model=model)) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "waiting_feedback"
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["metrics"]["supervisor_calls"] == 1
        assert final["metrics"]["supervisor_http_attempts"] == 0
    browser.act.assert_not_called()


def test_large_dom_facts_are_inspectable_in_bounded_slices_without_actions(tmp_path):
    large = page()
    large["actions"] = [
        {"id": f"field-{i}", "node": i, "kind": "fill", "role": "textbox", "label": f"Field {i}", "value": "x" * 2000}
        for i in range(180)
    ]
    browser = Mock(target="large-page", observe=Mock(return_value=large))
    seen, offsets = set(), []

    def policy(payload):
        assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 120 * 1024
        refs = {fact["ref"] for fact in payload["evidence"]}
        assert not refs & seen
        seen.update(refs)
        offsets.append(payload["evidence_offset"])
        result = feedback()
        if payload["next_evidence_offset"] is not None:
            result.update(action="inspect", evidence_offset=payload["next_evidence_offset"])
        return result

    with SupervisedAgent("https://example.test/", "Find a book", state_dir=tmp_path,
                         browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert len(seen) == len(agent.state["evidence"]) and len(offsets) > 1
    browser.act.assert_not_called()
