"""Discovery exposes links incrementally and returns control without processing them."""

from copy import deepcopy
from unittest.mock import Mock

from test_agent import decision
from test_supervised_loops import GOAL, ITEM_A, LIST_URL, LoopBrowser, listing, loop_update, reply

from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from AgentBrowser import agent as jev
from AgentBrowser.browser import fingerprint


class DiscoveryBrowser(LoopBrowser):
    def __init__(self):
        super().__init__(listing(()))
        self.page["actions"] = [{
            "id": "scroll", "kind": "scroll", "label": "Page — scroll down", "node": 500,
            "direction": "down", "role": "document", "scroll_y": 0,
        }]
        self.page["fingerprint"] = fingerprint(self.page)

    def act(self, action, page, text=None):
        self.inputs.append(deepcopy(action))
        self.page = listing((ITEM_A,))


def test_discovery_yields_after_one_scroll_exposes_a_new_link_without_another_jev_call(tmp_path, monkeypatch):
    browser = DiscoveryBrowser()
    chooser = Mock(return_value=decision("scroll"))
    monkeypatch.setattr(jev, "choose", chooser)
    monkeypatch.setattr(jev, "field_text", Mock(side_effect=AssertionError("No text model")))
    payloads = []

    def policy(payload):
        payloads.append(deepcopy(payload))
        if len(payloads) == 1:
            return reply("delegate", loops=[loop_update()], task={
                "id": "expose", "scope": "discover", "instruction": "Scroll to expose more result links.",
                "success_criteria": ["New result links are observed."],
            })
        assert payload["last_result"]["status"] == "yielded"
        assert payload["last_result"]["stop_reason"] == "observed_links_discovered"
        assert payload["observed_links"][0]["href"] == ITEM_A
        return reply(loops=[loop_update([payload["observed_links"][0]["ref"]])])

    with SupervisedAgent(LIST_URL, GOAL, browser=browser, state_dir=tmp_path, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "waiting_feedback"
        assert len(payloads) == 2
        assert chooser.call_count == final["metrics"]["jev_calls"] == 1
        assert [action["kind"] for action in browser.inputs] == ["scroll"]
        assert final["metrics"]["actions"] == 1
        assert agent.worker._subtask_budget is None
        item = next(iter(final["items"].values()))
        assert item["url"] == ITEM_A and item["status"] == "pending"
        assert not item["visited_refs"]
