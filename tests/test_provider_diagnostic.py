"""Request diagnostics retain dimensions and correlation IDs without provider prose."""

import hashlib
import json
from unittest.mock import Mock

import httpx
import pytest

from AgentBrowser import model
from AgentBrowser.candidates import SerializedBody


@pytest.fixture
def client(monkeypatch):
    client = Mock()
    monkeypatch.setattr(model, "CLIENT", client)
    monkeypatch.setattr(model.time, "sleep", Mock())
    return client


def body():
    return SerializedBody({
        "model": "jev-latest", "state": {"text": "PRIVATE_PAGE"},
        "questions": {
            "operation": {"criteria": {"CLICK": "Click", "DONE": "Done"}},
            "click_target": {"criteria": {"1": "PRIVATE_LABEL", "2": "PRIVATE_VALUE"}},
        },
    })


def test_400_records_exact_wire_dimensions_and_safe_correlation_without_retry(client):
    request = body()
    # Diagnostics describe bytes actually sent, even if a caller mutated the mapping.
    request["model"] = "mutated-model"
    client.post.return_value = httpx.Response(400, headers={"x-request-id": "request-abc_123"}, json={
        "error": {"code": "invalid_request_error", "param": "questions", "message": "PRIVATE_RESPONSE",
                  "value": "PRIVATE_VALUE", "raw": "PRIVATE_RAW"},
    })
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://provider.invalid/PRIVATE_URL", "PRIVATE_KEY", request)
    assert caught.value.metadata["provider_diagnostic"] == {
        "component": "jev", "model": "jev-latest", "request_bytes": len(request.content),
        "request_sha256": hashlib.sha256(request.content).hexdigest(), "candidate_count": 2, "head_count": 2,
        "provider_request_id": "request-abc_123", "error_code": "invalid_request_error", "field": "questions",
    }
    assert caught.value.status_code == 400
    assert request.http_attempts == client.post.call_count == 1
    assert "PRIVATE" not in str(caught.value) + json.dumps(caught.value.metadata)
    model.time.sleep.assert_not_called()


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": {"code": "PRIVATE_CODE", "param": "PRIVATE_FIELD"}}),
    httpx.Response(400, json={"error": {"code": ["invalid_request_error"], "param": {"model": "PRIVATE"}}}),
    httpx.Response(400, headers={"x-request-id": "https://private.invalid/key"}, text="PRIVATE_BODY"),
    httpx.Response(400, headers={"x-request-id": "x" * 97}, content=b"\xffPRIVATE_BODY"),
])
def test_untrusted_response_values_are_omitted(client, response):
    client.post.return_value = response
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://provider.invalid", "PRIVATE_KEY", body())
    diagnostic = caught.value.metadata["provider_diagnostic"]
    assert not {"error_code", "field", "provider_request_id"} & diagnostic.keys()
    assert "PRIVATE" not in json.dumps(caught.value.metadata)


def test_direct_diagnostic_whitelist_rejects_types_urls_prose_and_unknown_fields():
    error = model.ProviderError("request", diagnostic={
        "component": "PRIVATE_COMPONENT", "model": "https://private.invalid/key", "request_bytes": True,
        "candidate_count": -1, "head_count": 2**31, "request_sha256": "PRIVATE_HASH",
        "provider_request_id": "PRIVATE ID", "error_code": "PRIVATE_CODE", "field": "messages[0].content",
        "message": "PRIVATE_MESSAGE", "raw": "PRIVATE_RAW", "value": "PRIVATE_VALUE",
    })
    assert error.metadata == {"provider_error_kind": "request"}


@pytest.mark.parametrize(("failure", "kind", "attempts"), [
    (httpx.ReadTimeout("PRIVATE_TIMEOUT"), "timeout", 1),
    (httpx.ConnectError("PRIVATE_CONNECTION"), "connection", 1),
    (httpx.Response(503, text="PRIVATE_RESPONSE"), "unavailable", 3),
    (httpx.Response(200, text="PRIVATE_JSON"), "invalid_response", 1),
])
def test_diagnostics_preserve_transport_attempt_counts(client, failure, kind, attempts):
    request = body()
    if isinstance(failure, Exception):
        client.post.side_effect = failure
    else:
        client.post.return_value = failure
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://provider.invalid", "PRIVATE_KEY", request)
    assert caught.value.kind == kind
    assert request.http_attempts == client.post.call_count == attempts
    assert caught.value.metadata["provider_diagnostic"]["request_sha256"] == hashlib.sha256(request.content).hexdigest()
    assert "PRIVATE" not in str(caught.value) + json.dumps(caught.value.metadata)


@pytest.mark.parametrize(("schema", "component"), [(None, "text"), ("browser_outcome_review", "verification")])
def test_helper_and_review_components_are_identified_without_copying_messages(client, schema, component):
    request = SerializedBody({"model": "openai/gpt-4.1-mini", "messages": [{"content": "PRIVATE_CONTENT"}],
                              "response_format": {"json_schema": {"name": schema}}})
    client.post.return_value = httpx.Response(400)
    with pytest.raises(model.ProviderError) as caught:
        model.post_json("https://provider.invalid", "PRIVATE_KEY", request)
    assert caught.value.metadata["provider_diagnostic"]["component"] == component
    assert "PRIVATE" not in json.dumps(caught.value.metadata)
