"""Trace native/SDK transport using only simulated endpoints and browser input."""

import gzip
import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from test_agent import choice, page
from test_supervisor_policy import ToolModel
from test_supervisor_policy_v2 import payload, tool_call

from agentrun.supervisor_policy import V2_INSTRUCTIONS, SupervisorPolicy
from AgentBrowser import model
from AgentBrowser.agent import Agent
from AgentBrowser.candidates import SerializedBody
from AgentBrowser.observability import TraceConfig, TraceContext


def make_trace(tmp_path, name="transport"):
    return TraceContext(TraceConfig(directory=tmp_path), run_id=name)


def records(trace):
    return [json.loads(line) for path in sorted((trace.directory / "events").glob("*.jsonl"))
            for line in path.read_text().splitlines()]


def event_payload(trace, event):
    return json.loads(gzip.decompress((trace.directory / event["payload_ref"]["path"]).read_bytes()))


def trace_text(trace):
    parts = []
    for path in trace.directory.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            parts.append((gzip.decompress(content) if path.suffix == ".gz" else content).decode())
    return "\n".join(parts)


def test_native_exact_serialized_wire_and_registration_before_capture(tmp_path, monkeypatch):
    key = "native-not-in-environment-credential"
    body = SerializedBody({"model": "offline", "messages": [{"role": "user", "content": "á " + key}]})
    body["messages"][0]["content"] = "mutated mapping; cached wire must win"
    response = httpx.Response(200, json={"answer": "result " + key}, headers={"set-cookie": "cookie-private"})
    post = Mock(return_value=response)
    monkeypatch.setattr(model.CLIENT, "post", post)
    trace = make_trace(tmp_path)
    with trace.call("text", "write") as call:
        result = model.post_json("https://endpoint.invalid/?api_key=" + key, key, body)
    assert result == {"answer": "result " + key}
    post.assert_called_once_with("https://endpoint.invalid/?api_key=" + key, content=body.content, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
    })
    events = records(trace)
    request = next(event for event in events if event["event"] == "http.request")
    response_event = next(event for event in events if event["event"] == "http.response")
    assert request["request_bytes"] == len(body.content)
    assert request["request_sha256"] == hashlib.sha256(body.content).hexdigest()
    assert request["call_id"] == response_event["call_id"] == call.call_id
    assert request["attempt"] == body.http_attempts == 1
    assert event_payload(trace, request)["messages"][0]["content"] == "á [REDACTED]"
    assert event_payload(trace, response_event) == {"answer": "result [REDACTED]"}
    assert response_event["response_sha256"] == hashlib.sha256(response.content).hexdigest()
    assert request["redacted"] and response_event["redacted"]
    text = trace_text(trace)
    assert key not in text and "cookie-private" not in text and "Authorization" not in text
    assert "mutated mapping" not in text and "raw_utf8" not in text


def test_native_retries_and_wire_are_identical_with_trace_on_off(tmp_path, monkeypatch):
    outcomes = []
    for enabled in (False, True):
        body = SerializedBody({"model": "offline", "questions": {"operation": {"criteria": {"DONE": {}}}}})
        post = Mock(side_effect=[httpx.Response(503, json={"error": "retry"}),
                                 httpx.Response(429, json={"error": "slow"}), httpx.Response(200, json={"ok": True})])
        sleep = Mock()
        monkeypatch.setattr(model.CLIENT, "post", post)
        monkeypatch.setattr(model.time, "sleep", sleep)
        trace = make_trace(tmp_path) if enabled else None
        with trace.bind() if trace else nullcontext():
            result = model.post_json("https://endpoint.invalid", "wire-key", body)
        outcomes.append((result, body.http_attempts, post.call_args_list, sleep.call_args_list))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][1] == 3
    events = records(trace)
    assert [event["attempt"] for event in events if event["event"] == "http.request"] == [1, 2, 3]
    assert [event["status_code"] for event in events if event["event"] == "http.response"] == [503, 429, 200]
    assert [event["delay_seconds"] for event in events if event["event"] == "http.retry"] == [0.5, 1.0]


@pytest.mark.parametrize(("status", "content", "expected"), [
    (400, b'{"error":{"message":"echo wire-secret","code":"invalid_request_error"}}', {"error": {
        "message": "echo [REDACTED]", "code": "invalid_request_error",
    }}),
    (200, b"not JSON wire-secret", {"raw_utf8": "not JSON [REDACTED]", "json_valid": False}),
    (200, b"\xffwire-secret", {"decoding_failure": "invalid_utf8", "raw_omitted": True}),
])
def test_failed_response_is_captured_before_rejection_without_retry(tmp_path, monkeypatch, status, content, expected):
    post = Mock(return_value=httpx.Response(status, content=content))
    monkeypatch.setattr(model.CLIENT, "post", post)
    trace = make_trace(tmp_path)
    body = SerializedBody({"model": "offline", "messages": []})
    with trace.bind(), pytest.raises(model.ProviderError):
        model.post_json("https://endpoint.invalid", "wire-secret", body)
    events = [event for event in records(trace) if event["event"].startswith("http.")]
    assert [event["event"] for event in events] == ["http.request", "http.response", "http.error"]
    assert event_payload(trace, events[1]) == expected
    assert events[1]["response_bytes"] == len(content)
    assert body.http_attempts == post.call_count == 1
    assert "wire-secret" not in trace_text(trace)
    assert "base64" not in trace_text(trace)


