"""Functional offline loop supervision over a changing observed browser."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import decision

from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from AgentBrowser import agent as jev
from AgentBrowser.browser import fingerprint

GOAL = "For each result, open its page and inspect its details."
LIST_URL = "https://example.test/results"
ITEM_A = "https://example.test/items/a"
ITEM_B = "https://example.test/items/b"


def listing(urls=(ITEM_A, ITEM_B), *, truncated=False):
    actions = [{
        "id": f"item-{index}", "node": index, "kind": "click", "role": "link",
        "label": f"Item {index}", "href": url, **({"href_truncated": True} if truncated else {}),
    } for index, url in enumerate(urls, 1)]
    page = {
        "url": LIST_URL, "title": "Search results", "text": f"{len(urls)} results. End of results.",
        "scroll": {"y": 0}, "actions": actions,
    }
    page["fingerprint"] = fingerprint(page)
    return page


class LoopBrowser:
    target = "observed-loop-tab"

    def __init__(self, initial=None):
        self.initial = initial or listing()
        self.page = deepcopy(self.initial)
        self.inputs = []
        self.disconnect = Mock()
        self.close = Mock()

    def fresh(self, page, action=None):
        return page["fingerprint"] == self.page["fingerprint"]

    def observe(self, screenshot=False):
        return deepcopy(self.page)

    def act(self, action, page, text=None):
        self.inputs.append(deepcopy(action))
        if action["kind"] == "back":
            self.page = deepcopy(self.initial)
        else:
            self.page = {
                "url": action["href"], "title": action["label"],
                "text": f"Details for {action['label']}. Description is visible.",
                "scroll": {"y": 0},
                "actions": [{"id": "back", "kind": "back", "label": "Back to results", "href": LIST_URL}],
            }
            self.page["fingerprint"] = fingerprint(self.page)


def requirement(status="unsatisfied", refs=()):
    return {
        "id": "inspect_all", "goal_quote": GOAL, "description": "Every result inspected",
        "status": status, "evidence_refs": list(refs),
    }


def loop_update(refs=(), *, exhausted=False, evidence=()):
    return {
        "id": "results", "goal_quote": "For each result",
        "description": "All result detail pages", "item_refs": list(refs),
        "exhausted": exhausted, "evidence_refs": list(evidence),
    }


def reply(action="feedback", **kwargs):
    return {"action": action, "requirements": [requirement()], "question": "Await user input", **kwargs}


def task(ref, instruction="Inspect the observed result"):
    return {
        "id": "inspect", "instruction": instruction,
        "success_criteria": ["Its own detail page and description are observed."], "item_ref": ref,
    }


def links(payload):
    return [fact for fact in payload["evidence"] if fact.get("href") and fact.get("role") == "link"]


def page_fact(payload, url=None):
    return next(fact["ref"] for fact in payload["evidence"]
                if fact.get("kind") == "page" and "text" in fact
                and (url is None or fact.get("observed_url") == url))


def completion(item_id, refs):
    return {"item_id": item_id, "status": "completed", "evidence_refs": list(refs)}


def run(tmp_path, policy, *, initial=None):
    browser = LoopBrowser(initial)
    agent = SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy)
    return agent, browser


def test_repeated_observed_links_register_one_item_per_exact_url(tmp_path):
    def policy(payload):
        return reply(loops=[loop_update([fact["ref"] for fact in links(payload)])])

    with run(tmp_path, policy, initial=listing((ITEM_A, ITEM_A, ITEM_B)))[0] as agent:
        result = list(agent.run())[-1]
        assert sorted(item["url"] for item in result["items"].values()) == [ITEM_A, ITEM_B]
        assert len(result["items"]) == 2
        assert result["metrics"]["actions"] == 0


def test_unseen_reference_cannot_register_an_item_outside_evidence_slice(tmp_path):
    initial = listing(tuple(f"https://example.test/items/{index}" for index in range(150)))
    # The node exists in the full extraction, but was not sent in the initial 120-fact slice.
    agent, browser = run(tmp_path, lambda _: reply(), initial=initial)
    with agent:
        hidden = next(fact["ref"] for fact in agent.state["evidence"].values() if fact.get("node") == 150)
        agent.supervisor = lambda payload: reply(loops=[loop_update([hidden])])
        result = list(agent.run())[-1]
        assert result["status"] == "unverified"
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert result["items"] == {}
        assert browser.inputs == []


def test_inspect_allows_registering_an_item_from_later_evidence_slice(tmp_path):
    initial = listing(tuple(f"https://example.test/items/{index}" for index in range(150)))
    calls = []

    def policy(payload):
        calls.append(payload["evidence_offset"])
        last = next((fact for fact in links(payload) if fact["href"].endswith("/149")), None)
        if last is None:
            return reply("inspect", evidence_offset=payload["next_evidence_offset"])
        return reply(loops=[loop_update([last["ref"]])])

    with run(tmp_path, policy, initial=initial)[0] as agent:
        result = list(agent.run())[-1]
        assert calls == [0, 120]
        assert [item["url"] for item in result["items"].values()] == ["https://example.test/items/149"]
        assert result["metrics"]["actions"] == 0


@pytest.mark.parametrize("identity", ["truncated", "invented", "text_only"])
def test_unknown_item_identity_pauses_before_browser_input(tmp_path, identity):
    def policy(payload):
        if identity == "invented":
            ref = "https://invented.test/items/1"
        elif identity == "text_only":
            ref = page_fact(payload)
        else:
            ref = links(payload)[0]["ref"]
        return reply("delegate", loops=[loop_update([ref])], task=task(ref))

    agent, browser = run(tmp_path, policy, initial=listing(truncated=identity == "truncated"))
    with agent:
        result = list(agent.run())[-1]
        assert result["status"] == "unverified"
        assert result["items"] == {}
        assert browser.inputs == []


@pytest.mark.parametrize("downgrade", [False, True])
def test_completed_item_cannot_be_delegated_again_even_with_downgrade(tmp_path, monkeypatch, downgrade):
    calls = 0

    def policy(payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            ref = links(payload)[0]["ref"]
            return reply("delegate", loops=[loop_update([ref])], task=task(ref))
        item_id = next(iter(payload["items"]))
        if calls == 2:
            return reply("feedback", item_reviews=[completion(item_id, [page_fact(payload, ITEM_A)])])
        reviews = [{"item_id": item_id, "status": "pending", "evidence_refs": []}] if downgrade else []
        return reply("delegate", item_reviews=reviews, task=task(item_id, "Inspect the same result again"))

    monkeypatch.setattr(jev, "choose", lambda page, *_: decision(
        "item-1" if page["url"] == LIST_URL else "DONE",
    ))
    agent, browser = run(tmp_path, policy)
    with agent:
        result = list(agent.run())[-1]
        assert [item["status"] for item in result["items"].values()] == ["completed"]
        run_id = agent.run_id
    with SupervisedAgent.resume(run_id, state_dir=tmp_path, browser=browser,
                                supervisor=policy, feedback="Continue with the next result") as resumed:
        result = list(resumed.run())[-1]
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert len(browser.inputs) == 1
        assert next(iter(result["items"].values()))["status"] == "completed"


def test_worker_done_on_listing_cannot_complete_an_unvisited_item(tmp_path, monkeypatch):
    def policy(payload):
        if not payload["items"]:
            ref = links(payload)[0]["ref"]
            return reply("delegate", loops=[loop_update([ref])], task=task(ref))
        item_id = next(iter(payload["items"]))
        return reply(
            "finish", requirements=[requirement("satisfied", [page_fact(payload)])],
            loops=[loop_update(exhausted=True, evidence=[page_fact(payload)])],
            item_reviews=[completion(item_id, [page_fact(payload)])], evidence_refs=[page_fact(payload)],
        )

    monkeypatch.setattr(jev, "choose", lambda *_: decision("DONE"))
    agent, browser = run(tmp_path, policy)
    with agent:
        result = list(agent.run())[-1]
        assert result["status"] == "unverified"
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert next(iter(result["items"].values()))["status"] != "completed"
        assert browser.inputs == []


@pytest.mark.parametrize("pending", ["requirement", "enumeration", "item"])
def test_finish_requires_requirements_loop_coverage_and_all_items(tmp_path, pending):
    def policy(payload):
        ref = page_fact(payload)
        return reply(
            "finish", requirements=[requirement("unsatisfied" if pending == "requirement" else "satisfied",
                                                [] if pending == "requirement" else [ref])],
            loops=[loop_update([links(payload)[0]["ref"]] if pending == "item" else [],
                               exhausted=pending != "enumeration", evidence=[ref])],
            evidence_refs=[ref],
        )

    with run(tmp_path, policy)[0] as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "unverified"
        assert result["verification"]["status"] != "satisfied"
        assert result["metrics"]["actions"] == 0


def test_verified_item_progress_is_the_only_delegation_cycle_reset(tmp_path, monkeypatch):
    def policy(payload):
        if not payload["items"]:
            observed = links(payload)
            return reply("delegate", loops=[loop_update([fact["ref"] for fact in observed])],
                         task=task(observed[0]["ref"], "Inspect item A"))
        items = {item["url"]: item for item in payload["items"].values()}
        if items[ITEM_A]["status"] != "completed":
            return reply("delegate", item_reviews=[
                completion(items[ITEM_A]["item_id"], [page_fact(payload, ITEM_A)]),
            ], task=task(items[ITEM_B]["item_id"], "Inspect item B"))
        return reply("finish", requirements=[requirement("satisfied", [page_fact(payload, ITEM_B)])],
                     loops=[loop_update(exhausted=True, evidence=[page_fact(payload, LIST_URL)])],
                     item_reviews=[completion(items[ITEM_B]["item_id"], [page_fact(payload, ITEM_B)])],
                     evidence_refs=[page_fact(payload, ITEM_B)])

    def choose(page, goal, _history):
        desired = ITEM_B if "CURRENT SUBTASK:\nInspect item B" in goal else ITEM_A
        if page["url"] == desired:
            return decision("DONE")
        if page["url"] != LIST_URL:
            return decision("back")
        action = next(action for action in page["actions"] if action.get("href") == desired)
        return decision(action["id"])

    monkeypatch.setattr(jev, "choose", choose)
    agent, browser = run(tmp_path, policy)
    with agent:
        starts = []
        original = agent.worker.start_subtask

        def start(*args, **kwargs):
            before = list(agent.worker._semantic_visits)
            result = original(*args, **kwargs)
            starts.append((kwargs["verified_progress"], before, list(agent.worker._semantic_visits)))
            return result

        agent.worker._semantic_visits = ["earlier-state"] * 4
        monkeypatch.setattr(agent.worker, "start_subtask", start)
        result = list(agent.run())[-1]
        assert result["status"] == "done"
        assert len(browser.inputs) == 3
        assert [item["status"] for item in result["items"].values()] == ["completed", "completed"]
        assert starts[0] == (False, ["earlier-state"] * 4, ["earlier-state"] * 4)
        assert starts[1][0] is True and starts[1][1] and starts[1][2] == []


def test_item_review_cannot_use_newer_evidence_from_a_different_destination(tmp_path, monkeypatch):
    def policy(payload):
        if not payload["items"]:
            ref = links(payload)[0]["ref"]
            return reply("delegate", loops=[loop_update([ref])], task=task(ref))
        item_id = next(iter(payload["items"]))
        return reply("feedback", item_reviews=[completion(item_id, [page_fact(payload, ITEM_B)])])

    # Jev mistakenly opens B although A was delegated. Its observed success does
    # not prove the requested item A was processed.
    monkeypatch.setattr(jev, "choose", lambda page, *_: decision(
        "item-2" if page["url"] == LIST_URL else "DONE",
    ))
    agent, browser = run(tmp_path, policy)
    with agent:
        result = list(agent.run())[-1]
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert next(iter(result["items"].values()))["status"] != "completed"
        assert browser.inputs[0]["href"] == ITEM_B


def test_confirmed_redirect_cannot_reprocess_a_previously_registered_alias(tmp_path, monkeypatch):
    class RedirectBrowser(LoopBrowser):
        def act(self, action, page, text=None):
            super().act(action, page, text)
            if action.get("href") == ITEM_A:
                self.page["url"] = ITEM_B
                self.page["fingerprint"] = fingerprint(self.page)

    calls, alias_id = 0, None

    def policy(payload):
        nonlocal calls, alias_id
        calls += 1
        if calls == 1:
            observed = links(payload)
            return reply("delegate", loops=[loop_update([fact["ref"] for fact in observed])],
                         task=task(observed[0]["ref"]))
        if calls == 2:
            items = {item["url"]: item for item in payload["items"].values()}
            alias_id = items[ITEM_B]["item_id"]
            destination = next(fact["ref"] for fact in payload["evidence"] if fact.get("url") == ITEM_B)
            reviewed = completion(items[ITEM_A]["item_id"], [page_fact(payload, ITEM_B)])
            reviewed["destination_ref"] = destination
            return reply("feedback", item_reviews=[reviewed])
        return reply("delegate", task=task(alias_id, "Inspect the next result"))

    monkeypatch.setattr(jev, "choose", lambda page, *_: decision(
        "item-1" if page["url"] == LIST_URL else "DONE",
    ))
    browser = RedirectBrowser()
    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        result = list(agent.run())[-1]
        assert result["stop_reason"] == "supervisor_feedback"
        run_id = agent.run_id
    with SupervisedAgent.resume(run_id, state_dir=tmp_path, browser=browser,
                                supervisor=policy, feedback="Continue with the next result") as resumed:
        result = list(resumed.run())[-1]
        assert result["stop_reason"] == "supervisor_invalid_response"
        assert len(browser.inputs) == 1
