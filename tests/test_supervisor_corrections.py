"""Offline coordinator regressions for repair, intermediate progress and task scope."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from test_agent import decision, page
from test_supervised_loops import ITEM_A, LIST_URL, LoopBrowser, links, listing
from test_supervisor_policy import ToolModel

from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from agentrun.supervisor_policy import SupervisorPolicy
from AgentBrowser import agent as jev
from AgentBrowser.browser import fingerprint
from AgentBrowser.model import ProviderError

GOAL = "Find a book and inspect its details."


class SearchBrowser:
    target = "supervisor-corrections"

    def __init__(self):
        self.page = page()
        self.inputs = []
        self.disconnect = Mock()
        self.close = Mock()

    def set_text(self, text):
        self.page["text"] = text
        self.page["fingerprint"] = fingerprint(self.page)

    def observe(self, screenshot=False):
        return deepcopy(self.page)

    def fresh(self, observed, action=None):
        return observed["fingerprint"] == self.page["fingerprint"]

    def act(self, action, observed, text=None):
        self.inputs.append(deepcopy(action))
        self.set_text("Search complete: the requested book is listed.")


@pytest.fixture(autouse=True)
def offline_worker(monkeypatch):
    monkeypatch.setattr(jev, "choose", lambda *_: decision("DONE"))
    monkeypatch.setattr(jev, "field_text", Mock(side_effect=AssertionError("No text model is needed")))


def requirement(key="goal", quote=GOAL):
    return {
        "id": key, "goal_quote": quote, "description": quote, "status": "unknown", "evidence_refs": [],
    }


def task(instruction="Open the search results"):
    return {
        "id": "search", "instruction": instruction,
        "success_criteria": ["Search results visibly contain the requested book."],
    }


def response(action="feedback", **updates):
    return {
        "action": action, "requirements": [requirement()], "question": "Continue after review?",
        **({"task": task()} if action == "delegate" else {}), **updates,
    }


def text_ref(payload):
    return next(fact["ref"] for fact in payload["evidence"]
                if fact["kind"] == "page" and "text" in fact and fact["ref"] in payload["current_evidence_refs"])


def choose_search(observed, *_):
    return decision("DONE" if observed["text"].startswith("Search complete") else "e3")


def test_invalid_decision_is_repaired_once_before_any_worker_input(tmp_path, monkeypatch):
    browser = SearchBrowser()
    seen = []
    monkeypatch.setattr(jev, "choose", choose_search)

    def policy(payload):
        seen.append(deepcopy(payload))
        if len(seen) == 1:
            return response("delegate", task=None, plan=["Invalid plan must not persist"])
        if len(seen) == 2:
            assert browser.inputs == []
            assert payload["requirements"] == [] and payload["plan"] == []
            assert payload["validation_error"]["reason"] == "Delegation needs a subtask"
            assert payload["validation_error"]["browser_action_executed"] is False
            assert payload["validation_error"]["rejected_decision"]["task"] is None
            return response("delegate")
        assert payload["validation_error"] is None
        return response()

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["metrics"]["supervisor_calls"] == 3
        assert final["metrics"]["actions"] == len(browser.inputs) == 1
        assert agent.state["validation_retries"] == 0
    assert seen[0]["validation_error"] is None


def test_invalid_decision_repeated_stops_after_two_calls_without_input(tmp_path):
    browser = SearchBrowser()
    seen = []

    def policy(payload):
        seen.append(deepcopy(payload))
        return response("delegate", task=None)

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_invalid_response"
        assert final["status"] == "unverified" and final["question"] == ""
        assert final["metrics"]["supervisor_calls"] == 2
        assert final["metrics"]["jev_calls"] == 0
        assert agent.store.attempts() == []
        assert agent.state["requirements"] == []
    assert seen[1]["validation_error"]["browser_action_executed"] is False
    assert browser.inputs == []


def test_real_policy_schema_error_receives_one_coordinator_repair(tmp_path):
    browser = SearchBrowser()
    corrected = {key: value for key, value in response().items() if key != "action"}
    corrected.update(
        feedback_reason="missing_user_information", missing_information="The requested book's title",
        blocked_requirement_ids=["goal"], user_intervention="Only the user can identify the requested book.",
    )
    model = ToolModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "delegate_to_jev", "args": {"requirements": [requirement()]},
            "id": "bad", "type": "tool_call",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "request_feedback", "args": corrected, "id": "corrected", "type": "tool_call",
        }]),
    ])
    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser,
                         supervisor=SupervisorPolicy(model=model)) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert model.calls == final["metrics"]["supervisor_calls"] == 2
        assert final["metrics"]["supervisor_http_attempts"] == 0
        assert final["metrics"]["jev_calls"] == 0
    assert browser.inputs == []


@pytest.mark.parametrize(("kind", "http_status"), [("authentication", 401), ("billing", 402)])
def test_provider_authentication_and_billing_failures_are_not_retried(tmp_path, kind, http_status):
    browser = SearchBrowser()
    provider = Mock(spec=[], side_effect=ProviderError(kind, status_code=http_status))
    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser,
                         supervisor=provider) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_provider_error"
        assert final["metrics"]["supervisor_calls"] == provider.call_count == 1
        assert final["metrics"]["jev_calls"] == 0
        assert agent.state["provider_error"]["provider_error_kind"] == kind
        assert not agent.state.get("validation_error")
    assert browser.inputs == []


@pytest.mark.parametrize("model_offset", [None, 999999])
def test_inspect_coordinator_visits_every_retained_fact_once_without_model_pagination(tmp_path, model_offset):
    browser = SearchBrowser()
    browser.page["actions"] = [{
        "id": f"book-{index}", "node": index, "kind": "click", "role": "button", "label": f"Book {index}",
    } for index in range(250)]
    browser.set_text("Many observed books")
    browser.observe = Mock(wraps=browser.observe)
    seen = set()
    offsets = []

    def policy(payload):
        refs = {fact["ref"] for fact in payload["evidence"]}
        assert not seen.intersection(refs), "Inspection must not resend a previously visited evidence slice"
        seen.update(refs)
        offsets.append(payload["evidence_offset"])
        if payload["next_evidence_offset"] is None:
            return response()
        optional = {} if model_offset is None else {"evidence_offset": model_offset}
        return response("inspect", **optional)

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        reads_before = browser.observe.call_count
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert seen == set(agent.state["evidence"])
        assert offsets == [0, 120, 240]
        assert browser.observe.call_count == reads_before
        assert final["metrics"]["jev_calls"] == final["metrics"]["actions"] == 0
    assert browser.inputs == []


def test_inspect_cannot_fabricate_another_slice_or_new_page_when_evidence_is_exhausted(tmp_path):
    browser = SearchBrowser()
    browser.observe = Mock(wraps=browser.observe)
    seen = []

    def policy(payload):
        seen.append(deepcopy(payload))
        assert payload["next_evidence_offset"] is None
        return response("inspect", evidence_offset=1000)

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        reads_before = browser.observe.call_count
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_invalid_response"
        assert final["metrics"]["supervisor_calls"] == 2
        assert agent.state["evidence_offset"] == 0
        assert browser.observe.call_count == reads_before
        assert agent.store.attempts() == []
    assert seen[1]["validation_error"]["reason"] == (
        "All retained evidence has been supplied; inspect cannot fetch new facts"
    )
    assert seen[1]["validation_error"]["browser_action_executed"] is False
    assert browser.inputs == []


@pytest.mark.parametrize("change", ["extend", "remove", "change_quote"])
def test_requirement_registry_can_grow_but_cannot_lose_or_change_existing_outcomes(tmp_path, change):
    browser = SearchBrowser()
    first = [requirement(), requirement("find", "Find a book")]
    changed = deepcopy(first)
    if change == "extend":
        changed.append(requirement("inspect", "inspect its details"))
    elif change == "remove":
        changed.pop()
    else:
        changed[1]["goal_quote"] = "inspect its details"
    calls = 0

    def policy(payload):
        nonlocal calls
        calls += 1
        return response("delegate" if calls == 1 else "feedback",
                        requirements=first if calls == 1 else changed)

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        if change == "extend":
            assert final["stop_reason"] == "supervisor_feedback"
            assert final["requirements"] == changed
            assert calls == 2
        else:
            assert final["stop_reason"] == "supervisor_invalid_response"
            assert final["requirements"] == first
            assert calls == 3
        assert browser.inputs == []


def test_verified_subtask_advances_with_global_requirement_unknown(tmp_path, monkeypatch):
    browser = SearchBrowser()
    monkeypatch.setattr(jev, "choose", choose_search)
    calls = 0
    delegation_id = None

    def policy(payload):
        nonlocal calls, delegation_id
        calls += 1
        if calls == 1:
            return response("delegate")
        if calls == 2:
            last = payload["last_result"]
            assert last["status"] == "claimed_done"
            assert last["started_observation"] < last["observation_seq"]
            delegation_id = last["delegation_id"]
            return response(
                "delegate", task=task("Inspect the listed book's details"),
                subtask_review={
                    "delegation_id": delegation_id, "status": "satisfied", "evidence_refs": [text_ref(payload)],
                },
            )
        return response()

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        starts = []
        original = agent.worker.start_subtask

        def start(*args, **kwargs):
            starts.append(kwargs["verified_progress"])
            return original(*args, **kwargs)

        monkeypatch.setattr(agent.worker, "start_subtask", start)
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["requirements"][0]["status"] == "unknown"
        assert agent.state["subtask_reviews"][delegation_id]["status"] == "satisfied"
        assert agent.state["progress_revision"] == 1
        assert agent.state["corrections"] == 0
        assert starts == [False, True]
        assert len(browser.inputs) == 1


@pytest.mark.parametrize("invalid", ["wrong_delegation", "missing_refs", "old_postcondition"])
def test_subtask_review_rejects_wrong_identity_missing_or_old_evidence(tmp_path, monkeypatch, invalid):
    browser = SearchBrowser()
    monkeypatch.setattr(jev, "choose", choose_search)
    calls = 0
    old_ref = None

    def policy(payload):
        nonlocal calls, old_ref
        calls += 1
        if calls == 1:
            old_ref = text_ref(payload)
            # The previous snapshot disappears before work starts. Its fact
            # must not be refreshed by the worker's initial observation.
            browser.set_text("Search page has refreshed.")
            return response("delegate")
        refs = [] if invalid == "missing_refs" else [old_ref if invalid == "old_postcondition" else text_ref(payload)]
        return response(subtask_review={
            "delegation_id": "different-delegation" if invalid == "wrong_delegation"
            else payload["last_result"]["delegation_id"],
            "status": "satisfied", "evidence_refs": refs,
        })

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_invalid_response"
        assert calls == 3
        assert agent.state["progress_revision"] == 0
        assert agent.state.get("subtask_reviews", {}) == {}
        assert len(browser.inputs) == 1


def test_claimed_done_without_positive_review_does_not_reset_corrections(tmp_path):
    browser = SearchBrowser()
    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser,
                         supervisor=lambda _: response("delegate")) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "no_progress"
        assert agent.state["corrections"] == 2
        assert agent.state["progress_revision"] == 0
        assert final["metrics"]["supervisor_calls"] == 4
        assert final["metrics"]["jev_calls"] == 3
    assert browser.inputs == []


def test_jev_receives_only_current_task_criteria_and_observed_item_url(tmp_path, monkeypatch):
    goal = "Inspect one result. GLOBAL_ONLY_CONTEXT: report the full task after all work."
    browser = LoopBrowser(listing((ITEM_A,)))
    captured = []
    instruction = "Inspect the requested result without sending any message."
    criteria = "The result's title and description are visible."

    def policy(payload):
        common = {"requirements": [requirement(quote=goal)]}
        if payload["last_result"]:
            return response(**common)
        ref = links(payload)[0]["ref"]
        return response("delegate", **common, loops=[{
            "id": "result", "goal_quote": "Inspect one result", "description": "One observed result",
            "item_refs": [ref],
        }], task={
            "id": "inspect", "instruction": instruction, "success_criteria": [criteria], "item_ref": ref,
        })

    def choose(observed, task_goal, history):
        captured.append(task_goal)
        return decision("DONE")

    monkeypatch.setattr(jev, "choose", choose)
    with SupervisedAgent(LIST_URL, goal, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
    assert len(captured) == 1
    assert "CURRENT SUBTASK:\n" + instruction in captured[0]
    assert "SUCCESS CRITERIA:\n" + criteria in captured[0]
    assert "OBSERVED ITEM URL:\n" + ITEM_A in captured[0]
    assert "GLOBAL_ONLY_CONTEXT" not in captured[0]
    assert browser.inputs == []


@pytest.mark.parametrize("action", ["delegate", "finish"])
def test_partial_goal_cannot_delegate_or_finish_until_repair_adds_the_missing_outcome(tmp_path, action):
    goal = "Accept cookies if present. Find the requested book."
    browser = SearchBrowser()
    browser.set_text("Cookies accepted. The requested book is visible.")
    calls = []

    def policy(payload):
        calls.append(deepcopy(payload))
        refs = [text_ref(payload)] if action == "finish" else []
        requirements = [requirement("consent", "Accept cookies if present")]
        if len(calls) >= 2:
            requirements.append(requirement("book", "Find the requested book"))
        if action == "finish":
            for item in requirements:
                item.update(status="satisfied", evidence_refs=refs)
        if len(calls) == 2:
            assert payload["validation_error"]["browser_action_executed"] is False
            assert "original goal" in payload["validation_error"]["reason"].lower()
            assert payload["requirements"] == []
            assert browser.inputs == []
        return response("feedback" if len(calls) == 3 else action,
                        requirements=requirements, evidence_refs=refs)

    with SupervisedAgent(page()["url"], goal, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert [item["id"] for item in final["requirements"]] == ["consent", "book"]
        if action == "finish":
            assert final["status"] == "done"
            assert final["metrics"]["supervisor_calls"] == 2
            assert final["metrics"]["jev_calls"] == 0
        else:
            assert final["stop_reason"] == "supervisor_feedback"
            assert final["metrics"]["supervisor_calls"] == 3
            assert final["metrics"]["jev_calls"] == 1
    assert browser.inputs == []


@pytest.mark.parametrize("action", ["delegate", "finish"])
def test_persistently_omitted_goal_outcome_is_rejected_before_worker_input(tmp_path, action):
    goal = "Accept cookies if present. Find the requested book."
    browser = SearchBrowser()

    def policy(payload):
        ref = text_ref(payload)
        first = {**requirement("consent", "Accept cookies if present"),
                 "status": "satisfied", "evidence_refs": [ref]}
        return response(action, requirements=[first], evidence_refs=[ref])

    with SupervisedAgent(page()["url"], goal, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_invalid_response"
        assert final["metrics"]["supervisor_calls"] == 2
        assert final["metrics"]["jev_calls"] == final["metrics"]["actions"] == 0
        assert final["requirements"] == []
        assert final["verification"]["status"] != "satisfied"
    assert browser.inputs == []


@pytest.mark.parametrize("quotes", [
    ["return to the list", "inspect its details", "Find a book"],
    ["inspect its details; return to the list", "Find a book"],
])
def test_goal_coverage_accepts_unordered_and_compound_quotes_ignoring_punctuation(tmp_path, quotes):
    goal = "Find a book,\n inspect its details; return to the list."
    browser = SearchBrowser()
    requirements = [requirement(str(index), quote) for index, quote in enumerate(quotes)]
    calls = []

    def policy(payload):
        calls.append(deepcopy(payload))
        return response("delegate" if len(calls) == 1 else "feedback", requirements=requirements)

    with SupervisedAgent(page()["url"], goal, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["metrics"]["jev_calls"] == 1
        assert final["metrics"]["supervisor_calls"] == 2
    assert calls[1]["goal_coverage"]["complete"] is True
    assert calls[1]["goal_coverage"]["uncovered_words"] == []


def test_feedback_may_request_clarification_before_requirements_cover_the_entire_goal(tmp_path):
    goal = "Find a book. Contact its author."
    browser = SearchBrowser()
    policy = Mock(spec=[], return_value=response(
        requirements=[requirement("find", "Find a book")], question="Which book should I find?",
    ))
    with SupervisedAgent(page()["url"], goal, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["question"] == "Which book should I find?"
        assert policy.call_count == 1
        assert final["metrics"]["jev_calls"] == 0
    assert browser.inputs == []


def test_verified_subtask_memory_retains_criteria_and_refs_after_a_different_delegation(tmp_path, monkeypatch):
    browser = SearchBrowser()
    monkeypatch.setattr(jev, "choose", choose_search)
    calls = []
    first_review = None
    first_task = None

    def policy(payload):
        nonlocal first_review, first_task
        calls.append(deepcopy(payload))
        if len(calls) == 1:
            return response("delegate")
        if len(calls) == 2:
            first_task = deepcopy(payload["last_result"]["task"])
            first_review = {
                "delegation_id": payload["last_result"]["delegation_id"], "status": "satisfied",
                "evidence_refs": [text_ref(payload)],
            }
            return response("delegate", subtask_review=first_review, task=task("Inspect the book's details"))
        saved = payload["subtask_reviews"][first_review["delegation_id"]]
        assert saved == {**first_review, "task": first_task}
        assert saved["task"]["success_criteria"] == task()["success_criteria"]
        assert payload["last_result"]["delegation_id"] != first_review["delegation_id"]
        assert payload["last_result"]["task"]["instruction"] == "Inspect the book's details"
        assert payload["last_result"]["history"] == []
        return response()

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["metrics"]["actions"] == len(browser.inputs) == 1
        assert final["requirements"][0]["status"] == "unknown"
    assert len(calls) == 3


def test_repair_receives_rejected_next_task_and_preserves_last_executed_result(tmp_path, monkeypatch):
    browser = SearchBrowser()
    monkeypatch.setattr(jev, "choose", choose_search)
    calls = []
    last_executed = None
    rejected_task = {**task("Open a new item"), "item_ref": "unobserved-item"}

    def policy(payload):
        nonlocal last_executed
        calls.append(deepcopy(payload))
        if len(calls) == 1:
            return response("delegate")
        if len(calls) == 2:
            last_executed = deepcopy(payload["last_result"])
            return response("delegate", task=rejected_task)
        error = payload["validation_error"]
        assert error["rejected_decision"]["action"] == "delegate"
        assert error["rejected_decision"]["task"].items() >= rejected_task.items()
        assert error["browser_action_executed"] is False
        assert error["last_executed_delegation_id"] == last_executed["delegation_id"]
        assert payload["last_result"] == last_executed
        assert len(browser.inputs) == 1
        return response()

    with SupervisedAgent(page()["url"], GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["metrics"]["actions"] == 1
        assert final["metrics"]["supervisor_calls"] == 3
