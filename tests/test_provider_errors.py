"""Provider failures are diagnosed locally, without network or browser input."""

import json
import logging
from unittest.mock import Mock

import httpx
import pytest
from test_verification_workflow import pick, session

from AgentBrowser import agent as loop
from AgentBrowser import model
from AgentBrowser.candidates import SerializedBody
from AgentBrowser.verification import VerificationError, verify


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    client = Mock()
    client.post.side_effect = AssertionError("Unexpected provider call in offline test")
    monkeypatch.setattr(model, "CLIENT", client)
    monkeypatch.setattr(model.time, "sleep", Mock())
    return client


@pytest.mark.parametrize(("status", "kind", "attempts"), [
    (400, "request", 1), (401, "authentication", 1), (402, "billing", 1),
    (403, "authentication", 1), (429, "rate_limit", 3), (500, "unavailable", 1),
    (503, "unavailable", 3), (529, "unavailable", 3),
])
def test_http_error_preserves_safe_category_and_existing_retry_limits(offline, status, kind, attempts):
    offline.post.side_effect = None
    offline.post.return_value = httpx.Response(status, json={"error": "PRIVATE_PROVIDER_RESPONSE"})
    body = SerializedBody({"private": "PRIVATE_REQUEST"})
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://provider.invalid/private", "PRIVATE_KEY", body)
    assert caught.value.kind == kind
    assert caught.value.status_code == status
    assert body.http_attempts == offline.post.call_count == attempts
    assert "PRIVATE" not in str(caught.value)
    assert "HTTP " + str(status) in str(caught.value)


@pytest.mark.parametrize(("error", "kind"), [
    (httpx.ReadTimeout("PRIVATE_TIMEOUT"), "timeout"),
    (httpx.ConnectError("PRIVATE_CONNECTION"), "connection"),
])
def test_connection_failure_has_no_raw_exception_or_automatic_retry(offline, error, kind):
    offline.post.side_effect = error
    body = SerializedBody({})
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://provider.invalid", "PRIVATE_KEY", body)
    assert caught.value.kind == kind and caught.value.status_code is None
    assert body.http_attempts == offline.post.call_count == 1
    assert "PRIVATE" not in str(caught.value)


def test_existing_transient_transport_retry_can_still_succeed(offline):
    offline.post.side_effect = [httpx.Response(503), httpx.Response(200, json={"ok": True})]
    body = SerializedBody({})
    assert model.post_json("https://provider.invalid", "PRIVATE_KEY", body) == {"ok": True}
    assert body.http_attempts == 2


def test_invalid_http_json_is_safe_and_keeps_attempt_count(offline):
    offline.post.side_effect = None
    offline.post.return_value = httpx.Response(200, text="PRIVATE_RESPONSE")
    body = SerializedBody({})
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://provider.invalid", "PRIVATE_KEY", body)
    assert caught.value.kind == "invalid_response"
    assert caught.value.status_code == 200
    assert body.http_attempts == 1
    assert "PRIVATE" not in str(caught.value)


def test_billing_failure_survives_review_and_logging_without_browser_input(offline, monkeypatch, caplog):
    monkeypatch.setenv("VERIFICATION_API_KEY", "PRIVATE_KEY")
    monkeypatch.setenv("VERIFICATION_BASE_URL", "https://provider.invalid")
    monkeypatch.setenv("VERIFICATION_MODEL", "test-model")
    offline.post.side_effect = None
    offline.post.return_value = httpx.Response(402, json={"error": "PRIVATE_RESPONSE"})
    agent, browser = session(monkeypatch, verify)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    caplog.set_level(logging.INFO, logger="agentrun")
    state = agent.command("tick")
    assert state["status"] == "unverified"
    assert state["stop_reason"] == "verification_provider_error"
    assert state["verification_calls"] == state["verification_http_attempts"] == 1
    error = state["verification_reviews"][-1]
    assert error["provider_error_kind"] == "billing"
    assert error["provider_http_status"] == 402
    assert "HTTP 402" in caplog.text and "crédito o límite de gasto" in caplog.text
    assert "PRIVATE" not in caplog.text + json.dumps(error)
    assert state["history"] == []
    browser.act.assert_not_called()
    offline.post.assert_called_once()


