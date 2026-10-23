"""Replay the missing tenth item and bound premature discovery DONE recovery."""

import gzip
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_agent import decision, page
from test_coordinator_recovery import V2Supervisor, browser_for, contract, initialize, ready_agent

from agentrun import SupervisedAgent
from agentrun.supervised import SupervisorLimits
from AgentBrowser import agent as worker_module
from AgentBrowser import model


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No paid APIs"))))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text API")))
    monkeypatch.setattr(worker_module, "choose", Mock(return_value={
        **decision("DONE"), "operation": "DONE", "target": None,
    }))


@pytest.fixture
def recorded():
    path = Path(__file__).parent / "fixtures/discovery_context_34fd220.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


def recorded_agent(tmp_path, recorded, supervisor):
    agent = SupervisedAgent(page()["url"], recorded["state"]["goal"], state_dir=tmp_path,
                            run_id=recorded["source_run_id"], supervisor=supervisor,
                            browser=browser_for(), trace=False)
    agent.state.update(deepcopy(recorded["state"]))
    agent._save()
    return agent


def missing_link(payload):
    return next(link for link in payload["observed_links"] if "71361745-covo.aspx" in link["href"])


def test_recorded_large_context_preserves_missing_url_and_outcomes_without_mutating_state(tmp_path, recorded):
    assert recorded["original_catalog_size"] == 0
    assert recorded["original_catalog_omitted"] == 48
    with recorded_agent(tmp_path, recorded, lambda _: {}) as agent:
        before = deepcopy(agent.state)
        payload = agent._payload()
        link = missing_link(payload)
        assert link["label"] == "VOLVO XC60 2.0 B4 D Core Auto"
        assert link["ref"] in agent.state["sent_refs"]
        assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 120 * 1024
        assert len(payload["observed_links"]) + payload["observed_links_omitted"] == 48
        assert payload["discovery_progress"]["favorites-loop"] == {
            "registered": 9, "completed": 9, "limit": 10, "recoveries": 0,
        }
        for key in ("items", "requirements", "contract", "last_result", "evidence", "subtask_reviews"):
            assert agent.state[key] == before[key]
        for key, review in payload["subtask_reviews"].items():
            original = before["subtask_reviews"][key]
            assert review["task"]["success_criteria"] == original["task"]["success_criteria"]
            assert review["evidence_refs"] == original["evidence_refs"]
            assert review["task"]["instruction_omitted"] is True
        assert payload["next_evidence_offset"] == len(payload["evidence"])
        agent.browser.act.assert_not_called()
        worker_module.choose.assert_not_called()


def test_missing_recorded_item_is_scheduled_without_repeating_nine_completed_items(tmp_path, recorded):
    seen = []

    def supervisor(payload):
        seen.append(deepcopy(payload))
        # Only link selection is simulated; the failed response remains recorded.
        response = deepcopy(recorded["original_response"])
        response["discovered"] = [{"loop_id": "favorites-loop", "item_refs": [missing_link(payload)["ref"]]}]
        return response

    with recorded_agent(tmp_path, recorded, supervisor) as agent:
        completed = deepcopy(agent.state["items"])
        agent.state["discovery_recoveries"]["favorites-loop"] = 1
        agent._decide()
        assert agent.state["stop_reason"] is None
        assert len(agent.state["items"]) == 10
        for key, item in completed.items():
            assert agent.state["items"][key] == item and item["status"] == "completed"
        work = agent.state["active_work"]
        assert work["kind"] == "item" and work["template_id"] == "favorite-and-verify"
        assert "71361745-covo.aspx" in agent.state["items"][work["item_id"]]["url"]
        assert agent.state["discovery_recoveries"]["favorites-loop"] == 0
        assert agent.state["pending_delegation"]["input"]["item_ref"] == work["item_id"]
        assert agent.state["supervisor_calls"] == len(seen) == 1
        assert agent.store.attempts() == []
        agent.browser.act.assert_not_called()
        worker_module.choose.assert_not_called()


