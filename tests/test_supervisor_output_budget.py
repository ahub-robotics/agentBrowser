"""Supervisor output limits and truncation recovery use one counted call per turn."""

import json
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from langchain_openai import ChatOpenAI
from test_agent import page
from test_coordinator_recovery import GOAL, browser_for, contract
from test_supervisor_policy import feedback_intent

from agentrun import SupervisedAgent
from agentrun.supervisor_policy import SupervisorPolicy
from AgentBrowser import agent as worker_module
from AgentBrowser import model

TOOLS = {"initialize": "initialize_plan", "audit": "audit_plan", "review": "review_progress"}


def fields_for(phase):
    if phase == "initialize":
        return {"contract": contract()}
    if phase == "audit":
        return {"approved": True}
    if phase == "review":
        return {"next_action": "stop", "stop_reason": "Offline test boundary."}
    return feedback_intent()


def completion(phase, *, fields=None, finish="tool_calls"):
    fields = fields_for(phase) if fields is None else fields
    return {
        "id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": "offline",
        "choices": [{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "offline-tool", "type": "function", "function": {
                    "name": TOOLS.get(phase, "request_feedback"),
                    "arguments": fields if isinstance(fields, str) else json.dumps(fields),
                },
            }],
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


@contextmanager
def policy_for(responses):
    responses = iter(responses)
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        response = next(responses)
        if callable(response):
            response = response(requests[-1])
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        policy = SupervisorPolicy(model=ChatOpenAI(
            model="offline", api_key="offline-test-key", base_url="https://openrouter.ai/api/v1",
            max_retries=0, http_client=client,
        ))
        yield policy, requests


def payload_for(phase, **extra):
    return {"contract_version": 2, "phase": phase, "goal": GOAL, **extra}


def output_limit(request):
    limits = [request[key] for key in ("max_tokens", "max_completion_tokens") if key in request]
    assert len(limits) == 1, "The actual HTTP request must contain exactly one output limit"
    return limits[0]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No paid APIs"))))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text API")))
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=AssertionError("No browser work before audit")))


@pytest.mark.parametrize(("phase", "expected"), [
    ("initialize", 4096), ("audit", 2048), ("review", 2048), (None, 4096),
])
def test_default_output_limit_reaches_real_sdk_request(phase, expected):
    with policy_for([completion(phase)]) as (policy, requests):
        policy.invoke(payload_for(phase) if phase else {"goal": "Find a book"})
        assert policy.http_attempts == len(requests) == 1
        assert output_limit(requests[0]) == expected


@pytest.mark.parametrize(("phase", "expected"), [("initialize", 8192), ("audit", 8192), ("review", 4096)])
def test_matching_truncation_marker_expands_only_next_explicit_call(phase, expected):
    with policy_for([completion(phase), completion(phase), completion(phase)]) as (policy, requests):
        policy.invoke(payload_for(phase))
        policy.invoke(payload_for(phase, validation_error={"output_token_limit": True, "phase": phase}))
        policy.invoke(payload_for(phase))
        assert policy.http_attempts == len(requests) == 3
        assert [output_limit(request) for request in requests] == [
            4096 if phase == "initialize" else 2048, expected, 4096 if phase == "initialize" else 2048,
        ]


@pytest.mark.parametrize("marker", [
    None, {}, {"output_token_limit": True, "phase": "initialize"},
    {"output_token_limit": False, "phase": "audit"}, {"phase": "audit"},
    {"output_token_limit": "yes", "phase": "audit"},
    {"reason": "Invalid response", "phase": "audit"},
])
def test_unrelated_validation_errors_do_not_raise_audit_reservation(marker):
    with policy_for([completion("audit")]) as (policy, requests):
        policy.invoke(payload_for("audit", validation_error=marker))
        assert output_limit(requests[0]) == 2048
        assert policy.http_attempts == len(requests) == 1


@pytest.mark.parametrize("phase", ["initialize", "audit", "review"])
@pytest.mark.parametrize("valid_json", [True, False])
def test_truncation_is_rejected_even_when_tool_json_would_be_valid(phase, valid_json):
    fields = fields_for(phase) if valid_json else '{"unfinished":'
    with policy_for([completion(phase, fields=fields, finish="length")]) as (policy, requests):
        with pytest.raises(model.ProviderError) as caught:
            policy.invoke(payload_for(phase))
        assert caught.value.kind == "invalid_response"
        assert caught.value.metadata["provider_diagnostic"]["error_code"] == "output_token_limit"
        assert policy.http_attempts == len(requests) == 1


def test_invalid_json_without_truncation_does_not_claim_output_limit():
    with policy_for([completion("audit", fields='{"unfinished":')]) as (policy, requests):
        with pytest.raises(model.ProviderError) as caught:
            policy.invoke(payload_for("audit"))
        assert caught.value.kind == "invalid_response"
        assert caught.value.metadata.get("provider_diagnostic", {}).get("error_code") != "output_token_limit"
        assert policy.http_attempts == len(requests) == 1


def test_large_corrected_contract_is_accepted_on_expanded_audit_call():
    replacement = contract()
    for index in range(18):
        replacement["tasks"].append({
            **deepcopy(replacement["tasks"][0]), "id": f"follow-up-{index}",
            "instruction": "Inspect only the next pending outcome. " * 20,
        })
    fields = {"approved": False, "problems": ["The original plan omitted dependent work."],
              "replacement_contract": replacement}
    assert len(json.dumps(fields)) > 16000
    with policy_for([completion("audit", fields=fields)]) as (policy, requests):
        response = policy.invoke(payload_for("audit", validation_error={"output_token_limit": True, "phase": "audit"}))
        assert len(response["replacement_contract"]["tasks"]) == 19
        assert output_limit(requests[0]) == 8192
        assert policy.http_attempts == len(requests) == 1


