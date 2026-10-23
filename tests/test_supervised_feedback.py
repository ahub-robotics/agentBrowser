"""In-place feedback continues a paused run without replaying browser input."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import decision, page
from test_supervised_loops import (
    GOAL,
    ITEM_A,
    ITEM_B,
    LIST_URL,
    LoopBrowser,
    completion,
    links,
    loop_update,
    page_fact,
    reply,
    task,
)
from test_supervisor_corrections import GOAL as SEARCH_GOAL
from test_supervisor_corrections import SearchBrowser, response

from agentrun import SupervisorLimits
from agentrun import supervised as coordinator
from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from AgentBrowser import agent as jev


@pytest.fixture(autouse=True)
def offline_worker(monkeypatch):
    monkeypatch.setattr(jev, "choose", Mock(return_value=decision("DONE")))
    monkeypatch.setattr(jev, "field_text", Mock(side_effect=AssertionError("No text provider is needed")))


def test_feedback_refreshes_same_run_and_retains_completed_items_journal_and_budgets(tmp_path, monkeypatch):
    browser = LoopBrowser()
    browser.observe = Mock(wraps=browser.observe)
    payloads = []

    def policy(payload):
        payloads.append(deepcopy(payload))
        if not payload["items"]:
            refs = [fact["ref"] for fact in links(payload)]
            return reply("delegate", loops=[loop_update(refs)], task=task(refs[0]))
        remaining = next(item for item in payload["items"].values() if item["url"] == ITEM_B)
        if payload["feedback"] and remaining["status"] == "pending":
            return reply("delegate", task=task(remaining["item_id"], "Inspect item B"))
        url = ITEM_B if payload["feedback"] else ITEM_A
        completed = next(item for item in payload["items"].values() if item["url"] == url)
        return reply(item_reviews=[completion(completed["item_id"], [page_fact(payload, url)])])

    def choose(observed, goal, _history):
        if "Inspect item B" in goal:
            return decision("DONE" if observed["url"] == ITEM_B else
                            "item-2" if observed["url"] == LIST_URL else "back")
        return decision("item-1" if observed["url"] == LIST_URL else "DONE")

    chooser = Mock(side_effect=choose)
    monkeypatch.setattr(jev, "choose", chooser)
    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        paused = list(agent.run())[-1]
        assert paused["status"] == "waiting_feedback"
        assert any(item["status"] == "completed" for item in paused["items"].values())
        journal = agent.store.attempts()
        budgets = agent._metrics()
        elapsed = agent.state["elapsed_seconds"]
        items = deepcopy(agent.state["items"])
        observations = browser.observe.call_count
        model_calls = chooser.call_count
        agent.state.update(corrections=2, validation_retries=1,
                           validation_error={"reason": "Old rejected instruction"},
                           provider_error={"provider_error_kind": "invalid_response"})
        browser.page["title"] = "The user has reviewed the open item"

        ready = agent.submit_feedback("  Continue with the remaining item.  ")

        assert ready["status"] == "ready" and ready["run_id"] == paused["run_id"]
        assert agent.browser is browser and agent.state["target"] == browser.target
        assert ready["page"]["title"] == browser.page["title"]
        assert browser.observe.call_count == observations + 1
        assert len(payloads) == 2 and chooser.call_count == model_calls
        assert len(browser.inputs) == 1
        assert agent.state["feedback"] == ["Continue with the remaining item."]
        assert agent.state["corrections"] == agent.state["validation_retries"] == 0
        assert not agent.state.get("validation_error") and not agent.state.get("provider_error")
        assert agent._metrics() == budgets
        assert agent.state["elapsed_seconds"] == elapsed
        assert agent.state["items"] == items
        assert agent.store.attempts() == journal
        assert agent.store.load_state()["feedback"] == agent.state["feedback"]

        again = list(agent.run())[-1]
        assert again["status"] == "waiting_feedback"
        assert payloads[-1]["feedback"] == ["Continue with the remaining item."]
        assert len(payloads) == 4 and len(browser.inputs) == 3
        assert all(item["status"] == "completed" for item in again["items"].values())
        assert [action["id"] for action in browser.inputs] == ["item-1", "back", "item-2"]
        assert agent.store.attempts()[:1] == journal
    browser.close.assert_not_called()


def test_waiting_time_does_not_consume_budget_even_with_run_generator_suspended(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(coordinator.time, "perf_counter", lambda: now[0])
    browser = SearchBrowser()
    with SupervisedAgent(page()["url"], SEARCH_GOAL, state_dir=tmp_path, browser=browser,
                         supervisor=lambda _: response()) as agent:
        stream = agent.run()
        paused = next(stream)
        assert paused["status"] == "waiting_feedback"
        elapsed = agent.state["elapsed_seconds"]
        now[0] += 3600
        try:
            with pytest.raises(RuntimeError):
                agent.submit_feedback("Continue after an hour")
            assert agent.state["feedback"] == []
        finally:
            stream.close()
        assert agent.state["elapsed_seconds"] == elapsed

        ready = agent.submit_feedback("Continue after an hour")
        assert ready["status"] == "ready"
        assert agent.state["elapsed_seconds"] == elapsed
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert agent.state["elapsed_seconds"] == elapsed
    assert browser.inputs == []


@pytest.mark.parametrize("invalid", [None, "", "  ", "x" * 4001], ids=["none", "empty", "spaces", "too_long"])
def test_invalid_feedback_does_not_change_paused_state_or_observe(tmp_path, invalid):
    browser = SearchBrowser()
    browser.observe = Mock(wraps=browser.observe)
    with SupervisedAgent(page()["url"], SEARCH_GOAL, state_dir=tmp_path, browser=browser,
                         supervisor=lambda _: response()) as agent:
        list(agent.run())
        before = deepcopy(agent.state)
        observations = browser.observe.call_count
        with pytest.raises(ValueError):
            agent.submit_feedback(invalid)
        assert agent.state == before
        assert browser.observe.call_count == observations


def test_feedback_requires_paused_connected_run(tmp_path):
    browser = SearchBrowser()
    agent = SupervisedAgent(page()["url"], SEARCH_GOAL, state_dir=tmp_path, browser=browser,
                            supervisor=lambda _: response())
    with agent:
        with pytest.raises((ValueError, RuntimeError)):
            agent.submit_feedback("This run has not paused")
        list(agent.run())
    with pytest.raises((ValueError, RuntimeError)):
        agent.submit_feedback("The controller has disconnected")
    assert browser.inputs == []


def test_feedback_does_not_reset_exhausted_global_budget_or_call_models(tmp_path):
    browser = SearchBrowser()
    policy = Mock(spec=[], side_effect=lambda _: response())
    with SupervisedAgent(page()["url"], SEARCH_GOAL, state_dir=tmp_path, browser=browser,
                         limits=SupervisorLimits(max_supervisor_calls=1), supervisor=policy) as agent:
        first = list(agent.run())[-1]
        assert first["metrics"]["supervisor_calls"] == 1
        after = agent.submit_feedback("Continue anyway")
        assert after["status"] == "waiting_feedback"
        assert after["stop_reason"] == "supervisor_budget"
        assert after["metrics"] == first["metrics"]
        assert list(agent.run())[-1]["stop_reason"] == "supervisor_budget"
        assert policy.call_count == 1
    assert browser.inputs == []


def test_failed_feedback_observation_keeps_feedback_and_does_not_continue_from_old_page(tmp_path):
    browser = SearchBrowser()
    policy = Mock(spec=[], side_effect=lambda _: response())
    with SupervisedAgent(page()["url"], SEARCH_GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        list(agent.run())
        browser.observe = Mock(side_effect=RuntimeError("The original tab disappeared"))
        result = agent.submit_feedback("Continue with the chosen book")
        assert result["status"] == "browser_unavailable"
        assert agent.state["feedback"] == ["Continue with the chosen book"]
        assert agent.store.load_state()["feedback"] == agent.state["feedback"]
        assert list(agent.run())[-1]["status"] == "browser_unavailable"
        assert policy.call_count == 1
    assert browser.inputs == []


def test_feedback_does_not_clear_or_repeat_a_browser_mutation_with_uncertain_result(tmp_path, monkeypatch):
    browser = SearchBrowser()
    browser.act = Mock(side_effect=RuntimeError("Connection lost after possible click"))
    chooser = Mock(return_value=decision("e3"))
    monkeypatch.setattr(jev, "choose", chooser)
    with SupervisedAgent(page()["url"], SEARCH_GOAL, state_dir=tmp_path, browser=browser,
                         supervisor=lambda _: response("delegate")) as agent:
        first = list(agent.run())[-1]
        assert first["stop_reason"] == "uncertain_action"
        uncertain = agent.store.attempts(unresolved_only=True)
        assert len(uncertain) == 1
        ready = agent.submit_feedback("Continue from the observed state")
        assert ready["status"] == "ready"
        assert agent.store.attempts(unresolved_only=True) == uncertain
        after = list(agent.run())[-1]
        assert after["stop_reason"] == "uncertain_action"
        assert agent.store.attempts(unresolved_only=True) == uncertain
        assert after["metrics"]["actions"] == first["metrics"]["actions"] == 1
        assert browser.act.call_count == chooser.call_count == 1


def test_feedback_replaces_expired_observation_deadline_without_resetting_active_budget(tmp_path, monkeypatch):
    browser = SearchBrowser()
    with SupervisedAgent(page()["url"], SEARCH_GOAL, state_dir=tmp_path, browser=browser,
                         supervisor=lambda _: response()) as agent:
        list(agent.run())
        agent.state["elapsed_seconds"] = 17.5
        budgets = agent._metrics()
        browser.observation_deadline = 100.0
        monkeypatch.setattr(coordinator.time, "monotonic", lambda: 5000.0)
        original_observe = browser.observe

        def observe(**kwargs):
            assert browser.observation_deadline == pytest.approx(5000.0 + agent.limits.max_seconds - 17.5)
            assert agent.worker.state["started_at"] is None
            return original_observe(**kwargs)

        browser.observe = Mock(side_effect=observe)
        result = agent.submit_feedback("Continue after the long pause")
        assert result["status"] == "ready"
        browser.observe.assert_called_once_with(screenshot=False)
        assert agent.state["elapsed_seconds"] == 17.5
        assert agent._metrics() == budgets
        assert agent.worker.state["started_at"] is None
    assert browser.inputs == []