def test_unknown_provider_error_text_is_not_added_to_diagnostics(monkeypatch):
    monkeypatch.setenv("VERIFICATION_API_KEY", "PRIVATE_KEY")
    monkeypatch.setattr(model, "post_json", Mock(side_effect=RuntimeError("PRIVATE_RESPONSE")))
    with pytest.raises(VerificationError) as caught:
        verify({"goal": "Open an item", "evidence": [], "selected_action": {"operation": "CLICK"}})
    assert caught.value.metadata["provider_error_kind"] == "unknown"
    assert "PRIVATE" not in str(caught.value) + json.dumps(caught.value.metadata)


@pytest.mark.parametrize(("status", "source", "reason"), [
    (402, "openrouter_credits", None),
    (402, "openrouter_key_limit", None),
    (402, "openrouter_in_flight_budget", "in_flight_budget_exhausted"),
    (402, "openrouter_in_flight_budget", "weight_exceeds_budget"),
    (403, "openrouter_guardrail_budget", None),
    (403, "openrouter_workspace_budget", None),
])
def test_documented_openrouter_limits_keep_only_safe_categories(offline, status, source, reason):
    offline.post.side_effect = None
    offline.post.return_value = httpx.Response(status, json={"error": {
        "message": "PRIVATE_MESSAGE", "metadata": {
            "limit_source": source, "reason": reason, "remedy_hint": "PRIVATE_HINT", "raw": "PRIVATE_RAW",
        },
    }})
    body = SerializedBody({})
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://openrouter.ai/api/v1/chat/completions", "PRIVATE_KEY", body)
    assert caught.value.kind == "billing"
    assert caught.value.metadata == {
        "provider_error_kind": "billing", "provider_http_status": status, "provider_limit_source": source,
        **({"provider_limit_reason": reason} if reason else {}),
    }
    assert "PRIVATE" not in str(caught.value) + json.dumps(caught.value.metadata)
    assert body.http_attempts == 1
    offline.post.assert_called_once()
    assert json.loads(offline.post.call_args.kwargs["content"])["usage"] == {"include": True}
    model.time.sleep.assert_not_called()


@pytest.mark.parametrize(("url", "recognized"), [
    ("https://openrouter.ai/api/v1/chat/completions", True),
    ("https://eu.openrouter.ai/api/v1/chat/completions", True),
    ("https://us.openrouter.ai/api/v1/chat/completions", True),
    ("https://OPENROUTER.AI/api/v1/chat/completions", True),
    ("https://other-provider.invalid/api/v1/chat/completions", False),
    ("https://openrouter.ai.attacker.invalid/api/v1", False),
    ("https://attacker.invalid/openrouter.ai/api/v1", False),
    ("https://openrouter.ai@attacker.invalid/api/v1", False),
    ("https://attacker.invalid/?next=https://openrouter.ai", False),
    ("https://sub.openrouter.ai/api/v1", False),
    ("https://[malformed", False),
])
def test_openrouter_limit_metadata_requires_an_exact_documented_hostname(offline, url, recognized):
    offline.post.side_effect = None
    offline.post.return_value = httpx.Response(402, json={"error": {"metadata": {
        "limit_source": "openrouter_key_limit",
    }}})
    with pytest.raises(model.ProviderError) as caught:
        model.post_json(url, "PRIVATE_KEY", {})
    assert ("provider_limit_source" in caught.value.metadata) is recognized
    assert caught.value.kind == "billing" and caught.value.status_code == 402


@pytest.mark.parametrize("body", [
    None, [], "PRIVATE_RESPONSE", {"error": None}, {"error": []}, {"error": "PRIVATE_ERROR"},
    {"error": {"metadata": "PRIVATE_METADATA"}}, {"error": {"metadata": []}},
    {"error": {"metadata": {"limit_source": "PRIVATE_SOURCE", "reason": "PRIVATE_REASON"}}},
    {"error": {"metadata": {"limit_source": ["openrouter_key_limit"]}}},
    {"error": {"metadata": {"limit_source": {"PRIVATE_SOURCE": "openrouter_key_limit"}}}},
    {"error": {"metadata": {"limit_source": "openrouter_workspace_budget"}}},
    {"metadata": {"limit_source": "openrouter_key_limit"}},
])
def test_malformed_or_unknown_error_metadata_preserves_original_http_failure(offline, body):
    offline.post.side_effect = None
    offline.post.return_value = httpx.Response(402, json=body)
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://openrouter.ai/api/v1/chat/completions", "PRIVATE_KEY", {})
    assert caught.value.metadata == {"provider_error_kind": "billing", "provider_http_status": 402}
    assert "PRIVATE" not in str(caught.value)


