"""Offline allocation, byte budgets and native Jev pagination contracts."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from AgentBrowser import model
from AgentBrowser.candidates import (
    DEFAULT_LIMITS,
    CandidateBudgetExceeded,
    CandidateLimits,
    CandidateTraversal,
    CandidateTraversalExhausted,
    action_key,
    preview_actions,
    serialize_json,
)


def node(number, kind="click", *, label=None, region="root", options=0):
    result = {
        "node": number, "region": region, "role": "combobox" if kind == "select" else "button",
        "label": label or f"Control {number}", "operations": [kind], "value": "",
    }
    if kind == "select":
        result["options"] = [{"option": index + 10000, "label": "Same label", "value": "same-value"}
                             for index in range(options)]
    elif kind == "fill":
        result["role"] = "textbox"
        result["operations"].append("click")
    return result


def page(nodes, *, extraction_complete=True):
    return {
        "url": "https://example.test/", "title": "Candidates", "text": "Observed controls",
        "inventory": {
            "version": 1, "nodes": nodes,
            "globals": [{"id": "wait", "kind": "wait", "label": "Wait for page"}],
            "coverage": {
                "scope": "top_document_viewport", "extraction_complete": extraction_complete,
                "controls_inspected": len(nodes), "controls_total_known": extraction_complete,
                "options_inspected": sum(len(item.get("options", [])) for item in nodes),
                "options_total_known": extraction_complete, "unsupported_contexts": [],
            },
        },
    }


def answer(ids, selected):
    return {"choice": selected, "confidence": 1.0,
            "probabilities": {key: float(key == selected) for key in ids}}


def prepared(raw, limits=DEFAULT_LIMITS):
    traversal = CandidateTraversal(raw, limits)
    return traversal, model.prepare_candidates(raw, "Find the desired control", [], traversal=traversal)


def element_actions(selected):
    return [action for action in selected["actions"] if "node" in action]


def test_large_dropdown_cannot_hide_later_button_and_only_selected_actions_are_materialized():
    raw = page([node(1, "select", options=1000), node(2, label="Continue")])
    traversal, selected = prepared(raw)
    actions = element_actions(selected)

    assert {action["node"] for action in actions} == {1, 2}
    assert sum(action["kind"] == "select" for action in actions) == 12
    assert len(actions) == 13
    assert len(traversal.actions) == 1002
    # All option references share the original node dictionary, instead of
    # repeating its complete label/rectangle/context 1000 times in the inventory.
    assert traversal.actions[0].node is raw["inventory"]["nodes"][0]
    assert traversal.actions[999].node is traversal.actions[0].node
    assert selected["candidate_coverage"]["options_omitted"] == 988
    assert selected["candidate_coverage"]["more_available"]
    assert selected["request_bytes"] <= DEFAULT_LIMITS.max_request_bytes


def test_all_observed_options_are_reachable_without_repeats_even_with_identical_labels_and_values():
    raw = page([node(1, "select", options=1000), node(2)])
    traversal = CandidateTraversal(raw)
    seen = set()
    windows = 0
    while True:
        selected = model.prepare_candidates(raw, "Find an option", [], traversal=traversal)
        current = {action_key(action) for action in element_actions(selected)}
        assert not seen & current
        assert current
        assert len([action for action in selected["actions"] if action["kind"] == "select"]) <= 12
        assert selected["request_bytes"] == len(selected["_candidate_request"].content)
        assert selected["request_bytes"] <= DEFAULT_LIMITS.max_request_bytes
        seen.update(current)
        windows += 1
        traversal.commit_sent(selected["_candidate_window"])
        if not selected["candidate_coverage"]["more_available"]:
            break
        traversal.advance()
    assert windows == 84
    assert len(seen) == 1001
    assert selected["candidate_coverage"]["pending_known"] == 0
    assert selected["candidate_coverage"]["termination_reason"] == "inventory_exhausted"
    assert selected["candidate_coverage"]["traversal_complete"]
    assert not selected["candidate_coverage"]["budget_complete"]


def test_node_operation_and_global_action_limits_are_shared_across_regions():
    raw = page([
        node(1, "fill", region="form"), node(2, "fill", region="form"),
        node(3, "click", region="dialog"), node(4, "select", region="article", options=8),
    ])
    limits = replace(DEFAULT_LIMITS, max_nodes=3, max_actions=5, max_options_per_select=2)
    traversal, selected = prepared(raw, limits)
    actions = element_actions(selected)
    assert len(selected["actions"]) == 5
    assert len({action["node"] for action in actions}) == 3
    assert {action["region"] for action in actions} == {"form", "dialog", "article"}
    assert [action["kind"] for action in actions if action["node"] == 1] == ["fill", "click"]
    assert selected["actions"][-1]["kind"] == "wait"
    assert selected["candidate_coverage"]["regions_admitted"] == 3
    assert selected["candidate_coverage"]["operation_counts"]["fill"] == {"observed": 2, "admitted": 1}
    assert preview_actions(raw["inventory"], limits) == selected["actions"]
    assert traversal.presented == set()


def test_preparation_and_repeated_reads_do_not_rotate_or_mark_candidates_presented():
    raw = page([node(index) for index in range(4)])
    traversal, first = prepared(raw, replace(DEFAULT_LIMITS, max_nodes=1))
    repeated = model.prepare_candidates(raw, "Find the desired control", [], traversal=traversal)
    assert repeated["actions"] == first["actions"]
    assert repeated["selection_digest"] == first["selection_digest"]
    assert repeated["_candidate_window"] is first["_candidate_window"]
    assert not traversal.presented
    with pytest.raises(CandidateTraversalExhausted, match="no_progress"):
        traversal.advance()
    traversal.commit_sent(first["_candidate_window"])
    traversal.advance()
    second = model.prepare_candidates(raw, "Find the desired control", [], traversal=traversal)
    assert second["candidate_coverage"]["window_id"] == 2
    assert first["selection_digest"] != second["selection_digest"]
    with pytest.raises(ValueError, match="stale candidate window"):
        traversal.commit_sent(first["_candidate_window"])


def test_many_regions_of_one_family_cannot_take_the_entire_first_round():
    raw = page([
        *[node(index, "fill", region=f"form{index}") for index in range(20)],
        node(30, "click", region="dialog"), node(40, "select", region="article", options=20),
    ])
    _, selected = prepared(raw, replace(DEFAULT_LIMITS, max_nodes=3, max_actions=4))
    assert {action["kind"] for action in element_actions(selected)} == {"fill", "click", "select"}
    assert {action["node"] for action in element_actions(selected)} == {0, 30, 40}


def test_unicode_bytes_trim_only_presented_actions_and_later_windows_reach_every_control():
    raw = page([node(index, label="界" * 400 + str(index)) for index in range(12)])
    limits = replace(DEFAULT_LIMITS, max_request_bytes=12000)
    traversal, first = prepared(raw, limits)
    first_keys = {action_key(action) for action in element_actions(first)}
    assert 0 < len(first_keys) < 12
    assert "request_bytes" in first["candidate_coverage"]["trim_reasons"]
    assert not traversal.presented
    seen = set()
    selected = first
    while True:
        wire = selected["_candidate_request"].content
        assert wire == serialize_json(selected["_candidate_request"])
        assert len(wire) <= limits.max_request_bytes
        assert len(wire) > len(wire.decode("utf-8"))
        current = {action_key(action) for action in element_actions(selected)}
        assert not seen & current
        seen.update(current)
        traversal.commit_sent(selected["_candidate_window"])
        assert traversal.presented == seen
        if not selected["candidate_coverage"]["more_available"]:
            break
        traversal.advance()
        selected = model.prepare_candidates(raw, "Find the desired control", [], traversal=traversal)
    assert len(seen) == 12


def test_giant_candidate_does_not_hide_later_representable_button_or_advertise_an_impossible_window():
    raw = page([node(1, label="界" * 50000), node(2, label="Continue")])
    traversal, selected = prepared(raw)
    assert [action["node"] for action in element_actions(selected)] == [2]
    assert selected["request_bytes"] <= DEFAULT_LIMITS.max_request_bytes
    coverage = selected["candidate_coverage"]
    assert coverage["unrepresentable_actions"] == 1
    assert not coverage["more_available"]
    assert coverage["termination_reason"] == "budget_insufficient"
    assert not coverage["complete"]
    traversal.commit_sent(selected["_candidate_window"])
    with pytest.raises(CandidateTraversalExhausted, match="budget_insufficient"):
        traversal.advance()


def test_impossible_first_node_releases_node_quota_for_later_controls():
    raw = page([node(1, label="x" * 20000), node(2, label="Result")])
    _, selected = prepared(raw, replace(DEFAULT_LIMITS, max_nodes=1, max_request_bytes=8000))
    assert [action["node"] for action in element_actions(selected)] == [2]
    assert selected["candidate_coverage"]["termination_reason"] == "budget_insufficient"
    assert not selected["candidate_coverage"]["more_available"]


def test_later_impossible_candidate_does_not_advertise_more_even_if_first_window_fits():
    raw = page([node(1), node(2, label="x" * 20000)])
    _, selected = prepared(raw, replace(DEFAULT_LIMITS, max_nodes=1, max_request_bytes=8000))
    assert [action["node"] for action in element_actions(selected)] == [1]
    assert selected["candidate_coverage"]["termination_reason"] == "budget_insufficient"
    assert not selected["candidate_coverage"]["more_available"]


def test_global_reservation_leaving_no_node_capacity_fails_before_inference():
    with pytest.raises(CandidateBudgetExceeded, match="reserved global controls"):
        prepared(page([node(1)]), replace(DEFAULT_LIMITS, max_actions=1))


def test_partial_selector_declares_unknown_total_and_known_omissions():
    raw = page([node(1, "select", options=20)], extraction_complete=False)
    raw["inventory"]["nodes"][0]["options_complete"] = False
    _, selected = prepared(raw)
    selector = selected["candidate_coverage"]["selectors"][0]
    assert selector["observed"] == 20
    assert selector["admitted"] == 12
    assert selector["omitted"] == 8
    assert selector["total_known"] is False
    assert selector["total"] is None


def test_huge_goal_fails_before_any_provider_request(monkeypatch):
    post = Mock()
    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline")
    with pytest.raises(CandidateBudgetExceeded, match="minimum request"):
        model.choose(page([node(1)]), "界" * 50000, [])
    post.assert_not_called()


def test_incomplete_extraction_and_window_limit_are_distinct_from_inventory_exhaustion():
    _, incomplete = prepared(page([node(1)], extraction_complete=False))
    coverage = incomplete["candidate_coverage"]
    assert coverage["budget_complete"]
    assert not coverage["extraction_complete"]
    assert not coverage["complete"]
    assert coverage["actions_total"] is None
    assert coverage["termination_reason"] == "extraction_incomplete"
    _, limited = prepared(page([node(1), node(2)]), replace(DEFAULT_LIMITS, max_nodes=1, max_windows=1))
    assert limited["candidate_coverage"]["pending_known"] == 1
    assert limited["candidate_coverage"]["termination_reason"] == "window_limit"
    assert not limited["candidate_coverage"]["more_available"]


def test_more_candidates_uses_one_native_request_and_does_not_consume_other_target_heads(monkeypatch):
    _, selected = prepared(page([node(1, "fill"), node(2)]), replace(DEFAULT_LIMITS, max_nodes=1))
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {"model": "offline", "answers": {
            "operation": answer(body["questions"]["operation"]["criteria"], "MORE_CANDIDATES"),
            "click_target": {"choice": "invented"}, "type_text_target": {"choice": "also invented"},
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(selected, "Find the desired control", [])
    assert len(calls) == 1
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}
    assert decision["choice"] == "MORE_CANDIDATES"
    assert decision["target"] is None
    assert decision["target_probabilities"] == {}


def test_excluded_target_index_is_rejected(monkeypatch):
    _, selected = prepared(page([node(1), node(2)]), replace(DEFAULT_LIMITS, max_nodes=1))

    def post(_url, _key, body):
        return {"model": "offline", "answers": {
            "operation": answer(body["questions"]["operation"]["criteria"], "CLICK"),
            "click_target": answer(["1", "2"], "2"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(selected, "Find the desired control", [])


def test_transport_sends_exact_measured_bytes_with_cumulative_http_attempts_and_per_decision_counts(monkeypatch):
    _, selected = prepared(page([node(1)]))
    body = selected["_candidate_request"]
    result = {"model": "offline", "answers": {
        "operation": answer(body["questions"]["operation"]["criteria"], "CLICK"),
        "click_target": answer(["1"], "1"),
    }}
    retry = Mock(status_code=503)
    success = Mock(status_code=200, is_error=False, json=Mock(return_value=result))
    post = Mock(side_effect=[retry, success, success])
    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline")
    first = model.choose(selected, "Find the desired control", [])
    second = model.choose(selected, "Find the desired control", [])
    assert first["http_attempts"] == 2
    assert second["http_attempts"] == 1
    assert body.http_attempts == 3
    for call in post.call_args_list:
        assert call.kwargs["content"] is body.content
        assert len(call.kwargs["content"]) == selected["request_bytes"]
        assert call.kwargs["headers"]["Content-Type"] == "application/json"
        assert "json" not in call.kwargs


def test_prepared_request_cannot_silently_reuse_another_goal_or_history(monkeypatch):
    raw = page([node(1)])
    traversal, selected = prepared(raw)
    post = Mock()
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="no longer matches"):
        model.choose(selected, "A different goal", [])
    with pytest.raises(ValueError, match="context changed"):
        model.prepare_candidates(raw, "A different goal", [], traversal=traversal)
    post.assert_not_called()


@pytest.mark.parametrize("values", [
    {"max_nodes": 0}, {"max_actions": True}, {"max_request_bytes": -1},
    {"max_seconds": float("inf")}, {"max_seconds": float("nan")},
])
def test_invalid_limits_fail_before_extraction_or_inference(values):
    with pytest.raises(ValueError):
        CandidateLimits(**values)