@pytest.mark.parametrize(("exception", "kind"), [
    (httpx.ReadTimeout("echo wire-secret"), "timeout"),
    (httpx.ConnectError("echo wire-secret"), "connection"),
    (RuntimeError("echo wire-secret"), "unexpected"),
])
def test_transport_errors_have_safe_attempt_metadata(tmp_path, monkeypatch, exception, kind):
    post = Mock(side_effect=exception)
    monkeypatch.setattr(model.CLIENT, "post", post)
    trace = make_trace(tmp_path)
    body = SerializedBody({"model": "offline"})
    with trace.bind(), pytest.raises((model.ProviderError, RuntimeError)):
        model.post_json("https://endpoint.invalid", "wire-secret", body)
    errors = [event for event in records(trace) if event["event"] == "http.error"]
    assert len(errors) == 1 and errors[0]["error_kind"] == kind
    assert errors[0]["attempt"] == body.http_attempts == post.call_count == 1
    assert errors[0]["latency_ms"] >= 0
    assert "echo" not in trace_text(trace) and "wire-secret" not in trace_text(trace)


def test_supervisor_captures_exact_messages_schema_and_validated_tool(tmp_path):
    fields = {"next_action": "observe"}
    offline = ToolModel(responses=[tool_call("review_progress", fields)])
    policy = SupervisorPolicy(model=offline)
    trace = make_trace(tmp_path)
    request = payload("review", available_actions=["continue", "observe"])
    with trace.call("supervisor", "review") as call:
        result = policy.invoke(request)
    events = records(trace)
    incoming = next(event for event in events if event["event"] == "langchain.request")
    incoming_payload = event_payload(trace, incoming)
    assert incoming_payload["messages"][0][0]["content"] == V2_INSTRUCTIONS
    assert json.loads(incoming_payload["messages"][0][1]["content"]) == request
    assert incoming_payload["tools"][0]["function"]["name"] == "review_progress"
    assert incoming_payload["tools"][0]["function"]["parameters"]["properties"]["next_action"]["enum"] == [
        "continue", "observe",
    ]
    assert incoming_payload["model_options"]["tool_choice"] == "required"
    outgoing = next(event for event in events if event["event"] == "langchain.response")
    assert event_payload(trace, outgoing)["messages"][0][0]["tool_calls"][0]["args"] == fields
    assert incoming["call_id"] == outgoing["call_id"] == call.call_id
    assert any(event["event"] == "langchain.tool_result" for event in events)
    assert any(event["event"] == "supervisor.raw_result" for event in events)
    assert next(event for event in events if event["event"] == "supervisor.validation")["accepted"] is True
    assert result["next_action"] == "observe" and offline.calls == 1 and policy.http_attempts == 0
    assert sum(event["event"] == "call.started" for event in events) == 1


@pytest.mark.parametrize("response", [
    AIMessage(content="Cannot use a tool; echo supervisor-wire-secret"),
    tool_call("review_progress", {"next_action": "continue", "invented": "supervisor-wire-secret"}),
    AIMessage(content="", tool_calls=[
        {"name": "review_progress", "args": {"next_action": "continue"}, "id": "one", "type": "tool_call"},
        {"name": "review_progress", "args": {"next_action": "observe"}, "id": "two", "type": "tool_call"},
    ]),
])
def test_supervisor_captures_output_before_validation_rejects(tmp_path, monkeypatch, response):
    monkeypatch.setenv("SUPERVISOR_API_KEY", "supervisor-wire-secret")
    offline = ToolModel(responses=[response])
    policy = SupervisorPolicy(model=offline)
    trace = make_trace(tmp_path)
    with trace.bind(), pytest.raises(model.ProviderError):
        policy.invoke(payload("review"))
    events = records(trace)
    response_index = next(index for index, event in enumerate(events) if event["event"] == "langchain.response")
    rejection_index = next(index for index, event in enumerate(events) if event["event"] == "supervisor.validation")
    assert response_index < rejection_index
    captured = event_payload(trace, events[response_index])["messages"][0][0]
    assert "content" in captured and "tool_calls" in captured
    assert offline.calls == 1 and policy.http_attempts == 0
    assert not events[rejection_index]["accepted"]
    assert "supervisor-wire-secret" not in trace_text(trace)


