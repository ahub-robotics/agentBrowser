"""Completed browser actions supply a newly observed page to the next decision."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import decision, page

from AgentBrowser import agent as loop
from AgentBrowser.browser import StalePage, fingerprint


def session(monkeypatch, action, operation, *, unchanged_fingerprint=False, stale_once=False):
    before = page()
    before["actions"] = [item for item in before["actions"] if item["id"] != action["id"]] + [action]
    before["fingerprint"] = fingerprint(before)
    after = deepcopy(before)
    # Title is intentionally outside the fingerprint: an unchanged fingerprint
    # must not suppress the observation or discard its other newly read fields.
    after["title"] = "After the browser action"
    if not unchanged_fingerprint:
        after["text"] = "A new result is visible"
        after["actions"].append({
            "id": "new-result", "kind": "click", "node": 99,
            "label": "New result", "role": "button", "value": "",
        })
        after["fingerprint"] = fingerprint(after)
    events, seen = [], []
    current = before
    failed_observation = False

    def observe(*, screenshot):
        nonlocal failed_observation
        if current is after:
            # Execution must already be durable before a read that can fail.
            assert agent.state["decision"] is None
            assert agent.state["history"][-1]["choice"] == action["id"]
            if stale_once and not failed_observation:
                failed_observation = True
                events.append("observe_stale")
                raise StalePage("Navigation interrupted the post-action read")
            events.append("observe")
        return deepcopy(current)

    def act(chosen, observed, *, text):
        nonlocal current
        assert chosen == action
        assert observed["title"] == before["title"]
        events.append("act")
        current = after

    def choose(observed, _goal, _history):
        events.append("choose")
        seen.append({key: deepcopy(observed[key]) for key in ("title", "text", "actions")})
        assert observed["_candidate_request"]["state"]["page"]["title"] == observed["title"]
        if len(seen) == 1:
            targets = loop.action_space(observed["actions"])[1].get(operation, {})
            target = next((index for index, item in targets.items() if item["id"] == action["id"]), None)
            return {**decision(action["id"]), "operation": operation, "target": target}
        return {**decision("DONE"), "operation": "DONE", "target": None}

    browser = Mock(fresh=Mock(return_value=True), observe=Mock(side_effect=observe), act=Mock(side_effect=act))
    monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))
    monkeypatch.setattr(loop, "choose", choose)
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {"model": "offline", "latency_ms": 0})))
    agent = loop.Agent(before["url"], "Find a book", verification=False)
    return agent, browser, after, seen, events


@pytest.mark.parametrize(("action", "operation", "mode", "unchanged_fingerprint"), [
    ({"id": "click", "kind": "click", "node": 30, "label": "Open", "role": "button"},
     "CLICK", "manual", False),
    ({"id": "fill", "kind": "fill", "node": 30, "label": "Query", "role": "textbox", "value": ""},
     "TYPE_TEXT", "run", False),
    ({"id": "select", "kind": "select", "node": 30, "option": 31,
      "label": "Category → Books", "role": "combobox", "value": "books"},
     "SELECT", "manual", False),
    ({"id": "scroll_down", "kind": "scroll", "delta": 500, "label": "Scroll down"},
     "SCROLL_DOWN", "run", False),
    ({"id": "scroll_up", "kind": "scroll", "delta": -500, "label": "Scroll up"},
     "SCROLL_UP", "tick", False),
    ({"id": "wait", "kind": "wait", "label": "Wait for results"},
     "WAIT", "manual", True),
])
def test_every_completed_action_refreshes_snapshot_before_next_choice(
    monkeypatch, action, operation, mode, unchanged_fingerprint,
):
    agent, browser, after, seen, events = session(
        monkeypatch, action, operation, unchanged_fingerprint=unchanged_fingerprint,
    )
    with agent:
        if mode == "run":
            first, _terminal = list(agent.run())
        else:
            if mode == "manual":
                agent.command("predict")
                first = agent.command("act", agent.decision_body())
            else:
                first = agent.command("tick")
            agent.command("predict")
        assert first["page"]["title"] == after["title"]
        assert first["page"]["actions"] == after["actions"]
        assert first["history"][0]["page_changed"] is not unchanged_fingerprint
        assert seen[0]["title"] == "Search"
        assert seen[1] == {key: after[key] for key in ("title", "text", "actions")}
        assert events == ["choose", "act", "observe", "choose"]
        browser.act.assert_called_once()


def test_failed_post_action_read_recovers_new_snapshot_without_repeating_action(monkeypatch):
    action = {"id": "open", "kind": "click", "node": 30, "label": "Open", "role": "button"}
    agent, browser, after, seen, events = session(monkeypatch, action, "CLICK", stale_once=True)
    with agent:
        first, _terminal = list(agent.run())
        assert first["page"]["title"] == after["title"]
        assert seen[1] == {key: after[key] for key in ("title", "text", "actions")}
        assert events == ["choose", "act", "observe_stale", "observe", "choose"]
        assert len(agent.state["history"]) == 1
        browser.act.assert_called_once()
