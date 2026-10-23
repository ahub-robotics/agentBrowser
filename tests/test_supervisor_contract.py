"""Offline contracts validate provenance and deltas without claiming semantic truth."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from agentrun.supervisor_contract import (
    ContractError,
    goal_coverage,
    validate_contract,
    validate_progress_review,
    validate_response,
)

GOAL = "Find books. Inspect every result and return to the list."


def template(id="visit", *, quote="Inspect every result and return to the list.", scope="destination"):
    return {
        "id": id, "instruction": "Open the current observed book and inspect its details.",
        "success_criteria": ["The current book's title and description are visible."],
        "requirement_ids": ["inspect"], "goal_quote": quote, "verification_scope": scope,
    }


def contract():
    return {
        "version": 2,
        "requirements": [
            {"id": "search", "goal_quote": "Find books.", "description": "Display book results.",
             "status": "unknown", "evidence_refs": []},
            {"id": "inspect", "goal_quote": "Inspect every result and return to the list.",
             "description": "Inspect all book results and return after each.",
             "status": "unknown", "evidence_refs": []},
        ],
        "tasks": [{
            "id": "search-books", "instruction": "Search for books and display the results.",
            "success_criteria": ["Book search results are visible."], "requirement_ids": ["search"],
            "depends_on": [], "goal_quote": "Find books.", "verification_scope": "current",
        }],
        "loops": [{
            "id": "books", "goal_quote": "Inspect every result and return to the list.",
            "description": "Inspect all observed books across result pages.", "requirement_ids": ["inspect"],
            "depends_on": ["search-books"], "discovery": template("discover", scope="current"),
            "steps": [template(), template("return", scope="current")], "limit": None, "order": "observed",
        }],
        "execution_constraints": [],
    }


def review_context():
    plan = contract()
    return {
        "contract": plan, "requirements": deepcopy(plan["requirements"]),
        "loops": {"books": {"id": "books", "exhausted": False}},
        "evidence": [
            {"ref": "old-result", "kind": "page", "text": "Earlier result", "observed_at": 1},
            {"ref": "book-link", "kind": "control", "href": "https://example.test/books/one",
             "observed_at": 2, "observed_order": 1},
            {"ref": "book-details", "kind": "page", "text": "Book details", "observed_at": 4},
            {"ref": "listing", "kind": "page", "text": "All books displayed", "observed_at": 5},
        ],
        "sent_refs": ["old-result", "book-link", "book-details", "listing"],
        "last_result": {"delegation_id": "run:2", "task": template(), "started_observation": 2},
        "unresolved_attempts": [], "attempt_observations": {}, "next_evidence_offset": None,
        "available_actions": ["continue", "observe", "repair", "feedback", "stop"],
        "feedback_reasons": ["missing_user_information", "external_intervention"],
    }


def satisfied_review():
    return {
        "next_action": "continue",
        "subtask_review": {"delegation_id": "run:2", "status": "satisfied", "evidence_refs": ["book-details"]},
    }


def test_plan_has_separate_ordered_loop_templates_and_preserves_input():
    raw = contract()
    before = deepcopy(raw)
    result = validate_contract(raw, GOAL)
    assert raw == before and result is not raw
    assert [step["id"] for step in result["loops"][0]["steps"]] == ["visit", "return"]
    assert [step["verification_scope"] for step in result["loops"][0]["steps"]] == ["destination", "current"]
    assert result["loops"][0]["limit"] is None
    assert goal_coverage(GOAL, result["requirements"])["complete"]


@pytest.mark.parametrize("invalid", [
    "duplicate_requirement", "duplicate_task_loop", "unknown_requirement", "unknown_dependency", "cycle",
    "invented_quote", "omitted_goal", "unplanned_requirement", "duplicate_step", "foreign_loop_requirement",
    "completed_without_evidence", "completed_with_invented_evidence", "coerced_limit", "empty_criterion",
])
def test_invalid_plan_is_rejected_without_rewriting_input(invalid):
    raw = contract()
    if invalid == "duplicate_requirement":
        raw["requirements"].append(deepcopy(raw["requirements"][0]))
    elif invalid == "duplicate_task_loop":
        raw["loops"][0]["id"] = "search-books"
    elif invalid == "unknown_requirement":
        raw["tasks"][0]["requirement_ids"] = ["made-up"]
    elif invalid == "unknown_dependency":
        raw["tasks"][0]["depends_on"] = ["made-up"]
    elif invalid == "cycle":
        raw["tasks"][0]["depends_on"] = ["books"]
    elif invalid == "invented_quote":
        raw["loops"][0]["steps"][0]["goal_quote"] = "Send a message."
    elif invalid == "omitted_goal":
        raw["requirements"][1]["goal_quote"] = "Inspect every result"
    elif invalid == "unplanned_requirement":
        raw["tasks"] = []
        raw["loops"][0]["depends_on"] = []
    elif invalid == "duplicate_step":
        raw["loops"][0]["steps"][1]["id"] = "visit"
    elif invalid == "foreign_loop_requirement":
        raw["loops"][0]["steps"][0]["requirement_ids"] = ["search"]
    elif invalid.startswith("completed_"):
        raw["requirements"][0]["status"] = "satisfied"
        if invalid.endswith("invented_evidence"):
            raw["requirements"][0]["evidence_refs"] = ["invented"]
    elif invalid == "coerced_limit":
        raw["loops"][0]["limit"] = "14"
    elif invalid == "empty_criterion":
        raw["loops"][0]["steps"][0]["success_criteria"] = ["  "]
    before = deepcopy(raw)
    with pytest.raises((ContractError, ValidationError)):
        validate_contract(raw, GOAL)
    assert raw == before


def test_initial_satisfied_requirement_needs_supplied_observed_evidence():
    raw = contract()
    raw["requirements"][0].update(status="satisfied", evidence_refs=["results"])
    facts = [{"ref": "results", "kind": "page", "text": "Book results"}]
    assert validate_contract(raw, GOAL, evidence=facts)["requirements"][0]["status"] == "satisfied"
    with pytest.raises(ContractError, match="not supplied"):
        validate_contract(raw, GOAL, evidence=facts, sent_refs=[])


@pytest.mark.parametrize("raw", [
    {"approved": True, "problems": ["Missing return"]},
    {"approved": True, "replacement_contract": contract()},
    {"approved": False, "problems": []},
    {"approved": "true"},
])
def test_audit_cannot_simultaneously_approve_and_replace_or_coerce_bool(raw):
    with pytest.raises(ValidationError):
        validate_response(raw, "audit")


def test_rejected_audit_can_propose_replacement_without_approving_it():
    result = validate_response({
        "approved": False, "problems": ["The return step was missing."], "replacement_contract": contract(),
    }, "audit")
    assert result["approved"] is False
    assert result["replacement_contract"]["version"] == 2
    assert result["phase"] == "audit" and result["contract_version"] == 2


@pytest.mark.parametrize("raw", [
    {"next_action": "finish"},
    {"next_action": "continue", "requirements": contract()["requirements"]},
    {"next_action": "continue", "task": template()},
    {"next_action": "continue", "repair": {"instruction": "Read the page", "success_criteria": ["Details visible"]}},
    {"next_action": "repair"},
    {"next_action": "feedback"},
    {"next_action": "stop"},
    {"next_action": "continue", "stop_reason": "Done"},
])
def test_review_has_no_finish_new_task_or_whole_state_replacement(raw):
    with pytest.raises(ValidationError):
        validate_response(raw, "review")


def test_review_deltas_preserve_unknown_global_requirement_and_input_state():
    payload = review_context()
    raw = {**satisfied_review(), "discovered": [{"loop_id": "books", "item_refs": ["book-link"]}]}
    before_payload, before_raw = deepcopy(payload), deepcopy(raw)
    result = validate_progress_review(raw, payload)
    assert result["subtask_review"]["status"] == "satisfied"
    assert result["requirement_updates"] == []
    assert payload == before_payload and raw == before_raw
    assert payload["requirements"][1]["status"] == "unknown"


def test_discovery_exhaustion_accepts_positive_coverage_with_unprocessed_items():
    payload = review_context()
    payload["items"] = {"book-one": {"status": "pending", "loop_id": "books", "completed_steps": {}}}
    raw = {"next_action": "continue", "loop_reviews": [{
        "loop_id": "books", "exhausted": True, "evidence_refs": ["listing"],
    }]}
    before = deepcopy(payload)
    review = validate_progress_review(raw, payload)
    assert review["loop_reviews"][0]["exhausted"] is True
    assert review["requirement_updates"] == [] and review["subtask_review"] is None
    assert payload == before
    assert payload["items"]["book-one"]["status"] == "pending"


@pytest.mark.parametrize("invalid", [
    "unknown_requirement", "duplicate_update", "invented_ref", "old_subtask_ref", "missing_positive_ref",
    "wrong_delegation", "raw_url", "unknown_loop", "not_a_link", "truncated_link", "unsent_link",
    "unproved_exhaustion", "unknown_attempt", "unavailable_action", "exhausted_inspect",
])
def test_review_validation_is_atomic_for_every_delta(invalid):
    payload = review_context()
    raw = {**satisfied_review(), "requirement_updates": [
        {"id": "search", "status": "satisfied", "evidence_refs": ["listing"]},
    ]}
    if invalid == "unknown_requirement":
        raw["requirement_updates"][0]["id"] = "new"
    elif invalid == "duplicate_update":
        raw["requirement_updates"].append(deepcopy(raw["requirement_updates"][0]))
    elif invalid == "invented_ref":
        raw["requirement_updates"][0]["evidence_refs"] = ["new"]
    elif invalid == "old_subtask_ref":
        raw["subtask_review"]["evidence_refs"] = ["old-result"]
    elif invalid == "missing_positive_ref":
        raw["subtask_review"]["evidence_refs"] = []
    elif invalid == "wrong_delegation":
        raw["subtask_review"]["delegation_id"] = "run:1"
    elif invalid in {"raw_url", "unknown_loop", "not_a_link", "truncated_link", "unsent_link"}:
        raw["discovered"] = [{"loop_id": "books", "item_refs": ["book-link"]}]
        if invalid == "raw_url":
            raw["discovered"][0]["item_refs"] = ["https://example.test/books/one"]
        elif invalid == "unknown_loop":
            raw["discovered"][0]["loop_id"] = "unknown"
        elif invalid == "not_a_link":
            raw["discovered"][0]["item_refs"] = ["listing"]
        elif invalid == "truncated_link":
            payload["evidence"][1]["href_truncated"] = True
        else:
            payload["sent_refs"].remove("book-link")
    elif invalid == "unproved_exhaustion":
        raw["loop_reviews"] = [{"loop_id": "books", "exhausted": True, "evidence_refs": []}]
    elif invalid == "unknown_attempt":
        raw["reconcile_attempts"] = [{"attempt_id": "unknown", "evidence_refs": ["listing"]}]
    elif invalid == "unavailable_action":
        payload["available_actions"] = ["observe"]
    elif invalid == "exhausted_inspect":
        payload["available_actions"].append("inspect")
        raw["next_action"] = "inspect"
    before = deepcopy(payload)
    with pytest.raises(ContractError):
        validate_progress_review(raw, payload)
    assert payload == before


def test_observe_allowed_with_uncertain_attempt_and_temporal_reconciliation_required():
    payload = review_context()
    payload["unresolved_attempts"] = [{"id": "send-1"}]
    payload["attempt_observations"] = {"send-1": 2}
    assert validate_progress_review({"next_action": "observe"}, payload)["next_action"] == "observe"
    raw = {"next_action": "continue", "reconcile_attempts": [{"attempt_id": "send-1", "evidence_refs": ["old-result"]}]}
    with pytest.raises(ContractError, match="New postcondition"):
        validate_progress_review(raw, payload)
    raw["reconcile_attempts"][0]["evidence_refs"] = ["book-details"]
    assert validate_progress_review(raw, payload)["reconcile_attempts"] == raw["reconcile_attempts"]


def test_review_reports_independent_destination_and_attempt_errors_together():
    payload = review_context()
    payload["active_work"] = {"kind": "item", "item_id": "book-one"}
    payload["items"] = {"book-one": {"started_observation": 2, "navigation_destinations": {}}}
    payload["last_result"]["item_id"] = "book-one"
    raw = {
        **satisfied_review(),
        "subtask_review": {
            "delegation_id": "run:2", "status": "unknown", "evidence_refs": [],
            "destination_ref": "book-details",
        },
        "reconcile_attempts": [{"attempt_id": "already-observed", "evidence_refs": ["book-details"]}],
    }

    with pytest.raises(ContractError) as caught:
        validate_progress_review(raw, payload)

    assert "Redirect destination was not observed" in str(caught.value)
    assert "unknown uncertain attempt" in str(caught.value)


def test_repair_cannot_change_scope_or_retry_an_uncertain_effect():
    payload = review_context()
    raw = {"next_action": "repair", "repair": {"instruction": "Observe the current result.",
                                                "success_criteria": ["Result details are visible."]}}
    assert validate_progress_review(raw, payload)["repair"] == raw["repair"]
    payload["unresolved_attempts"] = [{"id": "send-1"}]
    with pytest.raises(ContractError, match="uncertain"):
        validate_progress_review(raw, payload)
    raw["repair"]["item_ref"] = "book-link"
    with pytest.raises(ValidationError):
        validate_progress_review(raw, payload)


def test_feedback_requires_pending_requirement_and_user_only_information():
    payload = review_context()
    raw = {"next_action": "feedback", "feedback": {
        "question": "Which edition should I find?", "feedback_reason": "missing_user_information",
        "missing_information": "The required edition.", "blocked_requirement_ids": ["search"],
        "user_intervention": "The user must provide the edition, absent from the goal and observed facts.",
    }}
    assert validate_progress_review(raw, payload)["feedback"]["question"] == raw["feedback"]["question"]
    raw["requirement_updates"] = [{"id": "search", "status": "satisfied", "evidence_refs": ["listing"]}]
    with pytest.raises(ContractError, match="pending"):
        validate_progress_review(raw, payload)
    raw.pop("requirement_updates")
    raw["feedback"]["feedback_reason"] = "provider_error"
    with pytest.raises(ValidationError):
        validate_progress_review(raw, payload)


def redirect_context():
    payload = review_context()
    destination = "https://example.test/books/canonical-one"
    payload["evidence"].append({
        "ref": "redirect", "kind": "page", "url": destination,
        "observed_url": destination, "observed_at": 3,
    })
    payload["sent_refs"].append("redirect")
    payload["items"] = {"book-one": {
        "item_id": "book-one", "url": "https://example.test/books/one", "started_observation": 2,
        "navigation_destinations": {"redirect": destination},
    }}
    payload["active_work"] = {"kind": "item", "item_id": "book-one", "template_id": "visit", "loop_id": "books"}
    payload["last_result"]["item_id"] = "book-one"
    return payload


def test_redirect_identity_uses_observed_destination_and_does_not_mutate_aliases():
    payload = redirect_context()
    raw = satisfied_review()
    raw["subtask_review"]["destination_ref"] = "redirect"
    before = deepcopy(payload)
    result = validate_progress_review(raw, payload)
    assert result["subtask_review"]["destination_ref"] == "redirect"
    assert payload == before
    # Identity was observed before a repair of this step; its completion still
    # needs separate evidence newer than the repaired delegation's own start.
    payload["last_result"]["started_observation"] = 3
    assert validate_progress_review(raw, payload)["subtask_review"]["status"] == "satisfied"


@pytest.mark.parametrize("invalid", [
    "not_active_item", "unknown_item", "wrong_delegation_item", "before_item_start", "unsent",
    "unrecorded_navigation", "wrong_url", "wrong_observed_url", "not_page_fact", "not_http",
])
def test_redirect_cannot_invent_identity_or_borrow_another_items_observation(invalid):
    payload = redirect_context()
    raw = satisfied_review()
    raw["subtask_review"]["destination_ref"] = "redirect"
    if invalid == "not_active_item":
        payload["active_work"]["kind"] = "task"
    elif invalid == "unknown_item":
        payload["active_work"]["item_id"] = "unknown"
    elif invalid == "wrong_delegation_item":
        payload["last_result"]["item_id"] = "different"
    elif invalid == "before_item_start":
        payload["evidence"][-1]["observed_at"] = 2
    elif invalid == "unsent":
        payload["sent_refs"].remove("redirect")
    elif invalid == "unrecorded_navigation":
        payload["items"]["book-one"]["navigation_destinations"] = {}
    elif invalid == "wrong_url":
        payload["evidence"][-1]["url"] = "https://example.test/other-book"
    elif invalid == "wrong_observed_url":
        payload["evidence"][-1]["observed_url"] = "https://example.test/other-book"
    elif invalid == "not_page_fact":
        payload["evidence"][-1]["kind"] = "control"
    else:
        destination = "javascript:alert(1)"
        payload["items"]["book-one"]["navigation_destinations"]["redirect"] = destination
        payload["evidence"][-1].update(url=destination, observed_url=destination)
    before = deepcopy(payload)
    with pytest.raises(ContractError):
        validate_progress_review(raw, payload)
    assert payload == before


@pytest.mark.parametrize("raw", [
    {"contract_version": 1, "phase": "review", "next_action": "continue"},
    {"contract_version": True, "phase": "review", "next_action": "continue"},
    {"contract_version": 2, "phase": "audit", "next_action": "continue"},
    {"action": "delegate", "requirements": contract()["requirements"]},
])
def test_v1_or_wrong_phase_output_cannot_cross_v2_contract(raw):
    with pytest.raises((ContractError, ValidationError)):
        validate_response(raw, "review")
