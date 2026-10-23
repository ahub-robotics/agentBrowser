"""Observed browser history, exact-tab reattachment and uncertain input guards."""

from copy import deepcopy
from unittest.mock import Mock, call

import pytest
from test_agent import page

from AgentBrowser import browser
from AgentBrowser.browser import Browser, MutationUncertain, StalePage
from AgentBrowser.model import prepare_candidates


def navigation_history():
    return {
        "currentIndex": 1,
        "entries": [
            {"id": 10, "url": "https://example.test/results"},
            {"id": 11, "url": "https://example.test/item/1"},
        ],
    }


def instance():
    owned = Browser.__new__(Browser)
    owned.session, owned.target = "session", "original-tab"
    owned.enable_history = True
    return owned


def test_observation_offers_back_from_real_history_only(monkeypatch):
    observed = page()
    observed["marker"] = "marker"
    monkeypatch.setattr(browser, "browser_operation", Mock(return_value=observed))
    owned = instance()
    owned.call = Mock(return_value=navigation_history())
    snapshot = owned.observe(screenshot=False)
    assert snapshot["actions"][-1]["id"] == "back"
    assert snapshot["actions"][-1]["history_entry"] == 10
    body = prepare_candidates(snapshot, "Return to results", [])["_candidate_request"]
    assert body["questions"]["operation"]["criteria"]["BACK"] == "Back to https://example.test/results"
    assert "back_target" not in body["questions"]


def test_old_direct_observations_do_not_request_history(monkeypatch):
    owned = instance()
    owned.enable_history = False
    owned.call = Mock()
    monkeypatch.setattr(browser, "browser_operation", Mock(return_value=page()))
    assert all(action["id"] != "back" for action in owned.observe(screenshot=False)["actions"])
    owned.call.assert_not_called()


@pytest.mark.parametrize("index", [-1, 0, 2, None])
def test_no_invented_back_without_previous_entry(index):
    assert browser._back_action({**navigation_history(), "currentIndex": index}) is None


def test_back_command_validates_history_and_dispatches_once(monkeypatch):
    observed = navigation_history()
    transport = Mock(side_effect=[observed, {}])
    monkeypatch.setattr(browser, "cdp", transport)
    action = browser._back_action(observed)
    result = browser.browser_operation({"operation": "act", "session": "owned", "action": action})
    assert result == {"executed": "back"}
    assert transport.call_args_list == [
        call("Page.getNavigationHistory", session_id="owned"),
        call("Page.navigateToHistoryEntry", session_id="owned", entryId=10),
    ]


def test_changed_back_entry_is_rejected_before_navigation(monkeypatch):
    action = browser._back_action(navigation_history())
    changed = deepcopy(navigation_history())
    changed["entries"][0]["url"] = "https://different.test/"
    transport = Mock(return_value=changed)
    monkeypatch.setattr(browser, "cdp", transport)
    with pytest.raises(StalePage):
        browser.browser_operation({"operation": "act", "session": "owned", "action": action})
    transport.assert_called_once_with("Page.getNavigationHistory", session_id="owned")


def test_history_changed_since_observation_invalidates_back():
    owned = instance()
    observed = navigation_history()
    owned.call = Mock(return_value={**observed, "currentIndex": 0})
    owned.evaluate = Mock()
    assert owned.fresh(page(), browser._back_action(observed)) is False
    owned.evaluate.assert_not_called()


def test_partial_click_exception_is_uncertain_never_stale(monkeypatch):
    transport = Mock(side_effect=[
        {"result": {"value": {"x": 10, "y": 20}}}, {}, StalePage("Lost mouse release"),
    ])
    monkeypatch.setattr(browser, "cdp", transport)
    with pytest.raises(MutationUncertain):
        browser.browser_operation({"operation": "act", "session": "owned", "action": {
            "id": "e1", "node": 1, "kind": "click",
        }})
    assert transport.call_count == 3


def test_lost_back_acknowledgement_cannot_be_replayed(monkeypatch):
    transport = Mock(side_effect=[navigation_history(), RuntimeError("Connection lost")])
    monkeypatch.setattr(browser, "cdp", transport)
    with pytest.raises(MutationUncertain):
        browser.browser_operation({
            "operation": "act", "session": "owned", "action": browser._back_action(navigation_history()),
        })
    assert transport.call_count == 2


def test_attach_uses_exact_existing_tab_without_creating_or_navigating(monkeypatch):
    transport = Mock(side_effect=[
        {"targetInfos": [{"targetId": "original-tab", "type": "page"}]},
        {"sessionId": "new-session"}, {}, {},
    ])
    monkeypatch.setattr(browser, "ensure_daemon", Mock())
    monkeypatch.setattr(browser, "cdp", transport)
    attached = Browser.attach("original-tab")
    assert attached.target == "original-tab"
    assert attached.session == "new-session"
    methods = [args.args[0] for args in transport.call_args_list]
    assert methods == [
        "Target.getTargets", "Target.attachToTarget",
        "Emulation.setDeviceMetricsOverride", "Emulation.setFocusEmulationEnabled",
    ]


def test_missing_owned_tab_is_never_replaced(monkeypatch):
    transport = Mock(return_value={"targetInfos": [{"targetId": "another-tab", "type": "page"}]})
    monkeypatch.setattr(browser, "ensure_daemon", Mock())
    monkeypatch.setattr(browser, "cdp", transport)
    with pytest.raises(RuntimeError, match="unavailable"):
        Browser.attach("original-tab")
    transport.assert_called_once_with("Target.getTargets")
