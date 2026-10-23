"""Supervisor handoffs preserve citable outcomes after leaving a busy page."""

from copy import deepcopy

import pytest
from test_agent import decision
from test_supervised_loops import ITEM_A, LIST_URL, LoopBrowser, links, listing

from agentrun import supervised
from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from AgentBrowser import agent as jev
from AgentBrowser.browser import fingerprint

GOAL = "For each result, fill a draft message and return to the list. Limit to one result."
MESSAGE = "Please send more information."


class DraftBrowser(LoopBrowser):
    def __init__(self):
        super().__init__(listing(tuple(
            [ITEM_A] + [f"https://example.test/items/{index}" for index in range(1, 220)],
        )))

    def act(self, action, page, text=None):
        if action["kind"] == "fill":
            self.inputs.append(deepcopy(action))
            self.page["actions"][0]["value"] = text
        else:
            super().act(action, page, text)
            if action["kind"] == "back":
                # A new observed list state makes old detail evidence compete
                # with the new controls when the retained-fact limit is reached.
                self.page["text"] += " Returned from detail."
            else:
                self.page["actions"] = [{
                    "id": "message", "kind": "fill", "label": "Draft message",
                    "role": "textbox", "value": "", "node": 1000,
                }, *[{
                    "id": f"detail-control-{index}", "kind": "click", "role": "button",
                    "label": f"Detail option {index}", "node": index + 1001,
                } for index in range(80)], *self.page["actions"]]
        self.page["fingerprint"] = fingerprint(self.page)


def requirement(status="unsatisfied", refs=()):
    return {
        "id": "draft", "goal_quote": GOAL, "description": "Draft one message and return",
        "status": status, "evidence_refs": list(refs),
    }


def draft_decision(page, _goal, history):
    if page["url"] == LIST_URL:
        choice = "DONE" if any(entry.get("kind") == "fill" for entry in history) else "item-1"
    else:
        choice = "message" if not page["actions"][0]["value"] else "back"
    return decision(choice)


@pytest.mark.parametrize("force_pruning", [False, True])
def test_filled_detail_evidence_is_citable_after_returning_to_large_listing(
    tmp_path, monkeypatch, force_pruning,
):
    if force_pruning:
        monkeypatch.setattr(supervised, "MAX_FACTS", 260)
    monkeypatch.setattr(jev, "choose", draft_decision)
    monkeypatch.setattr(jev, "field_text", lambda *_: (MESSAGE, {"model": "offline", "latency_ms": 0}))
    browser = DraftBrowser()
    seen_payloads = []

    def policy(payload):
        seen_payloads.append(deepcopy(payload))
        if not payload["items"]:
            ref = links(payload)[0]["ref"]
            return {
                "action": "delegate", "requirements": [requirement()],
                "loops": [{
                    "id": "results", "goal_quote": "For each result", "description": "One draft",
                    "item_refs": [ref],
                }],
                "task": {
                    "id": "draft-one", "instruction": "Open the result, fill a draft and return to the list.",
                    "success_criteria": ["The draft value is observed and the list is visible again."],
                    "item_ref": ref,
                },
            }
        assert payload["evidence_offset"] == 0
        assert payload["page"]["url"] == LIST_URL
        facts = {fact["ref"]: fact for fact in payload["evidence"]}
        fill_ref = next(effect["ref"] for entry in payload["last_result"]["history"]
                        for effect in entry["effects"]
                        if effect["kind"] == "value" and effect["after"] == MESSAGE)
        assert fill_ref in facts, "The first review must include the observed fill, despite 220 list controls"
        assert facts[fill_ref]["value"] == MESSAGE
        assert facts[fill_ref]["observed_url"] == ITEM_A
        item = next(iter(payload["items"].values()))
        assert item["visited_refs"]
        assert set(item["visited_refs"]) <= facts.keys()
        list_ref = next(fact["ref"] for fact in payload["evidence"]
                        if fact.get("kind") == "page" and fact.get("url") == LIST_URL)
        refs = [fill_ref, list_ref]
        return {
            "action": "finish", "requirements": [requirement("satisfied", refs)],
            "loops": [{
                "id": "results", "goal_quote": "For each result", "description": "One draft",
                "exhausted": True, "evidence_refs": refs,
            }],
            "item_reviews": [{
                "item_id": item["item_id"], "status": "completed", "evidence_refs": [fill_ref],
            }],
            "evidence_refs": refs,
        }

    with SupervisedAgent(LIST_URL, GOAL, browser=browser, state_dir=tmp_path, supervisor=policy) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "done"
        assert result["verification"]["status"] == "satisfied"
        assert len(seen_payloads) == 2
        assert [action["kind"] for action in browser.inputs] == ["click", "fill", "back"]
        assert result["metrics"]["supervisor_http_attempts"] == 0
        if force_pruning:
            assert agent.state["evidence_omitted"] > 0
        # Prioritization still offers every retained fact through inspect.
        all_refs, offsets = set(), []
        while True:
            payload = agent._payload()
            refs = {fact["ref"] for fact in payload["evidence"]}
            assert not refs & all_refs
            all_refs.update(refs)
            offsets.append(payload["evidence_offset"])
            if payload["next_evidence_offset"] is None:
                break
            agent.state["evidence_offset"] = payload["next_evidence_offset"]
        assert all_refs == agent.state["evidence"].keys()
        assert len(offsets) > 1
        assert len(browser.inputs) == 3


def test_previous_result_refs_are_retained_and_prioritized_after_new_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(supervised, "MAX_FACTS", 230)
    browser = LoopBrowser()
    with SupervisedAgent(LIST_URL, GOAL, browser=browser, state_dir=tmp_path, supervisor=lambda _: {}) as agent:
        old_ref = next(ref for ref, fact in agent.state["evidence"].items() if fact.get("url") == LIST_URL)
        agent.state["last_result"] = {"history": [], "evidence_refs": [old_ref]}
        # Two complete new inventories otherwise evict the oldest observation.
        for index in range(2):
            current = listing(tuple(f"https://example.test/new/{index}/{node}" for node in range(220)))
            agent._record_page(current)
        payload = agent._payload()
        refs = {fact["ref"] for fact in payload["evidence"]}
        assert agent.state["evidence_omitted"] > 0
        assert old_ref in refs
        assert agent._refs([old_ref], required=True) == [old_ref]
        assert browser.inputs == []
