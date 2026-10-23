"""Outcome review contracts and provider accounting; all providers are local fakes."""

import copy
import json

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableLambda

from AgentBrowser import model, verification
from AgentBrowser.candidates import SerializedBody, serialize_json
from AgentBrowser.evidence import execution_history
from AgentBrowser.verification import (
    VerificationError,
    VerificationReview,
    prepare_context,
    validate_assessment,
    verify,
)


@pytest.fixture
def payload():
    return {
        "goal": "Set the region to North and open the first result.",
        "evidence": [
            {"ref": "snapshot:node:3", "kind": "control", "label": "Region", "value": "North"},
            {"ref": "snapshot:page:title", "kind": "page", "title": "Results"},
        ],
        "history": [{"action": "Select region", "effects": [{"kind": "value", "after": "North"}]}],
        "previous_requirements": [],
        "selected_action": {"operation": "CLICK", "label": "First result"},
        "trigger": "before_action",
        "observation_digest": "snapshot",
    }


@pytest.fixture
def assessment():
    return {
        "requirements": [
            {"id": "region", "goal_quote": "Set the region to North", "description": "Region is North",
             "status": "satisfied", "evidence_refs": ["snapshot:node:3"]},
            {"id": "result", "goal_quote": "open the first result", "description": "First result is open",
             "status": "unsatisfied", "evidence_refs": ["snapshot:page:title"]},
        ],
        "action_assessment": "allow",
        "feedback": "Region is North; the results page is still open.",
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for key in (
        "VERIFICATION_API_KEY", "VERIFICATION_MODEL", "VERIFICATION_BASE_URL", "VERIFICATION_REASONING",
        "TEXT_MODEL_API_KEY", "TEXT_MODEL", "TEXT_MODEL_BASE_URL", "TEXT_MODEL_REASONING",
    ):
        monkeypatch.delenv(key, raising=False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A unit test attempted to contact a model provider")

    monkeypatch.setattr(model, "post_json", forbidden)


def test_forward_action_does_not_imply_goal_complete(payload, assessment):
    result = validate_assessment(assessment, prepare_context(payload))
    assert result["action_assessment"] == "allow"
    assert result["all_satisfied"] is False


def test_provider_receives_stale_proposal_as_unexecuted_feedback(monkeypatch, payload, assessment):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    payload["progress_feedback"] = {
        "reason": "stale_page", "action_was_executed": False,
        "proposal": {"operation": "CLICK", "label": "Item A", "context": {"heading": "Results"}},
        "feedback": "The proposal was discarded before input after the layout changed.",
    }

    def post(_url, _key, body):
        body.http_attempts += 1
        sent = json.loads(body["messages"][1]["content"])
        assert sent["progress_feedback"] == payload["progress_feedback"]
        assert sent["history"] == payload["history"]
        assert not any(fact.get("executed") for fact in sent["evidence"])
        return {"choices": [{"message": {"content": json.dumps(assessment)}}]}

    monkeypatch.setattr(model, "post_json", post)
    result, _ = verify(payload)
    assert result["all_satisfied"] is False


def test_confirmation_can_fulfill_pending_commit_without_claiming_its_future_effect(payload):
    payload["goal"] = "Select North and confirm the panel."
    payload["selected_action"] = {"operation": "CLICK", "label": "Confirm", "context": {"heading": "Region"}}
    answer = {
        "requirements": [{"id": "selection-and-confirmation", "goal_quote": "Select North and confirm the panel",
                          "description": "North is selected and confirmed", "status": "unknown",
                          "evidence_refs": ["snapshot:node:3"]}],
        "action_assessment": "allow", "feedback": "North is selected; the confirmation has not yet executed.",
    }
    result = validate_assessment(answer, prepare_context(payload))
    assert result["action_assessment"] == "allow"
    assert result["requirements"][0]["status"] == "unknown"
    assert result["all_satisfied"] is False


def test_conflicting_threshold_review_receives_exact_value_and_dependency_rules(monkeypatch):
    """Verify policy transport and rejection preservation, not simulated LLM intelligence."""
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    context = {
        "goal": "Set the maximum total to 800 and then open the first result.",
        "evidence": [{"ref": "current:limit", "kind": "control", "label": "Maximum total", "value": "500"}],
        "selected_action": {"operation": "CLICK", "label": "Submit"},
    }
    answer = {
        "requirements": [{"id": "limit", "goal_quote": "Set the maximum total to 800",
                          "description": "Requested maximum total is configured", "status": "unsatisfied",
                          "evidence_refs": ["current:limit"]}],
        "action_assessment": "reconsider", "feedback": "The observed configured maximum is 500, not 800.",
    }

    def post(_url, _key, body):
        body.http_attempts += 1
        instructions = body["messages"][0]["content"]
        assert "Do not substitute a stricter or looser threshold" in instructions
        assert "dependent later-stage action" in instructions
        assert "currently wrong control to edit it" in instructions
        sent = json.loads(body["messages"][1]["content"])
        assert sent["goal"] == context["goal"]
        assert sent["evidence"][0]["value"] == "500"
        return {"choices": [{"message": {"content": json.dumps(answer)}}]}

    monkeypatch.setattr(model, "post_json", post)
    result, _metadata = verify(context)
    assert result["action_assessment"] == "reconsider"
    assert result["all_satisfied"] is False


def test_done_requires_every_requirement_with_observed_evidence(payload, assessment):
    payload["selected_action"] = {"operation": "DONE"}
    result = validate_assessment(assessment, prepare_context(payload))
    assert result["action_assessment"] == "reconsider"
    assert result["all_satisfied"] is False
    assessment["requirements"][1]["status"] = "satisfied"
    result = validate_assessment(assessment, prepare_context(payload))
    assert result["all_satisfied"] is True
    assert result["action_assessment"] == "allow"


def test_unknown_remains_explicit(payload, assessment):
    assessment["requirements"][0].update(status="unknown", evidence_refs=[])
    assessment["action_assessment"] = "unknown"
    result = validate_assessment(assessment, prepare_context(payload))
    assert result["requirements"][0]["status"] == "unknown"
    assert result["all_satisfied"] is False


def test_equivalent_duplicate_citations_are_canonicalized_after_validation(payload, assessment):
    assessment["requirements"][0]["evidence_refs"] = ["snapshot:node:3", "snapshot:node:3"]
    original = copy.deepcopy(assessment)
    result = validate_assessment(assessment, prepare_context(payload))
    assert result["requirements"][0]["evidence_refs"] == ["snapshot:node:3"]
    assert result["requirements"][0]["status"] == "satisfied"
    assert assessment == original


def test_duplicate_invalid_reference_is_still_rejected(payload, assessment):
    assessment["requirements"][0]["evidence_refs"] = ["snapshot:node:3", "invented", "invented"]
    with pytest.raises(VerificationError, match="unavailable evidence"):
        validate_assessment(assessment, prepare_context(payload))


def test_duplicate_citations_do_not_bypass_raw_response_list_budget(payload, assessment):
    assessment["requirements"][0]["evidence_refs"] = ["snapshot:node:3"] * 17
    with pytest.raises(VerificationError, match="evidence_refs must be a bounded list"):
        validate_assessment(assessment, prepare_context(payload))


@pytest.mark.parametrize("change", [
    lambda answer: answer.update(selector="button"),
    lambda answer: answer.update(requirements=[]),
    lambda answer: answer.update(action_assessment="CLICK"),
    lambda answer: answer.update(feedback="x" * 1601),
    lambda answer: answer["requirements"][0].update(status="complete"),
    lambda answer: answer["requirements"][0].update(evidence_refs=[]),
    lambda answer: answer["requirements"][0].update(evidence_refs=["invented"]),
    lambda answer: answer["requirements"][0].update(goal_quote="Buy a ticket"),
    lambda answer: answer["requirements"][0].update(action="click"),
    lambda answer: answer["requirements"][0].update(id="button:nth-child(1)"),
    lambda answer: answer["requirements"].append(copy.deepcopy(answer["requirements"][0])),
])
def test_invalid_or_ungrounded_reviews_are_rejected(payload, assessment, change):
    change(assessment)
    with pytest.raises(VerificationError) as caught:
        validate_assessment(assessment, prepare_context(payload))
    assert caught.value.reason == "invalid_response"


def test_quotes_normalize_case_unicode_and_whitespace(payload, assessment):
    payload["goal"] = "Set the region to NORTH\n and open the first result."
    assessment["requirements"][0]["goal_quote"] = "set the region to ＮＯＲＴＨ"
    assert validate_assessment(assessment, prepare_context(payload))["requirements"][0]["status"] == "satisfied"


@pytest.mark.parametrize("change", [
    lambda answer: answer["requirements"].pop(),
    lambda answer: answer["requirements"][1].update(id="replacement"),
    lambda answer: answer["requirements"][1].update(goal_quote="first result"),
])
def test_previous_obligations_cannot_disappear_or_change_quote(payload, assessment, change):
    payload["previous_requirements"] = copy.deepcopy(assessment["requirements"])
    change(assessment)
    with pytest.raises(VerificationError, match="dropped or changed"):
        validate_assessment(assessment, prepare_context(payload))


def test_old_proof_cannot_support_satisfied_without_supplied_fact(payload, assessment):
    payload["previous_requirements"] = copy.deepcopy(assessment["requirements"])
    payload["evidence"].pop(0)
    with pytest.raises(VerificationError, match="unavailable evidence"):
        validate_assessment(assessment, prepare_context(payload))
    assessment["requirements"][0].update(status="unknown", evidence_refs=[])
    assert validate_assessment(assessment, prepare_context(payload))["all_satisfied"] is False


def test_history_and_current_evidence_are_distinguished(payload, assessment):
    payload["previous_requirements"] = assessment["requirements"]
    payload["evidence"][0].update(historical=True, observed_after_step=1)
    context = prepare_context(payload)
    old_fact = next(item for item in context["evidence"] if item["ref"] == "snapshot:node:3")
    assert old_fact["historical"] is True
    assert old_fact["observed_after_step"] == 1
    assert validate_assessment(assessment, context)["requirements"][0]["status"] == "satisfied"


@pytest.mark.parametrize(("visible", "complete"), [(False, True), (True, True), (False, False)])
def test_unsupported_context_visibility_is_retained_without_forcing_a_requirement_status(
    payload, assessment, visible, complete,
):
    limitation = {
        "ref": "snapshot:observation", "kind": "observation", "readiness": {"status": "ready", "settled": True},
        "coverage": {"extraction_complete": False, "unsupported_context_details": {
            "complete": complete, "count": 1, "hosts": [{
                "kind": "iframe", "title": "Embedded content", "css_visible": visible, "in_viewport": visible,
                "reachable_sample": False, "rect": {"x": 0, "y": 0, "width": 300 if visible else 0,
                                                     "height": 200 if visible else 0},
            }],
        }},
    }
    payload["evidence"].append(limitation)
    assessment["requirements"][0].update(status="unknown", evidence_refs=["snapshot:observation"])
    context = prepare_context(payload)
    observed = next(item for item in context["evidence"] if item["ref"] == "snapshot:observation")
    assert observed == limitation
    result = validate_assessment(assessment, context)
    assert result["requirements"][0]["status"] == "unknown"
    assert result["all_satisfied"] is False


def test_compaction_keeps_goal_and_referenced_history_and_reports_missing(payload, assessment):
    payload["previous_requirements"] = copy.deepcopy(assessment["requirements"])
    payload["evidence"] = [
        {"ref": f"old:{index}", "historical": True, "text": "x" * 500} for index in range(200)
    ] + [
        {"ref": "snapshot:node:3", "historical": True, "value": "North"},
        {"ref": "snapshot:page:title", "kind": "page", "title": "Results"},
    ]
    original = copy.deepcopy(payload)
    context = prepare_context(payload)
    assert len(serialize_json(context)) <= verification.MAX_CONTEXT_BYTES
    assert context["goal"] == original["goal"]
    assert context["previous_requirements"] == original["previous_requirements"]
    assert context["context_truncated"] is True
    assert context["omitted_evidence_count"] == len(payload["evidence"]) - len(context["evidence"])
    assert {"snapshot:node:3", "snapshot:page:title"} <= {item["ref"] for item in context["evidence"]}
    assert payload == original


def test_dropped_fact_cannot_be_cited_after_compaction(payload, assessment):
    payload["evidence"] = [{"ref": f"fact:{index}", "value": "x" * 6000} for index in range(100)]
    context = prepare_context(payload)
    assessment["requirements"][0]["evidence_refs"] = ["fact:99"]
    with pytest.raises(VerificationError, match="unavailable evidence"):
        validate_assessment(assessment, context)


def test_large_context_removes_dangling_previous_proof_without_dropping_obligations(payload):
    payload["evidence"] = [{"ref": f"fact:{index}", "value": "x" * 2000} for index in range(80)]
    payload["previous_requirements"] = [
        {"id": f"requirement_{index}", "goal_quote": "Set the region to North",
         "description": "Requested region is selected", "status": "satisfied",
         "evidence_refs": [f"fact:{index * 4 + offset}" for offset in range(4)]}
        for index in range(20)
    ]
    original = copy.deepcopy(payload)
    context = prepare_context(payload)
    refs = {fact["ref"] for fact in context["evidence"]}
    assert 0 < len(refs) < 80
    assert len(serialize_json(context)) <= verification.MAX_CONTEXT_BYTES
    assert len(context["previous_requirements"]) == 20
    assert [item["id"] for item in context["previous_requirements"]] == [
        item["id"] for item in original["previous_requirements"]
    ]
    assert all(set(item["evidence_refs"]) <= refs for item in context["previous_requirements"])
    assert any(item["status"] == "unknown" and item["evidence_refs"] == []
               for item in context["previous_requirements"])
    assert context["omitted_previous_reference_count"] == 80 - len(refs)
    assert context["context_truncated"] is True
    assert payload == original
    # Copying the supplied requirements is schema-valid even after compaction.
    result = validate_assessment({
        "requirements": context["previous_requirements"], "action_assessment": "unknown",
        "feedback": "Some earlier proof is unavailable in this bounded context.",
    }, context)
    assert result["all_satisfied"] is False


def test_missing_prior_proof_is_removed_before_packing_and_does_not_change_empty_status(payload):
    payload["previous_requirements"] = [
        {"id": "old", "goal_quote": "Set the region to North", "description": "Requested region",
         "status": "satisfied", "evidence_refs": ["obsolete:node:3"]},
        {"id": "pending", "goal_quote": "open the first result", "description": "Requested result",
         "status": "unsatisfied", "evidence_refs": []},
    ]
    context = prepare_context(payload)
    assert context["previous_requirements"][0]["status"] == "unknown"
    assert context["previous_requirements"][0]["evidence_refs"] == []
    assert context["previous_requirements"][1]["status"] == "unsatisfied"
    assert context["omitted_previous_reference_count"] == 1
    assert context["omitted_evidence_count"] == 0


def test_compaction_prioritizes_selected_navigation_source_and_its_url(payload):
    payload["evidence"] = [{"ref": f"current:{index}", "kind": "control", "value": "x" * 2000}
                           for index in range(80)] + [
        {"ref": "source:selected", "kind": "control", "historical": True, "navigation_source": True,
         "selected_control": True, "label": "Observed item", "href": "https://example.test/item/1"},
        {"ref": "source:url", "kind": "page", "historical": True, "navigation_source": True,
         "url": "https://example.test/list"},
    ]
    context = prepare_context(payload)
    refs = {fact["ref"] for fact in context["evidence"]}
    assert {"source:selected", "source:url"} <= refs
    assert len(context["evidence"]) < len(payload["evidence"])
    assert len(serialize_json(context)) <= verification.MAX_CONTEXT_BYTES
    assert all(fact["historical"] for fact in context["evidence"] if fact["ref"].startswith("source:"))


def test_history_is_bounded_and_effects_are_retained(payload):
    payload["history"] = [{"step": index, "effects": [{"before": "", "after": "North"}]} for index in range(20)]
    context = prepare_context(payload)
    assert [item["step"] for item in context["history"]] == list(range(10, 20))
    assert context["history"][-1]["effects"][0]["after"] == "North"
    assert context["context_truncated"] is True


def test_provider_retains_an_executed_confirmation_older_than_recent_effects(monkeypatch, payload, assessment):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    history = [{"step": step, "operation": "CLICK", "kind": "click", "action": "Inspect",
                "context": {"id": "options", "kind": "region", "heading": "Options"},
                "effects": [{"kind": "no_observed_change"}]} for step in range(1, 21)]
    history[3].update(action="Confirm", context={"id": "region", "kind": "form", "heading": "Region"})
    payload.update(history=history[-10:], execution_history=execution_history(history))

    def post(_url, _key, body):
        body.http_attempts += 1
        sent = json.loads(body["messages"][1]["content"])
        assert all(item["step"] > 10 for item in sent["history"])
        assert len(sent["execution_history"]) == 20
        assert sent["execution_history"][3] == {
            "step": 4, "operation": "CLICK", "kind": "click", "action": "Confirm",
            "context": {"id": "region", "kind": "form", "heading": "Region"},
        }
        assert sent["omitted_execution_count"] == 0
        assert not any("effects" in item or "ref" in item for item in sent["execution_history"])
        assert "neither log is a substitute for an observed postcondition" in body["messages"][0]["content"]
        return {"choices": [{"message": {"content": json.dumps(assessment)}}]}

    monkeypatch.setattr(model, "post_json", post)
    result, _ = verify(payload)
    assert result["all_satisfied"] is False


def test_large_review_preserves_compact_executions_and_reports_final_budget_omissions(payload, monkeypatch):
    history = [{"step": step, "kind": "click", "action": "Confirm region"} for step in range(1, 66)]
    payload["execution_history"] = execution_history(history)
    payload["evidence"] = [{"ref": f"fact:{index}", "value": "x" * 2000} for index in range(80)]
    context = prepare_context(payload)
    assert len(context["execution_history"]) == 60 and context["omitted_execution_count"] == 5
    assert len(context["evidence"]) < 80
    assert len(serialize_json(context)) <= verification.MAX_CONTEXT_BYTES
    monkeypatch.setattr(verification, "MAX_CONTEXT_BYTES", 1800)
    tiny = prepare_context(payload)
    assert 0 < len(tiny["execution_history"]) < 60
    assert tiny["omitted_execution_count"] == 65 - len(tiny["execution_history"])
    assert tiny["context_truncated"] is True
    assert len(serialize_json(tiny)) <= 1800


@pytest.mark.parametrize("change", [
    lambda context: context.update(goal=""),
    lambda context: context.update(goal="x" * 12001),
    lambda context: context.update(evidence=[{"label": "No reference"}]),
    lambda context: context["evidence"].append(copy.deepcopy(context["evidence"][0])),
    lambda context: context.update(history="an action"),
    lambda context: context.update(execution_history="an action"),
    lambda context: context.update(execution_history=["not a record"]),
    lambda context: context.update(execution_history=[{"earlier_executions_omitted": -1}]),
    lambda context: context["evidence"][0].update(value=float("nan")),
    lambda context: context.update(selected_action="CLICK"),
])
def test_bad_context_fails_before_provider(payload, change):
    change(payload)
    with pytest.raises(VerificationError) as caught:
        verify(payload)
    assert caught.value.reason == "invalid_input"
    assert caught.value.metadata["http_attempts"] == 0


def test_missing_configuration_does_not_claim_success(payload):
    with pytest.raises(VerificationError) as caught:
        verify(payload)
    assert caught.value.reason == "unconfigured"
    assert caught.value.metadata["http_attempts"] == 0


@pytest.mark.parametrize("separate_provider", [False, True])
def test_provider_config_and_transport_count_are_independent(payload, assessment, monkeypatch, separate_provider):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "text-secret")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://text.example/v1")
    monkeypatch.setenv("TEXT_MODEL", "writer")
    if separate_provider:
        monkeypatch.setenv("VERIFICATION_API_KEY", "review-secret")
        monkeypatch.setenv("VERIFICATION_BASE_URL", "https://review.example/v1/")
        monkeypatch.setenv("VERIFICATION_MODEL", "reviewer")
        monkeypatch.setenv("VERIFICATION_REASONING", "none")
    requests = []

    def post(url, key, body):
        requests.append((url, key, body))
        assert isinstance(body, SerializedBody)
        body.http_attempts += 2
        return {"choices": [{"message": {"content": json.dumps(assessment)}}], "usage": {"total_tokens": 100}}

    monkeypatch.setattr(model, "post_json", post)
    result, meta = verify(payload)
    assert result["requirements"] == assessment["requirements"]
    url, key, body = requests[0]
    assert url == ("https://review.example/v1/chat/completions" if separate_provider
                   else "https://text.example/v1/chat/completions")
    assert key == ("review-secret" if separate_provider else "text-secret")
    assert body["model"] == ("reviewer" if separate_provider else "writer")
    assert body["max_tokens"] == verification.MAX_OUTPUT_TOKENS
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert meta["http_attempts"] == 2
    assert meta["request_bytes"] == len(body.content)
    assert meta["usage"] == {"total_tokens": 100}
    assert len(body.content) <= verification.MAX_REQUEST_BYTES
    if separate_provider:
        assert body["reasoning"] == {"enabled": False}


def test_schema_forbids_the_extra_selected_action_field_seen_in_live_response():
    schema = verification.REVIEW_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == {
        "requirements", "action_assessment", "feedback",
    }
    item = schema["properties"]["requirements"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"]) == {
        "id", "goal_quote", "description", "status", "evidence_refs",
    }
    assert item["properties"]["status"]["enum"] == ["satisfied", "unsatisfied", "unknown"]
    assert schema["properties"]["action_assessment"]["enum"] == ["allow", "reconsider", "unknown"]


def test_provider_schema_bounds_match_local_review_limits(payload):
    schema = verification._review_schema(prepare_context(payload))
    requirements = schema["properties"]["requirements"]
    fields = requirements["items"]["properties"]
    assert requirements["minItems"] == 1
    assert requirements["maxItems"] == verification.MAX_REQUIREMENTS
    assert fields["evidence_refs"]["minItems"] == 0
    assert fields["evidence_refs"]["maxItems"] == 16
    assert fields["id"]["maxLength"] == 64
    assert fields["id"]["pattern"] == "^[A-Za-z0-9_-]+$"
    assert fields["goal_quote"]["maxLength"] == 2400
    assert fields["description"]["maxLength"] == 480
    assert schema["properties"]["feedback"]["maxLength"] == 1600


def test_review_with_31_citations_still_fails_if_provider_ignores_schema(payload, assessment):
    payload["evidence"] = [{"ref": f"observed:{index}", "kind": "control"} for index in range(31)]
    assessment["requirements"][0]["evidence_refs"] = [item["ref"] for item in payload["evidence"]]
    with pytest.raises(VerificationError, match="evidence_refs must be a bounded list"):
        validate_assessment(assessment, prepare_context(payload))


@pytest.mark.parametrize("many_facts", [False, True])
def test_sent_schema_citations_are_exactly_the_refs_retained_in_sent_context(
    payload, assessment, monkeypatch, many_facts,
):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    if many_facts:
        payload["evidence"] += [{"ref": f"old:{index}", "text": "x" * 1000} for index in range(170)]

    def post(_url, _key, body):
        body.http_attempts += 1
        context = json.loads(body["messages"][1]["content"])
        schema = body["response_format"]["json_schema"]["schema"]
        refs = schema["properties"]["requirements"]["items"]["properties"]["evidence_refs"]
        assert refs["items"]["enum"] == [item["ref"] for item in context["evidence"]]
        assert "snapshot" not in refs["items"]["enum"]
        if many_facts:
            assert context["omitted_evidence_count"] > 0
            assert "old:169" not in refs["items"]["enum"]
        return {"choices": [{"message": {"content": json.dumps(assessment)}}]}

    monkeypatch.setattr(model, "post_json", post)
    assert verify(payload)[1]["http_attempts"] == 1
    static_refs = verification.REVIEW_SCHEMA["properties"]["requirements"]["items"]["properties"]["evidence_refs"]
    assert "enum" not in static_refs["items"]


def test_exact_request_packing_accounts_for_escaped_ledger_and_dynamic_reference_schema(
    payload, assessment, monkeypatch,
):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    payload["previous_requirements"] = copy.deepcopy(assessment["requirements"])
    payload["execution_history"] = execution_history([
        {"step": step, "kind": "fill", "operation": "TYPE_TEXT", "action": "Search",
         "text": 'A "quoted" search\\value\n' * 5,
         "context": {"id": "form", "kind": "form", "heading": "Search"}}
        for step in range(1, 61)
    ])
    payload["evidence"] += [
        {"ref": f"observation:{index}:" + "r" * 150, "kind": "control",
         "value": 'A "quoted" value\\path\n' * 15}
        for index in range(160)
    ]
    original = copy.deepcopy(payload)
    built, submitted = [], []

    def capture(data):
        body = SerializedBody(data)
        built.append(body)
        return body

    def post(_url, _key, body):
        submitted.append(body)
        body.http_attempts += 1
        sent = json.loads(body["messages"][1]["content"])
        assert len(body.content) <= 64 * 1024
        assert len(serialize_json(sent)) <= 48 * 1024
        assert sent["goal"] == payload["goal"]
        assert [(item["id"], item["goal_quote"]) for item in sent["previous_requirements"]] == [
            (item["id"], item["goal_quote"]) for item in payload["previous_requirements"]
        ]
        refs = [item["ref"] for item in sent["evidence"]]
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["requirements"]["items"]["properties"]["evidence_refs"]["items"]["enum"] == refs
        assert all(set(item["evidence_refs"]) <= set(refs) for item in sent["previous_requirements"])
        assert sent["omitted_evidence_count"] == len(payload["evidence"]) - len(refs)
        answer = {"requirements": sent["previous_requirements"], "action_assessment": "allow",
                  "feedback": "The requested result remains pending."}
        return {"choices": [{"message": {"content": json.dumps(answer)}}]}

    monkeypatch.setattr(verification, "SerializedBody", capture)
    monkeypatch.setattr(model, "post_json", post)
    _result, metadata = verify(payload)
    assert len(built) >= 2 and len(submitted) == 1
    assert len(built[0].content) > verification.MAX_REQUEST_BYTES
    assert len(serialize_json(json.loads(built[0]["messages"][1]["content"]))) <= verification.MAX_CONTEXT_BYTES
    assert submitted[0] is built[-1]
    sent = json.loads(submitted[0]["messages"][1]["content"])
    assert metadata["request_bytes"] == len(submitted[0].content)
    assert metadata["context_bytes"] == len(serialize_json(sent))
    assert metadata["omitted_evidence_count"] == sent["omitted_evidence_count"]
    assert metadata["omitted_execution_count"] == sent["omitted_execution_count"]
    assert metadata["http_attempts"] == 1
    assert payload == original


def test_impossible_exact_request_budget_fails_before_http_with_last_body_metadata(payload, monkeypatch):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    monkeypatch.setattr(verification, "MAX_REQUEST_BYTES", 1024)
    built = []

    def capture(data):
        body = SerializedBody(data)
        built.append(body)
        return body

    monkeypatch.setattr(verification, "SerializedBody", capture)
    with pytest.raises(VerificationError) as caught:
        verify(payload)
    assert caught.value.reason == "budget_insufficient"
    assert caught.value.metadata["http_attempts"] == 0
    assert caught.value.metadata["request_bytes"] == len(built[-1].content)
    assert caught.value.metadata["context_bytes"] == len(
        serialize_json(json.loads(built[-1]["messages"][1]["content"])),
    )


def test_prepare_context_accepts_a_lower_byte_budget_without_raising_the_global_cap(payload):
    payload["evidence"] += [{"ref": f"fact:{index}", "value": "x" * 2000} for index in range(80)]
    small = prepare_context(payload, max_bytes=5000)
    assert len(serialize_json(small)) <= 5000
    assert small["omitted_evidence_count"] > 0
    large = prepare_context(payload, max_bytes=verification.MAX_CONTEXT_BYTES * 2)
    assert len(serialize_json(large)) <= verification.MAX_CONTEXT_BYTES


def test_schema_without_evidence_allows_only_empty_citations(payload, assessment, monkeypatch):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    payload["evidence"] = []
    for requirement in assessment["requirements"]:
        requirement.update(status="unknown", evidence_refs=[])
    assessment["action_assessment"] = "unknown"

    def post(_url, _key, body):
        body.http_attempts += 1
        schema = body["response_format"]["json_schema"]["schema"]
        refs = schema["properties"]["requirements"]["items"]["properties"]["evidence_refs"]
        assert refs["maxItems"] == 0
        assert "enum" not in refs["items"]
        return {"choices": [{"message": {"content": json.dumps(assessment)}}]}

    monkeypatch.setattr(model, "post_json", post)
    assert verify(payload)[0]["all_satisfied"] is False


@pytest.mark.parametrize(("base", "expected_routing"), [
    ("https://openrouter.ai/api/v1", {"require_parameters": True}),
    ("https://eu.openrouter.ai/api/v1", {"require_parameters": True}),
    ("https://openrouter.ai.example.test/v1", None),
])
def test_openrouter_schema_requires_compatible_endpoint_without_implicit_reasoning(
    payload, assessment, monkeypatch, base, expected_routing,
):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    monkeypatch.setenv("VERIFICATION_BASE_URL", base)
    monkeypatch.setenv("TEXT_MODEL_REASONING", "none")

    def post(_url, _key, body):
        body.http_attempts += 1
        assert body.get("provider") == expected_routing
        if expected_routing:
            assert "reasoning" not in body
        return {"choices": [{"message": {"content": json.dumps(assessment)}}]}

    monkeypatch.setattr(model, "post_json", post)
    assert verify(payload)[1]["http_attempts"] == 1


def test_unsupported_structured_output_does_not_downgrade_or_retry(payload, monkeypatch):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    calls = []

    def post(_url, _key, body):
        calls.append(body["response_format"]["type"])
        body.http_attempts += 1
        raise RuntimeError("Model provider returned HTTP 400; no action executed.")

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(VerificationError) as caught:
        verify(payload)
    assert caught.value.reason == "provider_error"
    assert caught.value.metadata["http_attempts"] == 1
    assert calls == ["json_schema"]


@pytest.mark.parametrize("content", [
    "not json", "[]", "{}", "null", "{" + '"requirements":[],"requirements":[]' + "}",
    "x" * (verification.MAX_RESPONSE_BYTES + 1), None,
])
def test_malformed_output_preserves_http_count(payload, content, monkeypatch):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")

    def post(_url, _key, body):
        body.http_attempts += 3
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(VerificationError) as caught:
        verify(payload)
    assert caught.value.reason == "invalid_response"
    assert caught.value.metadata["http_attempts"] == 3
    assert caught.value.metadata["validation_error"]
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(("change", "expected"), [
    (lambda answer: answer["requirements"][0].update(goal_quote="PRIVATE_PROVIDER_VALUE"), "goal_quote"),
    (lambda answer: answer["requirements"][0].update(evidence_refs=[]), "satisfied requires"),
    (lambda answer: answer["requirements"][0].update(evidence_refs=["PRIVATE_PROVIDER_VALUE"]), "unavailable evidence"),
    (lambda answer: answer["requirements"][0].update(extra="PRIVATE_PROVIDER_VALUE"), "missing or unexpected fields"),
])
def test_validation_diagnostic_names_field_without_echoing_provider_content(
    payload, assessment, monkeypatch, change, expected,
):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")
    change(assessment)

    def post(_url, _key, body):
        body.http_attempts += 1
        return {"choices": [{"finish_reason": "length", "message": {"content": json.dumps(assessment)}}]}

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(VerificationError) as caught:
        verify(payload)
    assert expected in caught.value.metadata["validation_error"]
    assert "requirements[0]" in caught.value.metadata["validation_error"]
    assert caught.value.metadata["finish_reason"] == "length"
    assert "PRIVATE_PROVIDER_VALUE" not in json.dumps(caught.value.metadata)


def test_provider_failure_preserves_attempt_count_without_leaking_secrets(payload, monkeypatch):
    monkeypatch.setenv("VERIFICATION_API_KEY", "secret")

    def post(_url, _key, body):
        body.http_attempts += 1
        raise RuntimeError("private provider request secret")

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(VerificationError) as caught:
        verify(payload)
    assert caught.value.reason == "provider_error"
    assert caught.value.metadata["http_attempts"] == 1
    assert "secret" not in str(caught.value)


def test_runnable_injected_provider_receives_full_input_without_internal_cache(payload, assessment):
    calls = []

    def reviewer(context):
        calls.append(context)
        return validate_assessment(assessment, prepare_context(context)), {"http_attempts": 0}

    runnable = VerificationReview(reviewer=reviewer)
    first = runnable.invoke(payload)
    second = runnable.invoke(payload)
    assert first == second
    assert calls == [payload, payload]
    assert calls[0] is payload


def test_runnable_propagates_callbacks_and_config(payload):
    class Trace(BaseCallbackHandler):
        def __init__(self):
            self.names = []

        def on_chain_start(self, _serialized, _inputs, **kwargs):
            self.names.append(kwargs.get("name"))

    trace = Trace()
    nested = RunnableLambda(lambda _payload, config: config["configurable"]["marker"], name="nested")
    runnable = VerificationReview(reviewer=lambda context: ({"marker": nested.invoke(context)}, {}))
    output = runnable.invoke(payload, config={"callbacks": [trace], "configurable": {"marker": "review"}})
    assert output == ({"marker": "review"}, {})
    assert {"VerificationReview", "nested"} <= set(trace.names)
