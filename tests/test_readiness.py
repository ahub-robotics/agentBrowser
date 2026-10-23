"""Bounded read-only recovery and observed scrolling; no browser or paid model calls."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from AgentBrowser import browser


def observation(status="ready", marker="content"):
    return {"marker": marker, "readiness": {"status": status, "reason": "fixture"}, "actions": []}


@pytest.fixture
def instance(monkeypatch):
    clock = {"now": 10.0}
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    b = browser.Browser.__new__(browser.Browser)
    b.session = "fixture"
    b.call = Mock(return_value={"data": "fixture-image"})
    b.observation_timeout = 0.2
    b.clock = clock
    return b


def test_loading_then_content_repeats_only_reads(instance, monkeypatch):
    operation = Mock(side_effect=[observation("loading"), observation()])
    monkeypatch.setattr(browser, "browser_operation", operation)
    result = instance.observe(screenshot=False)
    assert result["readiness"]["status"] == "ready"
    assert result["readiness"]["reads"] == 2
    assert result["readiness"]["settled"]
    assert all(call.args[0]["operation"] == "observe" for call in operation.call_args_list)
    instance.call.assert_not_called()


def test_empty_observation_has_bounded_deadline_and_explicit_result(instance, monkeypatch):
    operation = Mock(side_effect=lambda _: observation("unknown", "empty"))
    monkeypatch.setattr(browser, "browser_operation", operation)
    result = instance.observe(screenshot=False)
    assert result["readiness"]["timed_out"]
    assert result["readiness"]["status"] == "unknown"
    assert instance.clock["now"] == pytest.approx(10.2)
    assert operation.call_count < 10


def test_global_deadline_caps_observation_wait(instance, monkeypatch):
    instance.observation_timeout = 2
    instance.observation_deadline = 10.08
    monkeypatch.setattr(browser, "browser_operation", lambda _: observation("loading"))
    result = instance.observe(screenshot=False)
    assert instance.clock["now"] == pytest.approx(10.08)
    assert result["readiness"]["timed_out"]


def test_post_action_observation_waits_for_two_consistent_reads(instance, monkeypatch):
    instance.after_input = {"id": "e1", "kind": "click", "node": 1}
    operation = Mock(side_effect=[observation(marker="old"), observation(marker="new"), observation(marker="new")])
    monkeypatch.setattr(browser, "browser_operation", operation)
    result = instance.observe(screenshot=True)
    assert result["marker"] == "new" and result["screenshot"] == "fixture-image"
    assert result["readiness"]["reads"] == 3 and result["readiness"]["settled"]
    assert instance.after_input is None
    assert [call.args[0] for call in instance.call.call_args_list] == ["Runtime.evaluate", "Page.captureScreenshot"]


def test_interrupted_observation_can_recover_without_input(instance, monkeypatch):
    operation = Mock(side_effect=[browser.StalePage("Navigating"), observation()])
    monkeypatch.setattr(browser, "browser_operation", operation)
    assert instance.observe(screenshot=False)["readiness"]["reads"] == 2
    instance.call.assert_not_called()


def test_legacy_observation_remains_one_read(instance, monkeypatch):
    operation = Mock(return_value={"actions": []})
    monkeypatch.setattr(browser, "browser_operation", operation)
    assert instance.observe(screenshot=False) == {"actions": []}
    operation.assert_called_once()


def test_region_scroll_dispatches_one_wheel_at_validated_region(monkeypatch):
    call = Mock(side_effect=[{"result": {"value": {"x": 25, "y": 40}}}, {}])
    monkeypatch.setattr(browser, "cdp", call)
    action = {"id": "scroll_12_down", "kind": "scroll", "node": 12, "direction": "down", "delta": 100}
    assert browser.browser_operation({"operation": "act", "session": "test", "action": action}) == {
        "executed": "scroll_12_down",
    }
    assert [entry.args[0] for entry in call.call_args_list] == ["Runtime.evaluate", "Input.dispatchMouseEvent"]
    assert call.call_args.kwargs == {
        "session_id": "test", "type": "mouseWheel", "x": 25, "y": 40, "deltaX": 0, "deltaY": 100,
    }


def test_changed_scroll_region_rejects_before_wheel(monkeypatch):
    call = Mock(return_value={"result": {"value": None}})
    monkeypatch.setattr(browser, "cdp", call)
    with pytest.raises(browser.StalePage, match="Scroll region changed"):
        browser.browser_operation({"operation": "act", "session": "test", "action": {
            "id": "scroll_12_down", "kind": "scroll", "node": 12, "direction": "down", "delta": 100,
        }})
    call.assert_called_once()


def test_scroll_freshness_requires_matching_identity_and_scroll_guard(instance):
    page = {"inventory": {}, "inventory_marker": ["same"], "guards": {"12": ["scroll", 12, 0, 200, 800]}}
    action = {"kind": "scroll", "node": 12}
    instance.evaluate = Mock(return_value=[page["inventory_marker"], page["guards"]["12"]])
    assert instance.fresh(page, action)
    changed = deepcopy(page["guards"]["12"])
    changed[2] = 120
    instance.evaluate.return_value = [page["inventory_marker"], changed]
    assert not instance.fresh(page, action)


LOST_SESSION = "{'code': -32001, 'message': 'Session with given id not found.'}"


def recovery_transport(instance, monkeypatch, *, targets=None):
    instance.target = "owned-page"
    target_infos = targets if targets is not None else [{"targetId": instance.target, "type": "page"}]
    calls = []

    def call(method, **params):
        calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": target_infos}
        assert method == "Target.attachToTarget"
        assert params == {"targetId": "owned-page", "flatten": True}
        return {"sessionId": "replacement-" + str(instance.session_recoveries)}

    monkeypatch.setattr(browser, "cdp", call)
    return calls


def test_lost_session_freshness_discards_decision_after_owned_target_reattach(instance, monkeypatch):
    calls = recovery_transport(instance, monkeypatch)
    instance.evaluate = Mock(side_effect=RuntimeError(LOST_SESSION))
    assert not instance.fresh({"marker": "old"})
    assert instance.session == "replacement-1"
    instance.evaluate.assert_called_once()  # Never revalidate the old decision on the new session.
    assert [method for method, _ in calls] == ["Target.getTargets", "Target.attachToTarget"]
    assert [entry.args[0] for entry in instance.call.call_args_list] == [
        "Emulation.setDeviceMetricsOverride", "Emulation.setFocusEmulationEnabled",
    ]
    assert instance.call.call_args_list[0].kwargs == {
        "width": 1120, "height": 780, "deviceScaleFactor": 1, "mobile": False,
    }


def test_lost_session_during_preinput_freshness_never_dispatches_action(instance, monkeypatch):
    recovery_transport(instance, monkeypatch)
    instance.evaluate = Mock(side_effect=RuntimeError(LOST_SESSION))
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(browser.StalePage, match="Page changed"):
        instance.act({"kind": "wait", "id": "wait"}, {"marker": "old"})
    operation.assert_not_called()


def test_observation_reattaches_then_repeats_only_known_read(instance, monkeypatch):
    recovery_transport(instance, monkeypatch)
    operation = Mock(side_effect=[RuntimeError(LOST_SESSION), observation()])
    monkeypatch.setattr(browser, "browser_operation", operation)
    result = instance.observe(screenshot=False)
    assert result["readiness"]["settled"] and instance.session_recoveries == 1
    assert [call.args[0]["session"] for call in operation.call_args_list] == ["fixture", "replacement-1"]
    assert all(call.args[0]["operation"] == "observe" for call in operation.call_args_list)


def test_lost_session_during_mutation_is_never_reattached_or_replayed(instance, monkeypatch):
    transport = recovery_transport(instance, monkeypatch)
    instance.fresh = Mock(return_value=True)
    operation = Mock(side_effect=RuntimeError(LOST_SESSION))
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(RuntimeError, match="Session with given id not found"):
        instance.act({"kind": "click", "id": "click", "node": 7}, {})
    operation.assert_called_once()
    assert transport == []


def test_missing_owned_target_does_not_switch_to_another_tab(instance, monkeypatch):
    calls = recovery_transport(instance, monkeypatch, targets=[{"targetId": "someone-else", "type": "page"}])
    instance.evaluate = Mock(side_effect=RuntimeError(LOST_SESSION))
    with pytest.raises(RuntimeError, match="Owned browser target is unavailable"):
        instance.fresh({"marker": "old"})
    assert [method for method, _ in calls] == ["Target.getTargets"]
    assert instance.session == "fixture"


def test_session_recovery_has_a_browser_lifetime_cap(instance, monkeypatch):
    calls = recovery_transport(instance, monkeypatch)
    instance.evaluate = Mock(side_effect=RuntimeError(LOST_SESSION))
    assert not instance.fresh({"marker": "old"})
    assert not instance.fresh({"marker": "new"})
    with pytest.raises(RuntimeError, match="recovery limit reached"):
        instance.fresh({"marker": "newer"})
    assert instance.session_recoveries == 2
    assert [method for method, _ in calls].count("Target.attachToTarget") == 2


def test_expired_global_deadline_prevents_recovery(instance, monkeypatch):
    calls = recovery_transport(instance, monkeypatch)
    instance.observation_deadline = instance.clock["now"]
    instance.evaluate = Mock(side_effect=RuntimeError(LOST_SESSION))
    with pytest.raises(RuntimeError, match="deadline expired"):
        instance.fresh({"marker": "old"})
    assert calls == []


def test_other_cdp_failures_and_arbitrary_evaluate_are_not_recovered(instance, monkeypatch):
    calls = recovery_transport(instance, monkeypatch)
    instance.evaluate = Mock(side_effect=RuntimeError("Unrelated CDP failure"))
    with pytest.raises(RuntimeError, match="Unrelated CDP failure"):
        instance.fresh({"marker": "old"})
    instance.call.side_effect = RuntimeError(LOST_SESSION)
    with pytest.raises(RuntimeError, match="Session with given id not found"):
        browser.Browser.evaluate(instance, "window.arbitraryMutation()")
    assert calls == []


def test_post_action_session_recovery_preserves_record_and_does_not_replay(monkeypatch, instance):
    from test_agent import decision, page

    from AgentBrowser import agent as loop

    calls = recovery_transport(instance, monkeypatch)
    before, after = page(), page()
    after["title"] = "After one action"
    instance.fresh = Mock(return_value=True)
    mutated, interrupted = False, False
    operations = []

    def operation(request):
        nonlocal mutated, interrupted
        operations.append(request["operation"])
        if request["operation"] == "act":
            assert not mutated
            mutated = True
            return {"executed": request["action"]["id"]}
        if not mutated:
            return deepcopy(before)
        assert agent.state["decision"] is None
        assert len(agent.state["history"]) == 1  # Execution is already recorded before the failed read.
        if not interrupted:
            interrupted = True
            raise RuntimeError(LOST_SESSION)
        return deepcopy(after)

    monkeypatch.setattr(browser, "browser_operation", operation)
    monkeypatch.setattr(loop, "Browser", Mock(return_value=instance))
    monkeypatch.setattr(loop, "choose", Mock(return_value={**decision("e3"), "operation": "CLICK", "target": "2"}))
    agent = loop.Agent(before["url"], "Click Go", verification=False)
    state = agent.command("tick")
    assert state["page"]["title"] == "After one action"
    assert len(state["history"]) == 1 and operations.count("act") == 1
    assert instance.session_recoveries == 1
    assert [method for method, _ in calls] == ["Target.getTargets", "Target.attachToTarget"]
