"""LangGraph routing for automatic runs and the inspector's single-step commands.

The live run is shared with Agent so consumed decisions and executed-action logs
survive exceptions. Browser side effects have neither retries nor checkpoint replay.
"""

import time
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from .browser import StalePage
from .candidates import CandidateBudgetExceeded, CandidateTraversalExhausted
from .logs import log_event


class RunBudgetExceeded(ValueError):
    """Manual commands report a limit error; automatic runs publish their final state."""


class TurnState(TypedDict):
    mode: Literal["run", "tick", "predict", "act"]
    body: dict[str, Any]
    run: dict[str, Any]
    stale: bool
    stale_context: dict[str, Any]
    snapshot: dict[str, Any]


def build_graph(agent):
    """Compile a workflow bound to a single owned browser session."""
    def guarded(method):
        def node(state: TurnState, config: RunnableConfig):
            agent.state = state["run"]
            before = {
                "page": getattr(agent, "_candidate_raw_page", agent.state["page"]),
                "decision_page": agent.state["page"],
                "decision": agent.state.get("decision"),
                "steps": len(agent.state["history"]),
            }
            if before["decision"]:
                before["action"] = next((action for action in agent.state["page"]["actions"]
                                         if action["id"] == before["decision"]["choice"]), {})
            try:
                method(state, config)
            except StalePage:
                if before["decision"] and len(agent.state["history"]) == before["steps"]:
                    log_event("action_discarded", before["decision"], before["decision_page"],
                              reason="stale_observation")
                if state["mode"] not in {"tick", "run"}:
                    raise
                return {"run": agent.state, "stale": True, "stale_context": before}
            except (CandidateBudgetExceeded, CandidateTraversalExhausted) as error:
                agent.state.update(status="blocked", stop_reason=error.reason, decision=None)
            except RunBudgetExceeded:
                if state["mode"] != "run":
                    raise
                agent.state["decision"] = None
            return {"run": agent.state, "stale": False}

        return node

    def execute(state, config):
        body = state["body"] if state["mode"] == "act" else agent.decision_body()
        agent._execute(body, config)

    def advance_candidates(state, config):
        body = state["body"] if state["mode"] == "act" else agent.decision_body()
        agent._advance_candidates(body, config)

    def assess(state, config):
        body = state["body"] if state["mode"] == "act" else agent.decision_body()
        agent._assess(body, config)

    def selected_route(state):
        decision = state["run"].get("decision") or {}
        return "advance_candidates" if decision.get("choice") == "MORE_CANDIDATES" else "assess"

    def recover(state: TurnState):
        run = state["run"]
        run["decision"] = None
        run["status"] = "ready"
        try:
            observed = agent._observe()
            agent._record_stale_recovery(state.get("stale_context", {}), observed)
            agent._install_page(observed)
            before = state.get("stale_context", {}).get("page", {})
            # Recovery did not necessarily execute input or change task state.
            # Keep an already-pending review, but only navigation adds a new one.
            agent._review_due = getattr(agent, "_review_due", False) or before.get("url") != observed.get("url")
        except (CandidateBudgetExceeded, CandidateTraversalExhausted) as error:
            run.update(status="blocked", stop_reason=error.reason)
        run["elapsed_ms"] = round((time.perf_counter() - run["started_at"]) * 1000)
        return {"run": run, "stale": False, "stale_context": {}}

    def publish(state: TurnState):
        agent.state = state["run"]
        if state["mode"] == "predict" and agent.state.get("decision"):
            log_event("model_proposed", agent.state["decision"], agent.state["page"])
        return {"snapshot": agent.snapshot()}

    graph = StateGraph(TurnState)
    graph.add_node("observe", guarded(lambda _state, _config: agent._prepare()))
    graph.add_node("choose", guarded(lambda _state, config: agent._choose(config)))
    graph.add_node("execute", guarded(execute))
    graph.add_node("assess", guarded(assess))
    graph.add_node("advance_candidates", guarded(advance_candidates))
    graph.add_node("recover", recover)
    graph.add_node("publish", publish)
    graph.add_conditional_edges(START, lambda s: selected_route(s) if s["mode"] == "act" else "observe",
                                ["assess", "advance_candidates", "observe"])
    graph.add_conditional_edges(
        "observe", lambda s: "recover" if s["stale"] else (
            "choose" if s["run"]["status"] == "ready" else "publish"
        ), ["recover", "choose", "publish"],
    )
    graph.add_conditional_edges(
        "choose", lambda s: "recover" if s["stale"] else (
            "publish" if s["run"]["status"] in {"blocked", "unverified"} else (
                "publish" if s["mode"] == "predict" and
                s["run"].get("decision", {}).get("choice") == "MORE_CANDIDATES" else selected_route(s)
            )
        ),
        ["recover", "publish", "assess", "advance_candidates"],
    )
    graph.add_conditional_edges(
        "assess", lambda s: "recover" if s["stale"] else (
            "publish" if s["mode"] == "predict" or not s["run"].get("decision") else "execute"
        ), ["recover", "publish", "execute"],
    )
    graph.add_conditional_edges("execute", lambda s: "recover" if s["stale"] else "publish", ["recover", "publish"])
    graph.add_conditional_edges("advance_candidates", lambda s: "recover" if s["stale"] else "publish",
                                ["recover", "publish"])
    graph.add_edge("recover", "publish")
    graph.add_conditional_edges(
        "publish", lambda s: "observe" if s["mode"] == "run"
        and s["run"]["status"] not in {"done", "blocked", "unverified", "yielded"} else END,
        ["observe", END],
    )
    # A supervising graph may have a checkpointer, but this live worker owns
    # nonserializable browser state and must never inherit checkpoint replay.
    return graph.compile(name="AgentRun", checkpointer=False)