def test_supervisor_sdk_trace_preserves_exact_wire_and_http_counter(tmp_path):
    outcomes = []
    for enabled in (False, True):
        wires = []

        def transport(request):
            wires.append(request.content)
            return httpx.Response(200, json={
                "id": "offline-completion", "object": "chat.completion", "created": 0, "model": "offline",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{"id": "one", "type": "function",
                        "function": {"name": "review_progress", "arguments": '{"next_action":"continue"}'}}],
                }}], "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            })

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            sdk = ChatOpenAI(model="offline", api_key="sdk-not-in-environment-key", base_url="https://endpoint.invalid/v1",
                             max_retries=0, http_client=client, temperature=0.3)
            policy = SupervisorPolicy(model=sdk)
            trace = make_trace(tmp_path) if enabled else None
            with trace.bind() if trace else nullcontext():
                result = policy.invoke(payload("review", goal="echo sdk-not-in-environment-key"))
            outcomes.append((result, wires, policy.http_attempts))
    assert outcomes[0] == outcomes[1] and outcomes[1][2] == 1
    event = next(event for event in records(trace) if event["event"] == "langchain.request")
    captured = event_payload(trace, event)
    assert event["http_attempt"] == 1
    assert captured["tools_source"] == "invocation_params"
    assert captured["model_options"]["temperature"] == 0.3
    assert captured["model_options"]["max_tokens"] == 2048
    assert "sdk-not-in-environment-key" not in trace_text(trace)
    assert "Authorization" not in trace_text(trace) and "http_client" not in trace_text(trace)


def test_supervisor_sdk_error_captures_redacted_provider_body_without_retry(tmp_path):
    requests = []

    def transport(request):
        requests.append(request.content)
        return httpx.Response(400, json={"error": {
            "code": "invalid_request_error", "message": "echo injected-credential",
            "metadata": {"password": "provider-private-password"},
        }}, headers={"x-private-header": "provider-private-header"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        sdk = ChatOpenAI(model="offline", api_key="injected-credential", base_url="https://endpoint.invalid/v1",
                         max_retries=0, http_client=client)
        policy = SupervisorPolicy(model=sdk)
        trace = make_trace(tmp_path)
        with trace.bind(), pytest.raises(model.ProviderError) as caught:
            policy.invoke(payload("review"))
    assert caught.value.status_code == 400
    assert policy.http_attempts == len(requests) == 1
    events = records(trace)
    error = next(event for event in events if event["event"] == "langchain.error")
    assert error["status_code"] == 400
    captured = event_payload(trace, error)
    assert captured["body"]["message"] == "echo [REDACTED]"
    assert captured["body"]["metadata"]["password"] == "[REDACTED]"
    assert "injected-credential" not in trace_text(trace)
    assert "provider-private-password" not in trace_text(trace)
    assert "provider-private-header" not in trace_text(trace)


def test_degraded_writer_does_not_change_transport_retries_or_success(tmp_path, monkeypatch):
    trace = make_trace(tmp_path)
    # A write failure must not turn a successful POST into a repeat or error.
    (trace.directory / "blobs").rename(trace.directory / "old-blobs")
    post = Mock(side_effect=[httpx.Response(503, json={"error": "retry"}), httpx.Response(200, json={"ok": True})])
    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model.time, "sleep", Mock())
    body = SerializedBody({"model": "offline"})
    with trace.bind():
        assert model.post_json("https://endpoint.invalid", "offline-credential", body) == {"ok": True}
    assert trace.degraded and post.call_count == body.http_attempts == 2


def test_native_agent_trace_preserves_single_browser_mutation_and_model_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-native-credential")
    outcomes = []
    for enabled in (False, True):
        wires = []

        def post(_url, *, content, headers):
            wires.append(content)
            body = json.loads(content)
            questions = body["questions"]
            return httpx.Response(200, json={"model": "offline", "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(questions["click_target"]["criteria"], "2"),
            }})

        monkeypatch.setattr(model.CLIENT, "post", post)
        browser = Mock(observe=Mock(side_effect=lambda **_kwargs: deepcopy(page())), fresh=Mock(return_value=True))
        agent = Agent("https://example.test/", "Open the search", browser=browser, verification=False,
                      trace=TraceConfig(directory=tmp_path / "agent") if enabled else False)
        agent.command("tick")
        outcomes.append((wires, browser.act.call_args_list, agent.state["model_calls"],
                         agent.state["model_http_attempts"], len(agent.state["history"])))
    assert outcomes[0] == outcomes[1]
    assert outcomes[1][2:] == (1, 1, 1)
    assert len(outcomes[1][1]) == 1