def agent_for(tmp_path, policy):
    browser = browser_for()
    browser.target = f"output-budget-{tmp_path.name}"
    return SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, run_id="output-budget",
                           supervisor=policy, browser=browser, trace=False)


def test_graph_uses_single_validation_retry_then_resets_phase_budget(tmp_path):
    responses = [completion("initialize", finish="length"), completion("initialize"),
                 completion("audit"), completion("review")]
    with policy_for(responses) as (policy, requests), agent_for(tmp_path, policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_stopped"
        assert final["metrics"]["supervisor_calls"] == policy.http_attempts == len(requests) == 4
        assert [output_limit(request) for request in requests] == [4096, 8192, 2048, 2048]
        assert agent.state["validation_retries"] == 0
        assert "validation_error" not in agent.state
        assert final["metrics"]["actions"] == final["metrics"]["jev_calls"] == 0
        agent.browser.act.assert_not_called()


def test_second_truncation_stops_without_applying_partial_contract_or_extra_call(tmp_path):
    with policy_for([completion("initialize", finish="length")] * 2) as (policy, requests):
        with agent_for(tmp_path, policy) as agent:
            final = list(agent.run())[-1]
            assert final["status"] == "unverified" and final["stop_reason"] == "supervisor_invalid_response"
            assert agent.state["contract"] is None and agent.state["contract_phase"] == "initialize"
            assert agent.state["pending_delegation"] is None
            assert final["metrics"]["supervisor_calls"] == policy.http_attempts == len(requests) == 2
            assert [output_limit(request) for request in requests] == [4096, 8192]
            agent.browser.act.assert_not_called()


def test_truncation_retry_marker_survives_resume_before_next_call(tmp_path):
    with policy_for([completion("initialize"), completion("audit", finish="length")]) as (policy, requests):
        with agent_for(tmp_path, policy) as agent:
            agent._decide()
            agent._decide()
            saved = agent.store.load_state()
            assert saved["validation_error"]["output_token_limit"] is True
            assert saved["validation_error"]["phase"] == "audit"
            assert saved["validation_retries"] == 1
            assert agent.state["contract_phase"] == "audit"
            target = agent.browser.target
            assert len(requests) == 2
    browser = browser_for()
    browser.target = target
    with policy_for([completion("audit"), completion("review")]) as (policy, requests):
        with SupervisedAgent.resume("output-budget", state_dir=tmp_path, browser=browser,
                                    supervisor=policy, trace=False) as resumed:
            final = list(resumed.run())[-1]
            assert final["stop_reason"] == "supervisor_stopped"
            assert [output_limit(request) for request in requests] == [8192, 2048]
            assert final["metrics"]["supervisor_calls"] == 4
            assert final["metrics"]["actions"] == final["metrics"]["jev_calls"] == 0
            browser.act.assert_not_called()


def test_first_durable_validation_retry_already_contains_truncation_marker(tmp_path, monkeypatch):
    with policy_for([completion("initialize", finish="length")]) as (policy, requests):
        with agent_for(tmp_path, policy) as agent:
            save = agent.store.save_state
            retry_checkpoints = []

            def inspect_checkpoint(state):
                if state.get("validation_retries") == 1:
                    retry_checkpoints.append(deepcopy(state))
                    assert state["validation_error"]["output_token_limit"] is True
                    assert state["validation_error"]["phase"] == "initialize"
                return save(state)

            monkeypatch.setattr(agent.store, "save_state", inspect_checkpoint)
            agent._decide()
            assert retry_checkpoints and len(requests) == 1
            assert retry_checkpoints[0]["contract"] is None
            agent.browser.act.assert_not_called()


def test_credits_failure_does_not_expand_budget_or_retry_internally(tmp_path):
    denied = httpx.Response(402, json={"error": {
        "code": 402, "message": "Maximum request cost exceeds credits.",
        "metadata": {"limit_source": "openrouter_credits", "reason": "weight_exceeds_budget"},
    }})
    with policy_for([completion("initialize"), denied]) as (policy, requests), agent_for(tmp_path, policy) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "blocked" and final["stop_reason"] == "supervisor_provider_error"
        assert final["metrics"]["supervisor_calls"] == policy.http_attempts == len(requests) == 2
        assert [output_limit(request) for request in requests] == [4096, 2048]
        assert agent.state["provider_retry_count"] == 0
        assert agent.state["validation_retries"] == 0
        assert "validation_error" not in agent.state
        assert policy.last_usage == {}
        agent.browser.act.assert_not_called()


def test_smaller_audit_reservation_passes_provider_that_rejects_plan_sized_limit(tmp_path):
    def audit_endpoint(request):
        if output_limit(request) > 2048:
            return httpx.Response(402, json={"error": {
                "code": 402, "message": "Maximum request cost exceeds credits.",
                "metadata": {"limit_source": "openrouter_credits", "reason": "weight_exceeds_budget"},
            }})
        return completion("audit")

    with policy_for([completion("initialize"), audit_endpoint, completion("review")]) as (policy, requests):
        with agent_for(tmp_path, policy) as agent:
            final = list(agent.run())[-1]
            assert final["stop_reason"] == "supervisor_stopped"
            assert agent.state["contract_phase"] == "review"
            assert [output_limit(request) for request in requests] == [4096, 2048, 2048]
            assert final["metrics"]["supervisor_calls"] == policy.http_attempts == len(requests) == 3
            assert agent.state["provider_retry_count"] == 0 and "provider_error" not in agent.state
            agent.browser.act.assert_not_called()
