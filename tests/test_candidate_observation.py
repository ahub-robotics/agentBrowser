"""Offline browser-boundary contracts for full inventories and stable DOM freshness."""

from copy import deepcopy
from unittest.mock import Mock

from AgentBrowser import browser
from AgentBrowser.candidates import CandidateLimits


def inventory_page():
    return {
        "url": "https://example.test/", "title": "Fixture", "text": "Fixture", "scroll": {"y": 0},
        "marker": ["dom", "visible text"], "inventory_marker": ["dom"], "guards": {"1": ["guard"]},
        "inventory": {
            "version": 1,
            "nodes": [
                {"node": 1, "region": "root", "role": "combobox", "label": "Options", "operations": ["select"],
                 "current_value": "First", "options": [
                     {"option": 2, "value": "duplicate", "label": "Same"},
                     {"option": 3, "value": "duplicate", "label": "Same"},
                 ]},
                {"node": 4, "region": "root", "role": "button", "label": "Continue", "operations": ["click"]},
            ],
            "globals": [{"id": "wait", "kind": "wait", "label": "Wait"}],
            "coverage": {"extraction_complete": True},
        },
    }


def test_browser_observe_materializes_a_bounded_standalone_preview(monkeypatch):
    cdp = Mock(return_value={"result": {"value": inventory_page()}})
    monkeypatch.setattr(browser, "cdp", cdp)
    limits = CandidateLimits(max_options_per_select=1)
    page = browser.browser_operation({
        "operation": "observe", "session": "offline", "screenshot": False, "candidate_limits": limits,
    })
    assert len(page["inventory"]["nodes"][0]["options"]) == 2
    assert sum(action["kind"] == "select" for action in page["actions"]) == 1
    assert any(action["label"] == "Continue" for action in page["actions"])
    assert page["omitted_actions"] == 1
    assert cdp.call_count == 1


def test_inventory_fingerprint_tracks_pending_candidates_and_ignores_window():
    page = inventory_page()
    page["actions"] = [{"id": "first", "kind": "click"}]
    other = deepcopy(page)
    other["actions"] = [{"id": "next-window", "kind": "select"}]
    other["screenshot"] = "unrelated image"
    assert browser.fingerprint(page) == browser.fingerprint(other)
    other["inventory_marker"] = ["pending option replaced"]
    assert browser.fingerprint(page) != browser.fingerprint(other)


def test_action_freshness_checks_pending_inventory_and_selected_guard():
    page = inventory_page()
    instance = browser.Browser.__new__(browser.Browser)
    instance.candidate_limits = CandidateLimits(max_controls_inspected=7, max_options_inspected=9)
    instance.evaluate = Mock(return_value=[page["inventory_marker"], page["guards"]["1"]])
    action = {"kind": "select", "node": 1, "option": 2}
    assert instance.fresh(page, action)
    instance.evaluate.return_value = [["pending option replaced"], page["guards"]["1"]]
    assert not instance.fresh(page, action)
    # Freshness uses exactly the same extraction authority, including custom caller limits.
    expression = instance.evaluate.call_args.args[0]
    assert '"max_controls_inspected": 7' in expression
    assert '"max_options_inspected": 9' in expression
    assert page["inventory"]["nodes"][0]["options"][1]["option"] == 3


def test_repeated_terminal_freshness_does_not_rotate_or_mutate_inventory():
    page = inventory_page()
    original = deepcopy(page)
    instance = browser.Browser.__new__(browser.Browser)
    instance.evaluate = Mock(return_value=page["marker"])
    assert instance.fresh(page)
    assert instance.fresh(page)
    assert page == original


def test_invalid_action_node_fails_without_browser_read():
    instance = browser.Browser.__new__(browser.Browser)
    instance.evaluate = Mock()
    assert not instance.fresh(inventory_page(), {"kind": "select", "node": "1"})
    instance.evaluate.assert_not_called()
