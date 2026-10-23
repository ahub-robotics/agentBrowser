"""Review scheduling across observed changes, using only offline providers."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import page
from test_verification_workflow import pick, review, session

from AgentBrowser import agent as loop


@pytest.mark.parametrize(("expanded", "submits_form", "role", "peer_open", "calls"), [
    ("false", False, "button", False, 0),
    (False, False, "button", False, 0),
    ("true", False, "button", False, 1),
    ("false", True, "button", False, 1),
    ("false", None, "button", False, 1),
    ("false", False, "link", False, 1),
    ("false", False, "button", True, 1),
])
def test_disclosure_exploration_keeps_submit_navigation_and_panel_exit_gated(
    monkeypatch, expanded, submits_form, role, peer_open, calls,
):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    observed = deepcopy(page())
    observed["actions"][2].update(expanded=expanded, submits_form=submits_form, role=role)
    if peer_open:
        observed["actions"][1]["expanded"] = "true"
    agent._install_page(observed)
    browser.observe.return_value = observed
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    state = agent.command("tick")
    assert reviewer.call_count == calls
    assert state["verification_calls"] == calls
    assert len(state["history"]) == 1
    browser.act.assert_called_once()
    assert browser.observe.call_count == 2  # initial read and post-action snapshot


def test_scrolling_a_region_out_of_view_does_not_review_the_next_field(monkeypatch):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    before = deepcopy(page())
    before["contexts"] = [{"id": "above", "kind": "region", "heading": "Above the field"}]
    before["actions"].append({"id": "scroll", "kind": "scroll", "label": "Page down", "delta": 300})
    after = deepcopy(before)
    after.update(contexts=[], fingerprint="scrolled", scroll={"y": 300})
    agent._install_page(before)
    browser.observe.return_value = after
    scrolling = {**pick("scroll"), "operation": "SCROLL_DOWN", "target": None}
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[scrolling, pick("e1"), pick("DONE")]))
    helper = Mock(return_value=("book", {"model": "offline", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_text", helper)
    state = agent.command("tick")
    assert any(effect["kind"] == "closed" and effect["observed_only"]
               for effect in state["history"][0]["effects"])
    agent.command("tick")
    reviewer.assert_not_called()
    helper.assert_called_once()
    assert browser.act.call_count == 2 and browser.observe.call_count == 3
    agent.command("predict")
    reviewer.assert_called_once()  # DONE still needs independent semantic review
    assert reviewer.call_args.args[0]["selected_action"]["operation"] == "DONE"


@pytest.mark.parametrize("state", ["pressed", "checked", "selected"])
def test_native_submit_cannot_bypass_review_using_toggle_attributes(monkeypatch, state):
    reviewer = Mock(side_effect=lambda payload: review(payload, disposition="reconsider"))
    agent, browser = session(monkeypatch, reviewer)
    observed = deepcopy(page())
    observed["actions"][2].update({"submits_form": True, "expanded": "false", state: "false"})
    agent._install_page(observed)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    result = agent.command("tick")
    reviewer.assert_called_once()
    browser.act.assert_not_called()
    assert result["history"] == []


def test_navigation_still_reviews_the_new_state_before_the_next_action(monkeypatch):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    destination = {**deepcopy(page()), "url": "https://example.test/detail", "fingerprint": "destination"}
    browser.observe.return_value = destination
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[pick(), pick("e1")]))
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {"model": "offline", "latency_ms": 1})))
    agent.command("tick")
    agent.command("tick")
    assert reviewer.call_count == 2
    assert reviewer.call_args.args[0]["trigger"] == "transition"
    assert browser.act.call_count == 2 and browser.observe.call_count == 3


def test_rejected_done_is_reviewed_again_when_actual_evidence_changes(monkeypatch):
    answers = iter(("unknown", "satisfied"))
    reviewer = Mock(side_effect=lambda payload: review(payload, status=next(answers)))
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick("DONE")))
    assert agent.command("tick")["status"] == "ready"
    changed = {**deepcopy(page()), "text": "The requested outcome is now confirmed", "fingerprint": "new-proof"}
    agent._install_page(changed)
    assert agent.command("tick")["status"] == "done"
    assert reviewer.call_count == 2
    browser.act.assert_not_called()
