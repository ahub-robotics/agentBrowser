"""Scoped scroll choices and factual feedback contracts, entirely offline."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from AgentBrowser import model
from AgentBrowser.candidates import DEFAULT_LIMITS, CandidateTraversal, action_key, serialize_json
from AgentBrowser.evidence import compare_pages, evidence_view, execution_history, navigation_source, semantic_digest


def sample():
    return {
        "url": "https://example.test/search", "title": "Search", "text": "Options\nAny region",
        "page_key": [123, "https://example.test/search"],
        "readiness": {"state": "ready"},
        "inventory": {
            "nodes": [
                {"node": 1, "label": "Search", "role": "searchbox", "value": "", "operations": ["fill"],
                 "context": {"id": "panel", "kind": "dialog", "heading": "Region"}},
                {"node": 2, "label": "Any region", "role": "button", "pressed": "true", "operations": ["click"],
                 "context": {"id": "panel", "kind": "dialog", "heading": "Region"}},
            ],
            "globals": [
                {"id": "e3_scroll_up", "kind": "scroll", "node": 3, "direction": "up", "delta": -100,
                 "role": "region", "label": "Region options", "context": {"id": "panel", "heading": "Region"}},
                {"id": "e3_scroll_down", "kind": "scroll", "node": 3, "direction": "down", "delta": 100,
                 "role": "region", "label": "Region options", "context": {"id": "panel", "heading": "Region"}},
                {"id": "e4_scroll_down", "kind": "scroll", "node": 4, "direction": "down", "delta": 100,
                 "role": "region", "label": "Page", "context": {"id": "page", "heading": "Page"}},
                {"id": "wait", "kind": "wait", "label": "Wait"},
            ],
            "coverage": {"extraction_complete": True},
        },
    }


def test_native_submission_capability_reaches_actions_and_invalidates_evidence():
    before = sample()
    before["inventory"]["nodes"][1].update(expanded="false", submits_form=False)
    after = deepcopy(before)
    after["inventory"]["nodes"][1]["submits_form"] = True
    prepared = model.prepare_candidates(after, "Choose an option", [])
    selected = next(action for action in prepared["actions"] if action.get("node") == 2)
    assert selected["expanded"] == "false" and selected["submits_form"] is True
    assert semantic_digest(before) != semantic_digest(after)
    fact = next(item for item in evidence_view(after) if item.get("node") == 2)
    assert fact["submits_form"] is True
    assert any(change["kind"] == "submits_form" and change["after"] is True
               for change in compare_pages(before, after, selected))


def test_execution_history_is_bounded_explicit_and_omits_nonexecution_details():
    history = [{
        "step": step, "kind": "fill", "operation": "TYPE_TEXT", "action": "Search " + "x" * 300,
        "text": "北" * 700, "context": {"id": "form", "kind": "form", "heading": "h" * 200,
                                       "selector": "not passed"},
        "effects": [{"kind": "value", "after": "x" * 4000}], "probability": .9, "node": 123,
    } for step in range(1, 66)]
    original = deepcopy(history)
    compact = execution_history(history)
    assert len(compact) == 60
    assert compact[0]["step"] == 6 and compact[-1]["step"] == 65
    assert compact[0]["earlier_executions_omitted"] == 5
    assert len(compact[0]["action"]) == 240 and len(compact[0]["text"]) == 512
    assert compact[0]["truncated_fields"] == ["action", "text", "context.heading"]
    assert set(compact[0]["context"]) == {"id", "kind", "heading"}
    assert not ({"effects", "probability", "node", "ref", "status"} & compact[0].keys())
    assert history == original


def answer(ids, selected):
    return {"choice": selected, "confidence": 1,
            "probabilities": {key: int(key == selected) for key in ids}}


def test_scroll_regions_have_distinct_heads_and_only_chosen_head_is_consumed(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {"model": "offline", "answers": {
            "operation": answer(body["questions"]["operation"]["criteria"], "SCROLL_DOWN"),
            "scroll_down_target": answer(body["questions"]["scroll_down_target"]["criteria"], "3"),
            "scroll_up_target": {"choice": "malformed unused head"},
            "click_target": {"choice": "malformed unused head"},
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(sample(), "Find a region", [])
    assert len(calls) == 1
    assert set(calls[0]["questions"]) == {
        "operation", "type_text_target", "click_target", "scroll_up_target", "scroll_down_target",
    }
    assert calls[0]["questions"]["scroll_down_target"]["criteria"]["3"]["context"]["heading"] == "Region"
    assert decision["choice"] == "e3_scroll_down"
    assert decision["probabilities"] == {"e3_scroll_down": 1, "e4_scroll_down": 0}


def test_legacy_global_scroll_remains_compatible():
    action = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 100}
    elements, targets, controls = model.action_space([action])
    assert elements == [] and targets == {}
    assert controls == {"SCROLL_DOWN": action}


def test_scoped_scrolls_share_candidate_quotas_and_retain_direction_identity():
    page = sample()
    limits = replace(DEFAULT_LIMITS, max_nodes=2, max_actions=4)
    traversal = CandidateTraversal(page, limits)
    all_keys, windows = set(), 0
    while True:
        prepared = model.prepare_candidates(page, "Find a region", [], traversal=traversal)
        assert len(prepared["actions"]) <= 4
        assert len({action["node"] for action in prepared["actions"] if "node" in action}) <= 2
        keys = prepared["_candidate_window"].keys
        assert not keys & all_keys
        all_keys.update(keys)
        windows += 1
        assert prepared["request_bytes"] == len(serialize_json(prepared["_candidate_request"]))
        traversal.commit_sent(prepared["_candidate_window"])
        if not prepared["candidate_coverage"]["more_available"]:
            break
        traversal.advance()
    assert windows >= 2
    assert action_key(page["inventory"]["globals"][0]) in all_keys
    assert action_key(page["inventory"]["globals"][1]) in all_keys
    assert len(all_keys) == 5


@pytest.mark.parametrize("mutation", ["feedback", "readiness", "context", "effects"])
def test_prepared_request_rejects_changed_feedback_and_context_before_network(monkeypatch, mutation):
    page, history = sample(), [{"action": "Click", "effects": [{"kind": "no_observed_change"}]}]
    page["progress_feedback"] = {"reason": "Region requirement pending"}
    prepared = model.prepare_candidates(page, "Find a region", history)
    post = Mock()
    monkeypatch.setattr(model, "post_json", post)
    if mutation == "feedback":
        prepared["progress_feedback"] = {"reason": "Other pending requirement"}
    elif mutation == "readiness":
        prepared["readiness"] = {"state": "loading"}
    elif mutation == "context":
        prepared["actions"][0]["context"] = {"id": "other", "heading": "Price"}
    else:
        history[0]["effects"] = [{"kind": "navigated"}]
    with pytest.raises(ValueError, match="no longer matches"):
        model.choose(prepared, "Find a region", history)
    post.assert_not_called()


def test_current_facts_and_effects_do_not_claim_success_when_panel_closes():
    before, after = sample(), sample()
    after["inventory"]["nodes"] = []
    after["inventory"]["globals"] = [after["inventory"]["globals"][-1]]
    changes = compare_pages(before, after, {"kind": "click", "label": "Accept"})
    assert any(change["kind"] == "closed" and change["context"]["id"] == "panel" for change in changes)
    assert all("satisfied" not in change and "success" not in change for change in changes)
    assert all(fact["kind"] != "control" for fact in evidence_view(after))
    assert all("ref" not in change for change in changes if change["kind"] == "closed")


def test_control_changes_and_refs_are_current_while_cycles_ignore_geometry_and_time():
    before, after = sample(), sample()
    before_digest = semantic_digest(before)
    after["fingerprint"] = "new browser fingerprint"
    after["page_key"][0] += 5000
    after["inventory"]["nodes"][0]["rect"] = {"x": 400, "y": 600}
    assert semantic_digest(after) == before_digest
    assert evidence_view(after)[0]["ref"] != evidence_view(before)[0]["ref"]
    after["inventory"]["nodes"][0]["value"] = "Desired region"
    after["inventory"]["nodes"][1]["pressed"] = "false"
    assert semantic_digest(after) != before_digest
    effects = compare_pages(before, after, {"kind": "fill", "node": 1})
    assert [change["kind"] for change in effects] == ["value", "pressed"]
    refs = {fact["ref"] for fact in evidence_view(after)}
    assert all(change["ref"] in refs for change in effects)


def test_selected_option_alternatives_are_not_evidence_of_the_current_value():
    page = {"url": "https://example.test", "title": "", "text": "", "actions": [
        {"node": 4, "kind": "select", "id": "option", "label": "Region → Desired region",
         "value": "desired", "current_value": "Any region"},
    ]}
    fact = next(item for item in evidence_view(page) if item["kind"] == "control")
    assert fact["value"] == "Any region"
    assert fact["label"] == "Region"


def test_synthetic_click_cannot_overwrite_the_observed_editable_value():
    page = {"url": "https://example.test", "title": "", "text": "", "actions": [
        {"node": 10, "kind": "fill", "id": "field", "label": "Search", "value": ""},
        {"node": 10, "kind": "click", "id": "open_field", "label": "Open Search", "value": ""},
    ]}
    before = deepcopy(page)
    page["actions"][0]["value"] = "Book"
    assert semantic_digest(page) != semantic_digest(before)
    assert next(fact for fact in evidence_view(page) if fact["kind"] == "control")["value"] == "Book"
    assert compare_pages(before, page, page["actions"][0])[0]["after"] == "Book"


def test_field_helper_input_changes_with_panel_and_feedback():
    page = sample()
    action = page["inventory"]["nodes"][0]
    original = model.field_context("Find a region", action, page, [])
    page["progress_feedback"] = {"reason": "Typed query has not selected an option"}
    assert model.field_context("Find a region", action, page, []) != original
    other = deepcopy(action)
    other["context"]["heading"] = "Another panel"
    assert model.field_context("Find a region", other, sample(), []) != original


def test_text_helper_reports_actual_transport_attempts_and_exact_bytes(monkeypatch):
    def post(_url, _key, body):
        body.http_attempts += 2
        assert body.content == serialize_json(body)
        return {"choices": [{"message": {"content": '{"text":"Observed value"}'}}]}

    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "offline")
    text, metadata = model.field_text({"goal": "Enter the observed value"})
    assert text == "Observed value"
    assert metadata["http_attempts"] == 2
    assert metadata["request_bytes"] > 0


def test_navigation_source_retains_selected_observation_outside_default_evidence_window():
    page = sample()
    page["inventory"]["nodes"] = [
        {"node": number, "label": f"Item {number}", "role": "link", "operations": ["click"],
         "href": f"https://example.test/item/{number}", "href_truncated": number == 100,
         "context": {"id": "items", "kind": "region", "heading": "Items"}}
        for number in range(1, 101)
    ]
    page["inventory"]["globals"] = []
    source = navigation_source(page, {"node": 100, "kind": "click"}, 7)
    selected = source[0]
    assert len(source) <= 16
    assert selected["node"] == 100 and selected["selected_control"] is True
    assert selected["observed_order"] == 100
    assert selected["href"] == "https://example.test/item/100" and selected["href_truncated"] is True
    assert selected["ref"] not in {fact["ref"] for fact in evidence_view(page)}
    assert all(fact["historical"] and fact["navigation_source"] and fact["observed_before_step"] == 7
               for fact in source)
    assert any(fact.get("url") == page["url"] for fact in source)
    coverage = next(fact for fact in source if fact["kind"] == "coverage")
    assert coverage["controls_omitted"] > 0
    assert "not global result ranking" in coverage["order_scope"]


def test_identically_named_action_history_keeps_panel_context_and_invalidates_cached_inputs(monkeypatch):
    page = sample()
    history = [
        {"action": "Accept", "kind": "click", "context": {"id": "model", "kind": "dialog", "heading": "Model"}},
        {"action": "Accept", "kind": "click", "context": {"id": "price", "kind": "dialog", "heading": "Price"}},
    ]
    prepared = model.prepare_candidates(page, "Complete the requested selections", history)
    recent = prepared["_candidate_request"]["state"]["recent_actions"]
    assert [entry["action"] for entry in recent] == ["Accept", "Accept"]
    assert [entry["context"]["heading"] for entry in recent] == ["Model", "Price"]
    assert prepared["request_bytes"] == len(serialize_json(prepared["_candidate_request"]))
    helper_before = deepcopy(model.field_context("Complete the requested selections", page["inventory"]["nodes"][0],
                                                page, history))
    history[0]["context"]["heading"] = "Different panel"
    helper_after = model.field_context(
        "Complete the requested selections", page["inventory"]["nodes"][0], page, history,
    )
    assert helper_after != helper_before
    post = Mock()
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="no longer matches"):
        model.choose(prepared, "Complete the requested selections", history)
    post.assert_not_called()


def test_policy_history_prioritizes_postconditions_and_declares_every_effect_omission():
    effects = [
        {"kind": "control_observed", "node": number, "label": "Unrelated control " + "x" * 2000}
        for number in range(100, 140)
    ] + [
        {"kind": "position", "node": 17, "label": "Acted region", "before": 0, "after": 400},
        {"kind": "value", "node": 1, "ref": "scope:node:1", "before": "", "after": "v" * 2000},
        {"kind": "selected", "node": 2, "ref": "scope:node:2", "before": False, "after": True},
        {"kind": "checked", "node": 3, "ref": "scope:node:3", "before": False, "after": True},
        {"kind": "visible_text_changed", "ref": "scope:page:text", "before": "b" * 2000, "after": "a" * 2000},
        {"kind": "navigated", "ref": "scope:page:url", "before": "https://example.test/", "after": "https://example.test/result"},
        {"kind": "effects_truncated", "omitted": 7},
    ]
    history = [{
        "step": step, "action": "Acted region", "node": 17, "kind": "scroll", "operation": "SCROLL_DOWN",
        "text": None, "page_changed": True, "effects": deepcopy(effects),
    } for step in range(1, 11)]
    original = deepcopy(history)
    verification_history = execution_history(history)
    prepared = model.prepare_candidates(sample(), "Check the observed result", history)
    body = prepared["_candidate_request"]
    recent = body["state"]["recent_actions"]
    assert len(recent) == 10
    for entry in recent:
        assert [effect["kind"] for effect in entry["effects"]] == [
            "position", "value", "selected", "checked", "visible_text_changed", "navigated",
        ]
        assert entry["effects_omitted"] == 47  # 40 policy omissions plus 7 already omitted by extraction.
        value = entry["effects"][1]
        assert value["ref"] == "scope:node:1" and value["after"] == "v" * 512
        assert value["truncated_fields"] == ["after"]
        assert entry["effects"][4]["truncated_fields"] == ["before", "after"]
        assert entry["effects"][5]["after"] == "https://example.test/result"
    assert len(serialize_json(recent)) < len(serialize_json(history)) / 10
    assert len(body.content) == prepared["request_bytes"] <= DEFAULT_LIMITS.max_request_bytes
    assert history == original and execution_history(history) == verification_history


def test_policy_history_keeps_small_effects_exact_and_does_not_retain_nested_text_without_a_notice():
    history = [{"step": step, "kind": "click", "action": "Continue", "effects": []} for step in range(1, 12)]
    history[-1]["effects"] = [
        {"kind": "selected", "node": 1, "ref": "scope:node:1", "before": False, "after": True},
        {"kind": "opened", "context": {"heading": "h" * 700}, "observed_only": True},
    ]
    original = deepcopy(history)
    prepared = model.prepare_candidates(sample(), "Continue the current task", history)
    recent = prepared["_candidate_request"]["state"]["recent_actions"]
    assert len(recent) == 10 and recent[0]["earlier_executions_omitted"] == 1
    assert recent[-1]["effects"][0] == history[-1]["effects"][0]
    assert "effects_omitted" not in recent[-1]
    opened = recent[-1]["effects"][1]
    assert opened["context"]["heading"] == "h" * 512
    assert opened["truncated_fields"] == ["context.heading"]
    assert opened["observed_only"] is True
    assert history == original
