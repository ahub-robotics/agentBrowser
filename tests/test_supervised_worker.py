"""Delegated Jev work keeps one browser and durable, bounded side-effect boundaries."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import decision, page

from AgentBrowser import agent as loop
from AgentBrowser.browser import MutationUncertain, StalePage
from AgentBrowser.candidates import CandidateLimits


def worker(**kwargs):
    browser = Mock(fresh=Mock(return_value=True), observe=Mock(side_effect=lambda **_: deepcopy(page())))
    return loop.Agent("https://unused.test", "Original task", browser=browser, verification=False, **kwargs)


def test_injected_browser_is_only_session_and_subtasks_keep_global_progress(monkeypatch):
    constructor = Mock(side_effect=AssertionError("Must not create a new tab"))
    monkeypatch.setattr(loop, "Browser", constructor)
    agent = worker()
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[decision("wait"), decision("DONE")]))
    list(agent.run())
    history = deepcopy(agent.state["history"])
    started = agent.state["started_at"]
    decisions = agent.state["model_calls"]
    windows = agent.state["candidate_windows"]
    agent.pending_text = ("old input", "old text", {})
    agent._review_authorization = "old authorization"
    agent._semantic_visits = ["old state"]
    agent.start_subtask("Second task", progress_feedback={"work_item": {"url": "https://item.test/1"}})
    assert agent.state["goal"] == "Second task"
    assert agent.state["status"] == "ready"
    assert agent.state["history"] == history
    assert agent.state["started_at"] == started
    assert agent.state["model_calls"] == decisions
    assert agent.state["candidate_windows"] == windows
    assert agent.pending_text is agent._review_authorization is None
    assert agent._semantic_visits == ["old state"]
    assert agent.state["page"]["progress_feedback"]["work_item"]["url"] == "https://item.test/1"
    constructor.assert_not_called()


def test_bounded_delegation_yields_without_restarting_global_budgets(monkeypatch):
    agent = worker()
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("wait")))
    states = list(agent.run_subtask("Inspect a section", action_limit=2, decision_limit=8))
    assert [state["status"] for state in states] == ["ready", "ready", "yielded"]
    assert states[-1]["stop_reason"] == "subtask_action_limit"
    assert agent.browser.act.call_count == 2
    assert agent.state["model_calls"] == 2
    states = list(agent.run_subtask("Continue inspecting", action_limit=8, decision_limit=1))
    assert states[-1]["status"] == "yielded"
    assert states[-1]["stop_reason"] == "subtask_decision_limit"
    assert agent.browser.act.call_count == 3
    assert agent.state["model_calls"] == 3


def test_configurable_global_action_limit_survives_delegation(monkeypatch):
    agent = worker(max_actions=1)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("wait")))
    first = list(agent.run_subtask("First", action_limit=1))
    assert first[-1]["status"] == "yielded"
    second = list(agent.run_subtask("Second"))
    assert second[-1]["status"] == "blocked"
    assert second[-1]["stop_reason"] == "action_budget"
    assert agent.browser.act.call_count == 1


def test_configurable_query_budget_is_shared_by_subtasks(monkeypatch):
    agent = worker(candidate_limits=CandidateLimits(max_queries=1))
    chooser = Mock(return_value=decision("DONE"))
    monkeypatch.setattr(loop, "choose", chooser)
    assert list(agent.run_subtask("First"))[-1]["status"] == "done"
    assert list(agent.run_subtask("Second"))[-1]["stop_reason"] == "decision_budget"
    chooser.assert_called_once()


def test_only_verified_progress_resets_stall_detection():
    agent = worker()
    agent._semantic_visits = ["repeated"] * 4
    agent.state["history"] = [{"page_changed": False, "kind": "click"}] * 3
    agent.start_subtask("Same work renamed")
    assert agent._semantic_visits == ["repeated"] * 4
    agent.start_subtask("Next verified item", verified_progress=True)
    assert agent._semantic_visits == []
    assert agent.state["verified_progress_step"] == 3


@pytest.mark.parametrize("stale_after_input", [False, True])
def test_stale_recovery_keeps_delegated_context_for_policy_and_text_helper(monkeypatch, stale_after_input):
    agent = worker()
    context = {
        "requirements": [{"id": "message", "status": "unsatisfied"}],
        "active_item": {"url": "https://example.test/item/2"},
        "completed_items": ["https://example.test/item/1"],
    }
    agent.start_subtask("Fill the message on the active item", progress_feedback=context)
    after = page()
    after.update(url=context["active_item"]["url"], fingerprint="item-detail")
    if stale_after_input:
        agent.browser.observe.side_effect = [StalePage("Navigation interrupted the read"), deepcopy(after)]
    else:
        agent.browser.act.side_effect = [StalePage("Changed before input"), None]
        agent.browser.observe.side_effect = lambda **_: deepcopy(after)
    chooser = Mock(side_effect=[
        {**decision("e3"), "operation": "CLICK", "target": "2"}, decision("e1"),
    ])
    helper = Mock(return_value=("Requested message", {"model": "offline", "latency_ms": 0}))
    monkeypatch.setattr(loop, "choose", chooser)
    monkeypatch.setattr(loop, "field_text", helper)

    recovered = agent.command("tick")
    feedback = recovered["progress_feedback"]
    assert {key: feedback[key] for key in context} == context
    assert feedback["recovery"]["action_was_executed"] is stale_after_input
    assert len(recovered["history"]) == int(stale_after_input)
    agent.browser.observe.side_effect = lambda **_: deepcopy(after)
    agent.command("tick")

    submitted = chooser.call_args.args[0]["_candidate_request"]["state"]["progress_feedback"]
    assert submitted == feedback
    assert helper.call_args.args[0]["progress_feedback"] == feedback
    # A failed observation never repeats the earlier click; only the next fill executes.
    assert [call.args[0]["kind"] for call in agent.browser.act.call_args_list] == ["click", "fill"]


def test_repeated_stale_recovery_replaces_transient_feedback_and_new_subtask_replaces_context(monkeypatch):
    agent = worker()
    first = {
        "requirements": [{"id": "first", "status": "unknown"}],
        "active_item": {"url": "https://example.test/item/1"}, "completed_items": [],
    }
    agent.start_subtask("First item", progress_feedback=first)
    first["active_item"]["url"] = "https://example.test/caller-mutated"
    agent.browser.act.side_effect = StalePage("Changed before input")
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[
        {**decision("e3"), "operation": "CLICK", "target": "2"},
        {**decision("e2"), "operation": "CLICK", "target": "1"},
    ]))
    agent.command("tick")
    agent.state["progress_feedback"]["old_transient"] = "Do not retain this recovery"
    second_recovery = agent.command("tick")["progress_feedback"]
    assert second_recovery["active_item"]["url"] == "https://example.test/item/1"
    assert second_recovery["recovery"]["proposed_action"]["label"] == "Open Search"
    assert "old_transient" not in second_recovery
    assert "recovery" not in second_recovery["recovery"]

    next_context = {
        "requirements": [{"id": "second", "status": "unknown"}],
        "active_item": {"url": "https://example.test/item/2"},
        "completed_items": ["https://example.test/item/1"],
    }
    agent.start_subtask("Second item", progress_feedback=next_context)
    assert agent.state["progress_feedback"] == next_context
    assert agent.state["page"]["progress_feedback"] == next_context


def test_hooks_write_attempt_before_input_and_dispatch_before_snapshot(monkeypatch):
    events = []
    hooks = Mock()
    hooks.before_action.side_effect = lambda *_: events.append("attempt") or "attempt-id"
    hooks.dispatched.side_effect = lambda *_: events.append("dispatched")
    hooks.observed.side_effect = lambda *_: events.append("observed")
    agent = worker(execution_hooks=hooks, observer=lambda _: events.append("observer"))
    agent.browser.act.side_effect = lambda *_, **__: events.append("input")
    agent.browser.observe.side_effect = lambda **_: events.append("snapshot") or deepcopy(page())
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    events.clear()
    agent.command("tick")
    assert events == ["attempt", "input", "dispatched", "snapshot", "observer", "observed"]
    assert hooks.dispatched.call_args.args[0] == "attempt-id"
    hooks.rejected.assert_not_called()
    assert hooks.observed.call_args.args[2]["page_changed"] is False


def test_attempt_is_rejected_only_for_provably_stale_pre_input(monkeypatch):
    hooks = Mock()
    agent = worker(execution_hooks=hooks)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    agent.browser.act.side_effect = StalePage("Changed before input")
    agent.command("tick")
    hooks.rejected.assert_called_once_with(hooks.before_action.return_value)
    hooks.dispatched.assert_not_called()
    hooks.observed.assert_not_called()
    assert agent.state["history"] == []


@pytest.mark.parametrize("error", [MutationUncertain("Lost acknowledgement"), RuntimeError("Connection lost")])
def test_uncertain_input_is_never_rejected_or_replayed(monkeypatch, error):
    hooks = Mock()
    agent = worker(execution_hooks=hooks)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    agent.browser.act.side_effect = error
    with pytest.raises(type(error)):
        list(agent.run_subtask("Click an item"))
    hooks.before_action.assert_called_once()
    hooks.rejected.assert_not_called()
    hooks.dispatched.assert_not_called()
    agent.browser.act.assert_called_once()
    assert agent.state["decision"] is None


def test_failed_post_input_observation_preserves_dispatch_record(monkeypatch):
    hooks = Mock()
    agent = worker(execution_hooks=hooks)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    agent.browser.observe.side_effect = RuntimeError("Read unavailable")
    with pytest.raises(RuntimeError, match="Read unavailable"):
        agent.command("tick")
    hooks.dispatched.assert_called_once()
    hooks.rejected.assert_not_called()
    hooks.observed.assert_not_called()
    assert len(agent.state["history"]) == 1


def test_model_and_text_attempt_counters_are_durable_before_provider(monkeypatch):
    counts = []
    hooks = Mock()
    agent = worker(execution_hooks=hooks)
    hooks.before_decision.side_effect = lambda: counts.append(("decision", agent.state["model_calls"]))
    hooks.before_text.side_effect = lambda: counts.append(("text", agent.state["text_model_calls"]))
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e1")))
    monkeypatch.setattr(loop, "field_text", Mock(side_effect=RuntimeError("Provider interrupted")))
    with pytest.raises(RuntimeError, match="Provider interrupted"):
        agent.command("tick")
    assert counts == [("decision", 1), ("text", 1)]
    hooks.after_decision.assert_called_once()
    hooks.after_text.assert_called_once()
    agent.browser.act.assert_not_called()
    assert agent.state["text_calls"] == []


def test_observed_links_reach_element_and_selected_target_heads():
    from AgentBrowser.model import prepare_candidates

    observed = page()
    observed["actions"][2].update(href="https://example.test/item/1", href_truncated=True)
    prepared = prepare_candidates(observed, "Open next unseen item", [])
    body = prepared["_candidate_request"]
    element = body["state"]["elements"][1]
    target = body["questions"]["click_target"]["criteria"]["2"]
    assert element["href"] == target["href"] == "https://example.test/item/1"
    assert element["href_truncated"] is target["href_truncated"] is True


def test_live_worker_never_inherits_parent_graph_checkpointer(monkeypatch, tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    agent = worker()
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("DONE")))

    def delegate(_):
        return {"status": list(agent.run_subtask("Inspect this page"))[-1]["status"]}

    parent = StateGraph(dict)
    parent.add_node("delegate", delegate)
    parent.add_edge(START, "delegate")
    parent.add_edge("delegate", END)
    with SqliteSaver.from_conn_string(str(tmp_path / "parent.sqlite")) as saver:
        graph = parent.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "worker-isolation"}}
        assert graph.invoke({}, config=config, durability="sync") == {"status": "done"}
        assert graph.get_state(config).values == {"status": "done"}
    assert agent.graph.checkpointer is False
    agent.browser.act.assert_not_called()
