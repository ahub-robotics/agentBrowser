"""Offline regressions for routing and side effects in the compiled LangGraph."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.graph.state import CompiledStateGraph
from test_agent import decision, page

from AgentBrowser import agent as loop
from AgentBrowser.browser import StalePage


@pytest.fixture
def agent(monkeypatch):
    browser = Mock(fresh=Mock(return_value=True), observe=Mock(return_value=page()))
    monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))
    with loop.Agent("https://example.test/", "Find a book", verification=False) as agent:
        yield agent


class Trace(BaseCallbackHandler):
    def __init__(self):
        self.calls = []

    def on_chain_start(self, serialized, inputs, *, name=None, tags=None, **kwargs):
        self.calls.append((name, tags))


def test_tick_runs_real_graph_and_langchain_callbacks(agent, monkeypatch):
    choose = Mock(return_value=decision("e3"))
    monkeypatch.setattr(loop, "choose", choose)
    trace = Trace()
    result = agent.command("tick", config={"callbacks": [trace], "tags": ["integration"]})
    assert isinstance(agent.graph, CompiledStateGraph)
    names = [name for name, _tags in trace.calls]
    assert [name for name in names if name in {"observe", "choose", "execute", "publish"}] == [
        "observe", "choose", "execute", "publish",
    ]
    assert any(
        name == "JevPolicy" and "integration" in tags for name, tags in trace.calls
    )
    choose.assert_called_once()
    agent.browser.act.assert_called_once()
    assert result["status"] == "ready" and len(result["history"]) == 1
    assert "browser" not in result


@pytest.mark.parametrize("terminal", ["DONE", "BLOCKED"])
def test_terminal_nodes_do_not_mutate_browser(agent, monkeypatch, terminal):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision(terminal)))
    states = list(agent.run())
    assert len(states) == 1 and states[0]["status"] == terminal.lower()
    agent.browser.act.assert_not_called()
    assert list(agent.run()) == []


def test_manual_prediction_pauses_and_requires_matching_fingerprint(agent, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    predicted = agent.command("predict")
    agent.browser.act.assert_not_called()
    assert predicted["status"] == "predicted"
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", {"fingerprint": "outdated"})
    assert agent.state["decision"] is not None
    agent.command("act", {"fingerprint": predicted["page"]["fingerprint"]})
    agent.browser.act.assert_called_once()
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", {"fingerprint": predicted["page"]["fingerprint"]})
    agent.browser.act.assert_called_once()


def test_navigation_before_prediction_recovers_without_model_call(agent, monkeypatch):
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    agent.browser.fresh.side_effect = StalePage("navigation")
    assert agent.command("tick")["status"] == "ready"
    assert agent.state["decision"] is None
    choose.assert_not_called()
    agent.browser.act.assert_not_called()


def test_post_action_recovery_keeps_action_and_does_not_repeat_it(agent, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[decision("e3"), decision("DONE")]))
    agent.browser.observe.side_effect = [StalePage("after input"), page()]
    states = list(agent.run())
    assert [s["status"] for s in states] == ["ready", "done"]
    assert len(agent.state["history"]) == 1
    assert agent.state["history"][0]["choice"] == "e3"
    agent.browser.act.assert_called_once()


def test_ambiguous_mutation_failure_is_not_retried(agent, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    agent.browser.act.side_effect = RuntimeError("Input acknowledgement lost")
    agent.browser.observe.reset_mock()
    with pytest.raises(RuntimeError, match="acknowledgement"):
        list(agent.run())
    agent.browser.act.assert_called_once()
    agent.browser.observe.assert_not_called()
    assert agent.state["decision"] is None


def test_automatic_run_can_exceed_default_graph_recursion_limit(agent, monkeypatch):
    choose = Mock(side_effect=[decision("wait") for _ in range(10)] + [decision("DONE")])
    monkeypatch.setattr(loop, "choose", choose)
    states = list(agent.run())
    assert len(states) == 11 and states[-1]["status"] == "done"
    assert agent.browser.act.call_count == 10 and choose.call_count == 11


def test_model_call_budget_stops_before_provider(agent, monkeypatch):
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    agent.state["decisions"] = [decision()] * (loop.MAX_STEPS * 2)
    with pytest.raises(ValueError, match="model-call budget"):
        agent.command("tick")
    choose.assert_not_called()
    agent.browser.act.assert_not_called()


def test_action_budget_stops_before_browser_input(agent, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    agent.state["history"] = [{} for _ in range(loop.MAX_STEPS)]
    with pytest.raises(ValueError, match="action demo budget"):
        agent.command("tick")
    assert agent.state["status"] == "blocked"
    agent.browser.act.assert_not_called()


def test_no_effect_click_is_not_repeated_in_the_same_state(agent, monkeypatch):
    choose = Mock(side_effect=[decision("e3"), decision("DONE")])
    monkeypatch.setattr(loop, "choose", choose)
    states = list(agent.run())
    assert [state["status"] for state in states] == ["ready", "done"]
    assert len(agent.state["history"]) == 1
    assert agent.browser.act.call_count == 1
    assert agent.state["progress_feedback"]["reason"] == "no_observed_effect"


def test_text_value_runs_only_for_fill_and_is_traced(agent, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e1")))
    helper = Mock(return_value=("book", {"model": "text-test", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_text", helper)
    trace = Trace()
    agent.command("tick", config={"callbacks": [trace]})
    helper.assert_called_once()
    assert any(name == "TextValue" for name, _tags in trace.calls)
    assert agent.browser.act.call_args.kwargs["text"] == "book"
    assert len(agent.state["text_calls"]) == 1


def test_extra_inspector_state_survives_graph(agent, monkeypatch):
    agent.state["scenario"] = "research"
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("DONE")))
    assert agent.command("tick")["scenario"] == "research"


def test_fresh_page_is_supplied_to_policy(agent, monkeypatch):
    updated = deepcopy(page())
    updated["text"] = "Updated context"
    agent.browser.fresh.return_value = False
    agent.browser.observe.return_value = updated
    choose = Mock(return_value=decision("e3"))
    monkeypatch.setattr(loop, "choose", choose)
    agent.command("predict")
    assert choose.call_args.args[0]["text"] == "Updated context"
