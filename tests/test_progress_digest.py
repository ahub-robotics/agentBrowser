"""Cycle detection ignores DOM churn without changing evidence or freshness."""

from copy import deepcopy

import pytest

from AgentBrowser.evidence import evidence_view, progress_digest, semantic_digest


def page():
    context = {"id": "r10", "kind": "dialog", "heading": "Options"}
    return {
        "url": "https://example.test/search", "title": "Search", "text": "Options\nAny\nConfirm",
        "fingerprint": "original", "page_key": [123, "https://example.test/search", 0, 200, 1000, 800],
        "scroll": {"y": 200, "height": 4000}, "w": 1000, "h": 800,
        "contexts": [context],
        "inventory": {
            "nodes": [
                {"node": 11, "region": "r10", "context": context, "role": "textbox", "label": "Query",
                 "value": "", "readonly": False, "operations": ["fill", "click"],
                 "rect": {"x": 10, "y": 50, "w": 100, "h": 20}},
                {"node": 12, "region": "r10", "context": context, "role": "combobox", "label": "Choice",
                 "current_value": "Any", "operations": ["select"], "options_complete": True,
                 "options": [{"option": 13, "label": "North", "value": "north"},
                             {"option": 14, "label": "South", "value": "south"}]},
                {"node": 15, "region": "r10", "context": context, "role": "button", "label": "Confirm",
                 "operations": ["click"], "pressed": "false", "submits_form": True},
                {"node": 16, "region": "r10", "context": context, "role": "button", "label": "Confirm",
                 "operations": ["click"], "pressed": "false", "submits_form": True},
            ],
            "globals": [
                {"node": 10, "id": "scroll_10_down", "kind": "scroll", "context": context,
                 "region": "r10", "role": "region", "label": "Options — scroll down", "direction": "down",
                 "position": 0, "scroll": {"y": 0, "height": 2000, "viewport": 400}},
            ],
        },
    }


def renumber(value):
    if isinstance(value, list):
        return [renumber(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: f"remounted-{item}" if key in {"node", "option", "id", "region"} else renumber(item)
            for key, item in value.items()}


def test_renumbered_panel_and_layout_changes_repeat_progress_but_not_evidence_identity():
    before = page()
    after = renumber(before)
    after.update(fingerprint="new", w=1200, h=900, guards={"new": "guard"})
    after["page_key"] = [456, before["url"], 0, 200, 1200, 900]
    after["scroll"]["height"] = 4500
    after["inventory"]["nodes"][0]["rect"] = {"x": 55, "y": 99, "w": 220, "h": 40}
    after["inventory"]["globals"][0]["scroll"].update(height=2500, viewport=500)
    original = deepcopy(before)
    evidence = evidence_view(before)

    assert progress_digest(before) == progress_digest(after)
    assert semantic_digest(before) != semantic_digest(after)
    assert evidence_view(before) == evidence
    assert evidence_view(before) != evidence_view(after)
    assert before == original


@pytest.mark.parametrize(("location", "key", "value"), [
    ("page", "url", "https://example.test/result"),
    ("page", "title", "New results"),
    ("page", "text", "Options\nApplied\nConfirm"),
    ("query", "value", "north"),
    ("query", "readonly", True),
    ("query", "role", "searchbox"),
    ("query", "invalid", True),
    ("choice", "current_value", "North"),
    ("choice", "options_complete", False),
    ("confirm", "pressed", "true"),
    ("confirm", "checked", "true"),
    ("confirm", "selected", "true"),
    ("confirm", "expanded", "true"),
    ("confirm", "submits_form", False),
    ("context", "heading", "Other options"),
    ("context", "kind", "form"),
    ("scroll", "y", 500),
    ("panel_scroll", "y", 300),
])
def test_actual_observable_changes_are_not_cycles(location, key, value):
    before, after = page(), page()
    destinations = {
        "page": after, "query": after["inventory"]["nodes"][0],
        "choice": after["inventory"]["nodes"][1], "confirm": after["inventory"]["nodes"][2],
        "context": after["contexts"][0], "scroll": after["scroll"],
        "panel_scroll": after["inventory"]["globals"][0]["scroll"],
    }
    destinations[location][key] = value
    assert progress_digest(before) != progress_digest(after)


@pytest.mark.parametrize("change", ["control_order", "duplicate_control", "option_order", "option_value",
                                   "duplicate_option", "context_order", "context_membership"])
def test_order_multiplicity_and_semantic_grouping_are_preserved(change):
    before = page()
    # Distinct groups may have identical visible descriptions.
    before["contexts"].append({"id": "r20", "kind": "dialog", "heading": "Options"})
    if change == "context_order":
        before["contexts"][1]["heading"] = "Other options"
    after = deepcopy(before)
    nodes = after["inventory"]["nodes"]
    if change == "control_order":
        nodes[0], nodes[1] = nodes[1], nodes[0]
    elif change == "duplicate_control":
        nodes.pop()
    elif change == "option_order":
        nodes[1]["options"].reverse()
    elif change == "option_value":
        nodes[1]["options"][0]["value"] = "changed"
    elif change == "duplicate_option":
        nodes[1]["options"].append(deepcopy(nodes[1]["options"][0]))
    elif change == "context_order":
        after["contexts"].reverse()
    else:
        nodes[2]["context"] = after["contexts"][1]
    assert progress_digest(before) != progress_digest(after)


def test_untruncated_observed_values_and_text_cannot_hide_real_change():
    before = page()
    before["text"] = "x" * 7000
    before["inventory"]["nodes"][0]["value"] = "x" * 3000
    changed_text, changed_value = deepcopy(before), deepcopy(before)
    changed_text["text"] += "new"
    changed_value["inventory"]["nodes"][0]["value"] += "new"
    assert progress_digest(before) != progress_digest(changed_text)
    assert progress_digest(before) != progress_digest(changed_value)


def test_legacy_action_pairs_share_control_without_losing_distinct_identical_controls():
    before = {"actions": [
        {"node": 1, "kind": "fill", "role": "textbox", "label": "Query", "value": "North"},
        {"node": 1, "kind": "click", "role": "textbox", "label": "Query", "value": ""},
        {"node": 2, "kind": "click", "role": "button", "label": "Confirm"},
        {"node": 3, "kind": "click", "role": "button", "label": "Confirm"},
    ]}
    after = renumber(before)
    assert progress_digest(before) == progress_digest(after)
    after["actions"][0]["value"] = "South"
    assert progress_digest(before) != progress_digest(after)


def test_scroll_direction_availability_and_horizontal_position_are_preserved():
    before, after = page(), page()
    after["inventory"]["globals"][0]["direction"] = "up"
    assert progress_digest(before) != progress_digest(after)
    after = page()
    after["page_key"][2] = 300
    assert progress_digest(before) != progress_digest(after)


def test_fallback_operation_capabilities_change_digest_but_alternative_order_does_not():
    click = {"node": 1, "kind": "click", "role": "textbox", "label": "Query", "value": ""}
    fill = {**click, "kind": "fill"}
    assert progress_digest({"actions": [click]}) != progress_digest({"actions": [fill]})
    assert progress_digest({"actions": [click]}) != progress_digest({"actions": [click, fill]})
    assert progress_digest({"actions": [click, fill]}) == progress_digest({"actions": [fill, click]})
    # An equivalent alternative of one control is not a second independent control.
    assert progress_digest({"actions": [click, fill]}) != progress_digest({
        "actions": [click, {**fill, "node": 2}],
    })