def test_non_json_error_body_preserves_original_http_failure(offline):
    offline.post.side_effect = None
    offline.post.return_value = httpx.Response(402, content=b"\xffPRIVATE_ERROR")
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://openrouter.ai/api/v1/chat/completions", "PRIVATE_KEY", {})
    assert caught.value.kind == "billing" and caught.value.status_code == 402
    assert "PRIVATE" not in str(caught.value) + json.dumps(caught.value.metadata)


@pytest.mark.parametrize(("status", "source", "reason", "expected"), [
    (402, "openrouter_in_flight_budget", "PRIVATE_REASON", {"provider_limit_source": "openrouter_in_flight_budget"}),
    (402, "openrouter_in_flight_budget", [], {"provider_limit_source": "openrouter_in_flight_budget"}),
    (402, "openrouter_in_flight_budget", {"PRIVATE": True},
     {"provider_limit_source": "openrouter_in_flight_budget"}),
    (402, "openrouter_credits", "weight_exceeds_budget",
     {"provider_limit_source": "openrouter_credits", "provider_limit_reason": "weight_exceeds_budget"}),
    (402, "openrouter_credits", "in_flight_budget_exhausted", {"provider_limit_source": "openrouter_credits"}),
    (402, "openrouter_key_limit", "weight_exceeds_budget", {"provider_limit_source": "openrouter_key_limit"}),
    (402, None, "weight_exceeds_budget", {}),
    (403, "openrouter_key_limit", None, {}),
    (429, "openrouter_in_flight_budget", "in_flight_budget_exhausted", {}),
])
def test_limit_categories_and_reasons_are_sanitized_as_a_pair(status, source, reason, expected):
    error = model.ProviderError("billing", status_code=status, limit_source=source, limit_reason=reason)
    assert error.metadata == {"provider_error_kind": "billing", "provider_http_status": status, **expected}


@pytest.mark.parametrize(("status", "source", "reason", "cause"), [
    (402, "openrouter_key_limit", None, "límite de gasto de la clave"),
    (402, "openrouter_in_flight_budget", "in_flight_budget_exhausted", "presupuesto temporal"),
    (402, "openrouter_in_flight_budget", "weight_exceeds_budget", "coste de la petición"),
    (403, "openrouter_workspace_budget", None, "presupuesto de la organización"),
])
def test_limit_diagnostics_survive_review_and_logs_without_browser_input(
    offline, monkeypatch, caplog, status, source, reason, cause,
):
    monkeypatch.setenv("VERIFICATION_API_KEY", "PRIVATE_KEY")
    monkeypatch.setenv("VERIFICATION_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("VERIFICATION_MODEL", "test-model")
    offline.post.side_effect = None
    offline.post.return_value = httpx.Response(status, json={"error": {
        "message": "PRIVATE_MESSAGE", "metadata": {
            "limit_source": source, "reason": reason, "remedy_hint": "PRIVATE_HINT",
        },
    }})
    agent, browser = session(monkeypatch, verify)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    caplog.set_level(logging.DEBUG, logger="agentrun")
    state = agent.command("tick")
    review = state["verification_reviews"][-1]
    assert state["status"] == "unverified" and state["history"] == []
    assert review["provider_error_kind"] == "billing" and review["provider_http_status"] == status
    assert review["provider_limit_source"] == source
    if reason:
        assert review["provider_limit_reason"] == reason
    assert cause in caplog.text and "PRIVATE" not in caplog.text + json.dumps(review)
    event = next(record.agentrun_event for record in caplog.records if hasattr(record, "agentrun_event")
                 and record.agentrun_event["event"] == "run_stopped")
    assert event["provider_limit_source"] == source
    assert state["verification_calls"] == state["verification_http_attempts"] == 1
    browser.act.assert_not_called()
    offline.post.assert_called_once()
