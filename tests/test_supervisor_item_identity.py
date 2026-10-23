"""Observed URL aliases and explicit item delegation never create invented work."""

from copy import deepcopy

import pytest
from test_agent import decision
from test_supervised_loops import GOAL, ITEM_A, LIST_URL, LoopBrowser, listing, loop_update, reply, task

from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from AgentBrowser import agent as jev


@pytest.fixture(autouse=True)
def offline_jev(monkeypatch):
    monkeypatch.setattr(jev, "choose", lambda *_: decision("DONE"))


def test_observed_url_in_loop_registers_the_complete_href_and_its_source_fact(tmp_path):
    browser = LoopBrowser(listing((ITEM_A,)))
    seen = []

    def policy(payload):
        seen.extend(payload["evidence"])
        return reply(loops=[loop_update([ITEM_A])])

    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        item = next(iter(final["items"].values()))
        source = next(fact for fact in seen if fact.get("href") == ITEM_A)
        assert item["url"] == ITEM_A
        assert item["source_ref"] == source["ref"]
        assert item["loop_id"] == "results"
    assert browser.inputs == []


@pytest.mark.parametrize("identity", ["url", "ref"])
def test_explicit_item_task_registers_observed_item_in_its_declared_loop(tmp_path, identity):
    browser = LoopBrowser(listing((ITEM_A,)))
    calls = []

    def policy(payload):
        calls.append(deepcopy(payload))
        if payload["last_result"]:
            return reply()
        source = next(fact for fact in payload["evidence"] if fact.get("href") == ITEM_A)
        ref = ITEM_A if identity == "url" else source["ref"]
        return reply("delegate", loops=[loop_update()], task={
            **task(ref), "scope": "item", "loop_id": "results",
        })

    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["metrics"]["jev_calls"] == 1
        assert len(final["items"]) == 1
        item = next(iter(final["items"].values()))
        assert item["url"] == ITEM_A and item["loop_id"] == "results"
        assert calls[-1]["last_result"]["item_id"] == item["item_id"]
        assert calls[-1]["last_result"]["task"]["scope"] == "item"
    assert browser.inputs == []


@pytest.mark.parametrize("problem", ["invented", "unsent", "truncated"])
def test_url_identity_rejects_unobserved_unsent_or_incomplete_hrefs(tmp_path, problem):
    urls = tuple(f"https://example.test/items/{index}" for index in range(180)) if problem == "unsent" else (ITEM_A,)
    initial = listing(urls, truncated=problem == "truncated")
    browser = LoopBrowser(initial)
    wanted = "https://invented.test/item" if problem == "invented" else urls[-1]

    def policy(payload):
        if problem == "unsent":
            assert wanted not in [fact.get("href") for fact in payload["evidence"]]
        return reply("delegate", loops=[loop_update()], task={
            **task(wanted), "scope": "item", "loop_id": "results",
        })

    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        if problem == "unsent":
            assert any(fact.get("href") == wanted for fact in agent.state["evidence"].values())
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_invalid_response"
        assert final["metrics"]["supervisor_calls"] == 2
        assert final["metrics"]["jev_calls"] == 0
        assert final["items"] == {}
    assert browser.inputs == []


@pytest.mark.parametrize("problem", ["general_scope", "missing_ref", "missing_loop", "unknown_loop"])
def test_automatic_item_registration_requires_item_scope_and_an_existing_loop(tmp_path, problem):
    browser = LoopBrowser(listing((ITEM_A,)))
    selected = {**task(ITEM_A), "scope": "item", "loop_id": "results"}
    if problem == "general_scope":
        selected["scope"] = "general"
    elif problem == "missing_ref":
        selected.pop("item_ref")
    elif problem == "missing_loop":
        selected.pop("loop_id")
    else:
        selected["loop_id"] = "invented-loop"

    def policy(payload):
        return reply("delegate", loops=[loop_update()], task=selected)

    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_invalid_response"
        assert final["metrics"]["jev_calls"] == 0
        assert final["items"] == {}
    assert browser.inputs == []