def test_recorded_premature_done_gets_a_new_bounded_discovery_task_instead_of_reading_same_page(tmp_path, recorded):
    with recorded_agent(tmp_path, recorded, lambda _: deepcopy(recorded["original_response"])) as agent:
        completed = deepcopy(agent.state["items"])
        reads = agent.browser.observe.call_count
        agent._decide()
        assert agent.state["stop_reason"] is None
        assert agent.state["items"] == completed
        assert agent.state["active_work"]["kind"] == "discover"
        task = agent.state["pending_delegation"]["input"]
        assert task["success_criteria"] == agent.state["contract"]["loops"][0]["discovery"]["success_criteria"]
        assert "9 distinct relevant items are registered" in task["instruction"]
        assert "did not verify the previous DONE" in task["instruction"]
        assert agent.state["discovery_recoveries"]["favorites-loop"] == 1
        assert agent.store.load_state()["discovery_recoveries"] == {"favorites-loop": 1}
        assert agent.browser.observe.call_count == reads
        agent.browser.act.assert_not_called()
        worker_module.choose.assert_not_called()


def unfinished_discovery(payload):
    last = payload.get("last_result")
    return {"next_action": "continue", **({"subtask_review": {
        "delegation_id": last["delegation_id"], "status": "unsatisfied", "evidence_refs": [],
    }} if last else {})}


def test_repeated_premature_done_stops_after_two_continuations_without_reset_on_resume(tmp_path):
    policy = V2Supervisor(unfinished_discovery, plan=contract(loop=True))
    with ready_agent(tmp_path, supervisor=policy, trace=False) as agent:
        initialize(agent)
        agent._decide()
        agent._delegate()
        agent._decide()
        assert agent.state["discovery_recoveries"] == {"results": 1}
        agent._delegate()
        assert agent.state["last_result"]["status"] == "claimed_done"
        assert agent.state["pending_delegation"] is None
        calls = agent.state["supervisor_calls"]
    with SupervisedAgent.resume("recovery", state_dir=tmp_path, browser=browser_for(),
                                supervisor=policy, trace=False) as resumed:
        result = list(resumed.run())[-1]
        assert result["stop_reason"] == "discovery_not_verified"
        assert resumed.state["discovery_recoveries"] == {"results": 2}
        assert resumed.state["delegation_sequence"] == 3
        assert resumed.state["supervisor_calls"] == calls + 2
        assert worker_module.choose.call_count == 3
        assert resumed.state["read_recoveries"] == 0
        assert resumed.store.attempts() == []
        resumed.browser.act.assert_not_called()


def test_discovery_continuation_respects_global_decision_budget(tmp_path):
    policy = V2Supervisor(unfinished_discovery, plan=contract(loop=True))
    with ready_agent(tmp_path, supervisor=policy, trace=False,
                     limits=SupervisorLimits(max_decisions=1)) as agent:
        result = list(agent.run())[-1]
        assert result["stop_reason"] == "decision_budget"
        assert agent.state["delegation_sequence"] == 1
        assert worker_module.choose.call_count == 1
        agent.browser.act.assert_not_called()


def test_uncertain_browser_effect_blocks_discovery_continuation(tmp_path, recorded):
    with recorded_agent(tmp_path, recorded, lambda _: deepcopy(recorded["original_response"])) as agent:
        observed = page()
        attempt = agent.store.reserve_attempt(agent.state["last_result"]["delegation_id"], None,
                                              observed["actions"][2], observed)
        before = agent.state["delegation_sequence"]
        agent._decide()
        assert agent.state["stop_reason"] == "uncertain_action"
        assert agent.state["pending_delegation"] is None
        assert agent.state["delegation_sequence"] == before
        assert agent.state["discovery_recoveries"] == {}
        assert [a["id"] for a in agent.store.attempts(unresolved_only=True)] == [attempt]
        agent.browser.act.assert_not_called()
        worker_module.choose.assert_not_called()


def test_catalog_prioritizes_unregistered_urls_and_treats_aliases_as_registered(tmp_path):
    with ready_agent(tmp_path, trace=False) as agent:
        old = deepcopy(agent.state["evidence"])
        urls = [f"https://example.test/items/{i}" for i in range(85)]
        for i, url in enumerate(urls):
            agent.state["evidence"][f"link-{i}"] = {
                "ref": f"link-{i}", "kind": "control", "href": url,
                "observed_at": i, "observed_url": page()["url"], "label": f"Item {i}",
            }
        agent.state["observed_url_order"] = urls
        agent.state["items"] = {"existing": {
            "url": urls[0], "aliases": urls[1:84], "status": "completed", "evidence_refs": [],
        }}
        payload = agent._payload()
        assert payload["observed_links"][0]["href"] == urls[-1]
        assert payload["observed_links_total"] == 85 and payload["observed_links_omitted"] == 5
        assert {ref: agent.state["evidence"][ref] for ref in old} == old
        assert "link-84" in agent.state["sent_refs"]
        agent.browser.act.assert_not_called()
