"""Jev agent with LangChain inference and a bounded LangGraph browser workflow."""

import base64
import hashlib
import json
import time
import warnings
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from .browser import Browser, StalePage
from .candidates import DEFAULT_LIMITS, CandidateTraversal, CandidateTraversalExhausted
from .evidence import (
    bounded_context,
    compare_pages,
    evidence_view,
    execution_history,
    navigation_source,
    progress_digest,
    semantic_digest,
)
from .logs import configure_logging, log_event
from .model import action_space, choose, field_context, field_text, prepare_candidates
from .observability import NULL_TRACE, TraceContext
from .questions import MAX_STEPS
from .runnables import JevPolicy, TextValue
from .usage import summarize_model_usage
from .verification import VerificationError, VerificationReview
from .workflow import RunBudgetExceeded, build_graph


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False, candidate_limits=None,
                 verification=True, verifier=None, max_verification_calls=24, close_tab_on_exit=False,
                 browser=None, max_actions=MAX_STEPS, execution_hooks=None, observer=None,
                 trace=True, _trace_context=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        configure_logging()
        plan = [task]
        self.pending_text = None
        if type(max_verification_calls) is not int or max_verification_calls < 1:
            raise ValueError("max_verification_calls must be a positive integer")
        if type(close_tab_on_exit) is not bool:
            raise ValueError("close_tab_on_exit must be a boolean")
        if type(max_actions) is not int or max_actions < 1:
            raise ValueError("max_actions must be a positive integer")
        self.close_tab_on_exit = close_tab_on_exit
        self.max_actions = max_actions
        self.execution_hooks = execution_hooks
        self.observer = observer
        self._subtask_budget = None
        self._delegation_feedback = None
        self._recovery_context = None
        self.verification_enabled = verification
        self.verifier = VerificationReview(reviewer=verifier) if verification else None
        self.max_verification_calls = max_verification_calls
        self._review_cache = None
        self._reviewed_decision = None
        self._review_authorization = None
        self._terminal_rejection = None
        self._historical_evidence = {}
        self._navigation_source = []
        self._pending_navigation_source = None
        self._rejected_actions = {}
        self._recovery_counts = {}
        self._semantic_visits = []
        self._review_due = False
        self.candidate_limits = candidate_limits or DEFAULT_LIMITS
        self._owns_trace = _trace_context is None
        self._trace = (_trace_context if _trace_context is not None else TraceContext(
            trace, mode="direct", metadata={"goal": task, "initial_url": url},
        ))
        self._trace_fields = {}
        try:
            self.browser = browser if browser is not None else Browser(url, candidate_limits=self.candidate_limits)
        except BaseException as error:
            self._trace_emit("browser.connection_failed", error_type=type(error).__name__)
            self._close_trace()
            raise
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except BaseException as error:
            self._trace_emit("browser.observation_failed", error_type=type(error).__name__)
            self._release_browser()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            text_model_calls=0,
            candidate_controls=[],
            candidate_windows=0,
            model_calls=0,
            model_http_attempts=0,
            verification_calls=0,
            verification_http_attempts=0,
            verification_reviews=[],
            verification={"status": "pending" if verification else "disabled", "requirements": []},
            progress_feedback={},
            stop_reason=None,
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        try:
            self._install_page(page)
        except BaseException:
            self._release_browser()
            raise
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    @property
    def trace(self):
        return getattr(self, "_trace", NULL_TRACE)

    def _trace_emit(self, event, *, component="browser", payload=None, **fields):
        return self.trace.emit(event, component=component, payload=payload,
                               **{**getattr(self, "_trace_fields", {}), **fields})

    def snapshot(self):
        """Return the latest state; every completed browser action refreshes its page."""
        # The full local inventory and prepared transport are not inspector/model inputs.
        page = {k: v for k, v in self.state["page"].items() if not k.startswith("_") and k != "inventory"}
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "page": page,
            "elements": action_space(self.state["page"]["actions"])[0],
            "model_usage": summarize_model_usage(self.state),
            "trace": self.trace.info(),
        }

    def _install_page(self, page):
        """Start a traversal for a new observation without renewing the run budgets."""
        pending_source = getattr(self, "_pending_navigation_source", None)
        if pending_source is not None:
            if page.get("url") != pending_source["url"]:
                self._navigation_source = pending_source["facts"]
                self._pending_navigation_source = None
        limits = getattr(self, "candidate_limits", DEFAULT_LIMITS)
        remaining = max(1, limits.max_windows - self.state.get("candidate_windows", 0))
        self._candidate_raw_page = page
        self._candidate_input_page = self._filter_rejected(page)
        self._candidate_traversal = CandidateTraversal(
            self._candidate_input_page, replace(limits, max_windows=remaining),
        )
        self._candidate_window_queried = False
        self._refresh_candidates()
        self._candidate_context = self._request_context()
        observer = getattr(self, "observer", None)
        if observer is not None and getattr(self, "_last_observer_page", None) is not page:
            observer(page)
            self._last_observer_page = page
        elif observer is None and getattr(self, "_last_traced_page", None) is not page:
            self._trace_observation = getattr(self, "_trace_observation", 0) + 1
            if hasattr(self, "_trace_fields"):
                self._trace_fields["observation_seq"] = self._trace_observation
            self._trace_emit("observation.recorded", observation_seq=self._trace_observation,
                             payload={key: value for key, value in page.items()
                                      if not key.startswith("_") and key != "screenshot"})
            self._last_traced_page = page

    def _observe(self):
        """Read-only observation shares the run deadline; it cannot replay input."""
        started = self.state.get("started_at")
        if started is not None:
            remaining = getattr(self, "candidate_limits", DEFAULT_LIMITS).max_seconds - (time.perf_counter() - started)
            self.state["browser"].observation_deadline = time.monotonic() + max(0, remaining)
        return self.state["browser"].observe(screenshot=self.screenshots)

    def _record_stale_recovery(self, before, observed):
        """Keep a cancelled proposal distinct from input already recorded in history."""
        decision = before.get("decision")
        if not decision:
            return
        previous_page = before["page"]
        action = before.get("action", {})
        executed = len(self.state["history"]) > before["steps"]
        proposal = {"operation": decision["operation"]}
        for key, limit in (("kind", 40), ("label", 240), ("role", 40)):
            if key in action:
                proposal[key] = str(action[key])[:limit]
        if action.get("context"):
            proposal["context"] = bounded_context(action["context"])
        changes = compare_pages(previous_page, observed, action)
        changes.sort(key=lambda change: change.get("node") != action.get("node"))
        # These changes were observed across recovery, not caused by the proposal.
        recovery = {
            "proposed_action": proposal, "action_was_executed": executed,
            "observed_changes": changes[:8], "changes_omitted": max(0, len(changes) - 8),
        }
        self._recovery_context = {
            "target": self._recovery_target(action), "url": previous_page.get("url"),
            "document_epoch": self._document_epoch(previous_page),
            "decision_id": decision.get("decision_id"), "executed": executed,
        }
        self.state["progress_feedback"] = {
            **self._base_progress_feedback(),
            "reason": "stale_observation", "recovery": recovery,
            "feedback": (
                "The action was executed once, but its first result observation was interrupted. "
                "Review the recovered observation before choosing further input."
                if executed else
                "The proposed action was NOT executed because the observation changed. "
                "Its outcome remains unproven. Locate the pending target again using current "
                "candidates or observed scroll regions if necessary. These changes are not action effects."
            ),
        }
        self._trace_emit("recovery.stale_page", decision_id=decision.get("decision_id"), payload=recovery)

    def _base_progress_feedback(self):
        """Keep the assigned task separate from transient execution diagnostics."""
        feedback = deepcopy(getattr(self, "_delegation_feedback", None) or {})
        requirements = self.state.get("verification", {}).get("requirements", [])
        if requirements or "requirements" not in feedback:
            feedback["requirements"] = deepcopy(requirements)
        return feedback

    @staticmethod
    def _recovery_target(action):
        # This identity only expires feedback; it never locates a DOM element
        # or authorizes an input. Context distinguishes identically named buttons.
        return {**{key: action.get(key) for key in ("kind", "label", "role", "href")},
                **({"value": action.get("value")} if action.get("kind") == "select" else {}),
                "context": {key: value for key, value in bounded_context(action.get("context")).items()
                            if key != "id"}}

    @staticmethod
    def _document_epoch(page):
        page_key = page.get("page_key")
        return page_key[0] if isinstance(page_key, (tuple, list)) and page_key else None

    def _retire_recovery(self, action, before, observed, decision_id):
        recovery = getattr(self, "_recovery_context", None)
        if recovery is None:
            return
        if self.state.get("progress_feedback", {}).get("reason") != "stale_observation":
            self._recovery_context = None
            return
        target = self._recovery_target(action)
        matching_target = (bool(target.get("label") or target.get("href"))
                           and target == recovery["target"] and before.get("url") == recovery["url"])
        changed_document = (observed.get("url") != before.get("url")
                            or before.get("url") != recovery["url"])
        epoch = recovery.get("document_epoch")
        changed_document = changed_document or (epoch is not None and any(
            current is not None and current != epoch
            for current in (self._document_epoch(before), self._document_epoch(observed))
        ))
        if not (matching_target or changed_document or recovery["executed"]):
            return
        # An input and its observation are now recorded. Retiring the notice is
        # not a claim that the task's business postcondition has been satisfied.
        self.state["progress_feedback"] = self._base_progress_feedback()
        self._recovery_context = None
        self._trace_emit("recovery.retired", decision_id=decision_id,
                         source_decision_id=recovery["decision_id"],
                         reason="scope_changed" if changed_document else "observed_execution")

    def _request_context(self):
        return json.dumps({
            "goal": self.state["goal"],
            "history": [
                {key: entry.get(key) for key in ("action", "kind", "text", "page_changed", "effects", "context")}
                for entry in self.state["history"][-10:]
            ],
            "progress_feedback": self.state.get("progress_feedback", {}),
        }, sort_keys=True)

    def _refresh_candidates(self):
        self._candidate_raw_page["progress_feedback"] = self.state.get("progress_feedback", {})
        self._candidate_input_page["progress_feedback"] = self.state.get("progress_feedback", {})
        self.state["page"] = prepare_candidates(
            self._candidate_input_page, self.state["goal"], self.state["history"],
            traversal=self._candidate_traversal,
        )

    def _filter_rejected(self, page):
        """A review rejection lasts only for the exact observed semantic state."""
        rejected = getattr(self, "_rejected_actions", {}).get(semantic_digest(page), set())
        if not rejected:
            return dict(page)
        filtered = {**page, "actions": [a for a in page["actions"] if a["id"] not in rejected]}
        if "inventory" in page:
            inventory = page["inventory"]
            nodes = []
            for node in inventory.get("nodes", []):
                updated = dict(node)
                updated["operations"] = [
                    kind for kind in node["operations"]
                    if kind == "select" or f"e{node['node']}_{kind}" not in rejected
                ]
                if "select" in updated["operations"]:
                    updated["options"] = [option for option in node.get("options", [])
                                          if f"e{node['node']}_select_{option['option']}" not in rejected]
                    if not updated["options"]:
                        updated["operations"].remove("select")
                if updated["operations"]:
                    nodes.append(updated)
            filtered["inventory"] = {
                **inventory, "nodes": nodes,
                "globals": [a for a in inventory.get("globals", []) if a["id"] not in rejected],
                "coverage": {**inventory.get("coverage", {}), "review_excluded_actions": len(rejected)},
            }
        return filtered

    def _reject_no_effect_action(self, action, observed, effects):
        """Do not offer the same click again when a fresh snapshot proved no change."""
        if action.get("kind") != "click" or effects != [{"kind": "no_observed_change"}]:
            return False
        digest = semantic_digest(observed)
        rejected = self._rejected_actions.setdefault(digest, set())
        rejected.add(action["id"])
        while len(self._rejected_actions) > 8:
            self._rejected_actions.pop(next(iter(self._rejected_actions)))
        self.state["progress_feedback"] = {
            **self._base_progress_feedback(),
            "feedback": (
                "The previous click was executed once, but a fresh snapshot showed no observable change. "
                "That exact click is unavailable in this unchanged state. Do not repeat it; choose another "
                "observed action or use the supervisor's recovery path."
            ),
            "reason": "no_observed_effect", "rejected_action": action.get("label", "")[:240],
            "action_was_executed": True,
        }
        self._trace_emit("progress.action_no_effect", component="browser",
                         operation=action.get("kind"), action_id=action.get("id"))
        return True

    def _check_time_budget(self):
        started = self.state.get("started_at")
        if started is not None and time.perf_counter() - started >= getattr(
            self, "candidate_limits", DEFAULT_LIMITS
        ).max_seconds:
            self.state.update(status="blocked", stop_reason="time_budget", decision=None)
            raise CandidateTraversalExhausted("time_budget")

    def decision_body(self):
        """The inspector and automatic graph use the same decision binding."""
        decision = self.state.get("decision") or {}
        return {
            "fingerprint": self.state["page"]["fingerprint"],
            **{key: decision[key] for key in ("decision_id", "inventory_id", "window_id", "selection_digest")
               if key in decision},
        }

    def _consume_decision(self, body, *, consume=True):
        state = self.state
        decision, page = state["decision"], state["page"]
        valid = decision is not None and body.get("fingerprint") == page["fingerprint"]
        coverage = page.get("candidate_coverage", {})
        # Older complete action lists retain the fingerprint-only API. Once any
        # adapter pages or trims its list, window identity is mandatory as well.
        strict_binding = "inventory" in page or (coverage and not coverage.get("budget_complete", False))
        if valid and strict_binding:
            coverage = page["candidate_coverage"]
            expected = {
                "decision_id": decision.get("decision_id"),
                "inventory_id": coverage["inventory_id"],
                "window_id": coverage["window_id"],
                "selection_digest": page["selection_digest"],
            }
            valid = all(value is not None and body.get(key) == value and decision.get(key) == value
                        for key, value in expected.items())
            valid = valid and decision.get("fingerprint") == page["fingerprint"]
        if not valid:
            raise ValueError("Observe and choose before acting; the decision or candidate window changed")
        # Consume once, before any browser input, helper call or inventory advance.
        if consume:
            state["decision"] = None
        self._check_time_budget()
        return decision, page

    def _advance_candidates(self, body, _config):
        decision, page = self._consume_decision(body)
        if decision["choice"] != "MORE_CANDIDATES":
            raise ValueError("Choose MORE_CANDIDATES before advancing the inventory")
        if not self.state["browser"].fresh(page):
            raise StalePage("Page changed before consulting more candidates. Choose again.")
        self._check_time_budget()
        coverage = page["candidate_coverage"]
        controls = self.state.setdefault("candidate_controls", [])
        record = {
            "step": len(controls) + 1, "operation": "MORE_CANDIDATES",
            "decision_id": decision["decision_id"], "inventory_id": coverage["inventory_id"],
            "from_window_id": coverage["window_id"], "status": "limited",
        }
        controls.append(record)
        try:
            self._candidate_traversal.advance()
            self._candidate_window_queried = False
            self._refresh_candidates()
        except ValueError as error:
            record["reason"] = getattr(error, "reason", "budget_insufficient")
            raise
        finally:
            self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
            record["elapsed_ms"] = self.state["elapsed_ms"]
        record.update(status="advanced", to_window_id=self.state["page"]["candidate_coverage"]["window_id"])
        self.state.update(status="ready", stop_reason=None)
        log_event("candidates_advanced", decision, page, to_window_id=record["to_window_id"])

    @property
    def graph(self):
        """The compiled workflow; created lazily for compatibility with existing integrations."""
        if not hasattr(self, "_graph"):
            self._graph = build_graph(self)
        return self._graph

    def command(self, name, body=None, *, config=None):
        if name not in {"tick", "predict", "act"}:
            raise ValueError("Unknown command")
        with self._trace_execution(), warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="`durability` has no effect when no checkpointer is present")
            result = self.graph.invoke(
                {"mode": name, "body": body or {}, "run": self.state, "stale": False},
                config=config, durability="async",
            )
        self._trace_progress()
        return {**result["snapshot"], "trace": self.trace.info()}

    def start_subtask(self, goal, *, progress_feedback=None, verified_progress=False):
        """Delegate a fresh local goal without renewing session-wide budgets."""
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("Supply a subtask")
        self._check_time_budget()
        self.pending_text = None
        self._review_cache = None
        self._reviewed_decision = None
        self._review_authorization = None
        self._terminal_rejection = None
        self._rejected_actions = {}
        self._recovery_counts = {}
        self._review_due = False
        self._subtask_budget = None
        self._recovery_context = None
        if verified_progress:
            # Only a supervisor-verified result resets cycle detection. Merely
            # renaming a subtask must not turn a navigation loop into progress.
            self._semantic_visits = []
            self.state["verified_progress_step"] = len(self.state["history"])
        self._delegation_feedback = deepcopy(progress_feedback or {})
        self.state.update(
            goal=goal.strip(), plan=[goal.strip()], plan_index=0, decision=None,
            status="ready", stop_reason=None, readiness_recoveries=0,
            progress_feedback=deepcopy(self._delegation_feedback),
            verification={"status": "pending" if self.verification_enabled else "disabled", "requirements": []},
        )
        self._install_page(self._observe())
        return self.snapshot()

    def run_subtask(self, goal, *, progress_feedback=None, verified_progress=False,
                    action_limit=12, decision_limit=24, config=None):
        """Yield worker turns until completion, blockage or a delegation quota."""
        for name, value in (("action_limit", action_limit), ("decision_limit", decision_limit)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.start_subtask(goal, progress_feedback=progress_feedback, verified_progress=verified_progress)
        self._subtask_budget = {
            "actions": len(self.state["history"]), "decisions": self.state.get("model_calls", 0),
            "action_limit": action_limit, "decision_limit": decision_limit,
        }
        try:
            yield from self.run(config=config)
        finally:
            self._subtask_budget = None

    def _prepare(self):
        state = self.state
        if not state["browser"]:
            raise ValueError("Start a demo first")
        if state["started_at"] is None:
            state["started_at"] = time.perf_counter()
        self._check_time_budget()
        if not state["browser"].fresh(state["page"]):
            self._install_page(self._observe())
        state["decision"] = None
        if state["status"] in {"done", "blocked", "unverified", "yielded"}:
            raise ValueError("This run has stopped. Start a fresh demo.")
        budget = getattr(self, "_subtask_budget", None)
        if budget:
            reason = (
                "subtask_action_limit" if len(state["history"]) - budget["actions"] >= budget["action_limit"]
                else "subtask_decision_limit"
                if state.get("model_calls", 0) - budget["decisions"] >= budget["decision_limit"] else None
            )
            if reason:
                state.update(status="yielded", stop_reason=reason)
                return
        readiness = state["page"].get("readiness", {})
        if readiness.get("status") == "loading" or (
            readiness.get("status") == "unknown" and not state["page"].get("text", "").strip()
        ):
            reads = state.get("readiness_recoveries", 0) + 1
            state["readiness_recoveries"] = reads
            self._install_page(self._observe())
            readiness = state["page"].get("readiness", {})
            if readiness.get("status") != "ready":
                state.update(status="blocked" if reads >= 3 else "observing",
                             stop_reason="observation_not_ready" if reads >= 3 else None)
                self._check_time_budget()
                return
        state["status"] = "ready"
        state["readiness_recoveries"] = 0
        calls = max(state.get("model_calls", 0), len(state["decisions"]))
        if calls >= getattr(self, "candidate_limits", DEFAULT_LIMITS).max_queries:
            state.update(status="blocked", stop_reason="decision_budget")
            raise RunBudgetExceeded("Reached the demo's model-call budget")
        if not hasattr(self, "_candidate_traversal"):
            self._install_page(state["page"])
        elif self._candidate_context != self._request_context():
            self._install_page(self._candidate_raw_page)
        else:
            self._refresh_candidates()
        if (not self._candidate_window_queried
                and state.get("candidate_windows", 0) >= getattr(self, "candidate_limits", DEFAULT_LIMITS).max_windows):
            raise CandidateTraversalExhausted("window_limit")
        self._check_time_budget()

    def _choose(self, config):
        state = self.state
        self._check_time_budget()
        if not self._candidate_window_queried:
            state["candidate_windows"] = state.get("candidate_windows", 0) + 1
            self._candidate_window_queried = True
        state["model_calls"] = state.get("model_calls", len(state["decisions"])) + 1
        body = state["page"].get("_candidate_request")
        before = getattr(body, "http_attempts", 0)
        hooks = getattr(self, "execution_hooks", None)
        before_hook = getattr(hooks, "before_decision", None)
        if before_hook is not None:
            before_hook()
        try:
            intent = (f"Jev analiza {len(state['page'].get('actions', []))} elementos observados "
                      "para elegir operación y objetivo.")
            with self.trace.call("jev", "choose", semantic_intent=intent, payload={
                "page": {key: value for key, value in state["page"].items()
                         if not key.startswith("_") and key != "screenshot"},
                "goal": state["goal"], "history": state["history"],
            }, **getattr(self, "_trace_fields", {})) as call:
                decision = JevPolicy(chooser=choose).invoke(
                    {"page": state["page"], "goal": state["goal"], "history": state["history"]}, config=config
                )
                action = next((item for item in state["page"]["actions"]
                               if item["id"] == decision["choice"]), None)
                if action is not None and decision.get("target") is not None:
                    label = json.dumps(action["label"], ensure_ascii=False)
                    semantic_summary = f"Jev decide {decision['operation'].lower()} en {label}"
                else:
                    semantic_summary = f"Jev decide {decision.get('operation') or decision['choice']}"
                call.result(decision, semantic_summary=semantic_summary, model=decision.get("model"))
        finally:
            attempts = getattr(body, "http_attempts", 0) - before
            state["model_http_attempts"] = state.get("model_http_attempts", 0) + attempts
            if attempts:
                # A submitted window was presented even when its provider response is invalid.
                self._candidate_traversal.commit_sent(state["page"]["_candidate_window"])
            after_hook = getattr(hooks, "after_decision", None)
            if after_hook is not None:
                after_hook()
        self._candidate_traversal.commit_sent(state["page"]["_candidate_window"])
        coverage = state["page"]["candidate_coverage"]
        state["decision"] = {
            **decision, "decision_id": uuid4().hex, "fingerprint": state["page"]["fingerprint"],
            "observation_seq": getattr(self, "_trace_fields", {}).get("observation_seq"),
            "inventory_id": coverage["inventory_id"], "window_id": coverage["window_id"],
            "selection_digest": state["page"]["selection_digest"],
        }
        state["decisions"].append(
            {
                **state["decision"],
                "fingerprint": state["page"]["fingerprint"],
                "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
            }
        )
        state["status"] = "predicted"
        self._trace_emit("decision.selected", component="jev", call_id=call.call_id,
                         decision_id=state["decision"]["decision_id"],
                         inventory_id=coverage["inventory_id"], window_id=coverage["window_id"],
                         payload=state["decision"])
        log_event("model_choice", state["decision"], state["page"], model_call=state["model_calls"])

    def _needs_review(self, decision, action):
        if decision["choice"] in {"DONE", "BLOCKED"}:
            return True
        if self._review_due or decision.get("review_trigger"):
            return True
        if action is None or action["kind"] not in {"click", "select"}:
            return False
        if action["kind"] == "click" and action.get("submits_form") is True:
            return True
        # A collapsed disclosure can be inspected without a semantic round-trip.
        # Submit controls and switching away from an open panel remain gated.
        if (action["kind"] == "click" and action.get("role") == "button"
                and (action.get("expanded") == "false" or action.get("expanded") is False)
                and action.get("submits_form") is False):
            observed = getattr(self, "_candidate_raw_page", self.state["page"])
            controls = observed.get("inventory", {}).get("nodes", observed.get("actions", []))
            if not any(control.get("node") != action.get("node") and
                       (control.get("expanded") == "true" or control.get("expanded") is True)
                       for control in controls):
                return False
        # Plain buttons/links can submit or leave a region. This does not claim
        # that a particular label means confirmation. Toggle state is observable.
        stateful = any(key in action for key in ("checked", "pressed", "selected"))
        return action["kind"] == "click" and not stateful and action.get("role") not in {
            "textbox", "searchbox", "combobox",
        }

    def _assess(self, body, config):
        """Review before input. LangGraph decides whether execution can follow."""
        decision, page = self._consume_decision(body, consume=False)
        if not getattr(self, "verification_enabled", False):
            return
        action = next((a for a in page["actions"] if a["id"] == decision["choice"]), None)
        if not self._needs_review(decision, action):
            self._reviewed_decision = decision["decision_id"]
            return
        if not self.state["browser"].fresh(page):
            raise StalePage("Page changed before verification. Choose again.")
        current_facts = evidence_view(self._candidate_raw_page)
        observed_scope = {
            "readiness": {key: value for key, value in page.get("readiness", {}).items()
                          if key not in {"waited_ms", "reads"}},
            "coverage": self._candidate_raw_page.get("inventory", {}).get("coverage", {}),
            "document": self._candidate_raw_page.get("page_key", [])[:2],
        }
        scope_ref = hashlib.sha256(json.dumps(observed_scope, sort_keys=True).encode()).hexdigest()[:16]
        current_facts.append({"ref": scope_ref + ":observation", "kind": "observation", **observed_scope})
        current_refs = {fact["ref"] for fact in current_facts}
        historical = [fact for ref, fact in self._historical_evidence.items() if ref not in current_refs]
        source = [fact for fact in getattr(self, "_navigation_source", []) if fact["ref"] not in current_refs]
        source_refs = {fact["ref"] for fact in source}
        facts = current_facts + source + [fact for fact in historical[-24:] if fact["ref"] not in source_refs]
        available_refs = {fact["ref"] for fact in facts}
        previous = []
        for requirement in self.state["verification"].get("requirements", []):
            refs = [ref for ref in requirement["evidence_refs"] if ref in available_refs]
            previous.append({**requirement, "evidence_refs": refs,
                             "status": "unknown" if requirement["evidence_refs"] and not refs
                             else requirement["status"]})
        trigger = decision.setdefault("review_trigger", "terminal"
                                      if decision["choice"] in {"DONE", "BLOCKED"} else (
                                          "transition" if self._review_due else "before_action"))
        payload = {
            "goal": self.state["goal"], "evidence": facts,
            "execution_history": execution_history(self.state["history"]),
            "history": [{key: entry.get(key) for key in ("action", "kind", "text", "effects", "context")}
                        for entry in self.state["history"][-10:]],
            "previous_requirements": previous,
            "selected_action": {"operation": decision["operation"], **{
                key: action[key] for key in (
                    "kind", "node", "label", "role", "value", "checked", "pressed", "selected", "context",
                ) if action and key in action
            }},
            "trigger": trigger,
            "observation_digest": semantic_digest(self._candidate_raw_page),
            "coverage": {"window": page.get("candidate_coverage", {}),
                         "extraction": self._candidate_raw_page.get("inventory", {}).get("coverage", {}),
                         "readiness": page.get("readiness", {})},
            "progress_feedback": self.state.get("progress_feedback", {}),
        }
        terminal = decision["choice"] in {"DONE", "BLOCKED"}
        terminal_evidence = hashlib.sha256(json.dumps({
            "goal": payload["goal"], "operation": decision["operation"], "facts": current_facts,
            "history": payload["history"], "execution_history": payload["execution_history"],
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if terminal else None
        if terminal and self._terminal_rejection == terminal_evidence:
            # Rephrasing a previous review is not new evidence. Let the policy
            # inspect or act after one rejected stop, but never pay for the same
            # terminal conclusion again without new observations/executions.
            self.state.update(status="unverified", stop_reason="verification_no_progress", decision=None)
            self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
            log_event("run_stopped", decision, page, status="unverified", reason="no_new_evidence")
            return
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self._reviewed_decision == decision["decision_id"] and self._review_authorization == key:
            return
        if self._review_cache and self._review_cache[0] == key:
            assessment, metadata = self._review_cache[1:]
            self._trace_emit("review.cached", component="verification", decision_id=decision["decision_id"],
                             payload={"assessment": assessment, "metadata": metadata})
        else:
            if self.state["verification_calls"] >= self.max_verification_calls:
                self.state.update(status="unverified", stop_reason="verification_budget", decision=None)
                log_event("run_stopped", decision, page, status="unverified", reason="verification_budget")
                return
            self._check_time_budget()
            self.state["verification_calls"] += 1
            try:
                with self.trace.call("verification", "review", payload=payload,
                                     decision_id=decision["decision_id"],
                                     **getattr(self, "_trace_fields", {})) as call:
                    assessment, metadata = self.verifier.invoke(payload, config=config)
                    call.result({"assessment": assessment, "metadata": metadata})
            except VerificationError as error:
                self.state["verification_http_attempts"] += error.metadata.get("http_attempts", 0)
                self.state["verification_reviews"].append({**error.metadata, "error": error.reason,
                                                          "trigger": payload["trigger"]})
                self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
                self.state.update(status="unverified", stop_reason="verification_" + error.reason, decision=None)
                log_event("run_stopped", decision, page, status="unverified", reason=self.state["stop_reason"],
                          **{key: error.metadata[key] for key in (
                              "provider_error_kind", "provider_http_status",
                              "provider_limit_source", "provider_limit_reason",
                          )
                             if key in error.metadata})
                return
            self.state["verification_http_attempts"] += metadata.get("http_attempts", 0)
            self._review_cache = (key, assessment, metadata)
            self.state["verification_reviews"].append({
                **metadata, "trigger": payload["trigger"], "assessment": assessment,
                "after_step": len(self.state["history"]),
            })
        self._check_time_budget()
        self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
        # A slow review cannot authorize a changed page, including a navigation.
        if not self.state["browser"].fresh(page):
            raise StalePage("Page changed during verification. Choose again.")
        self._review_due = False
        self.state["verification"] = {
            "status": "satisfied" if assessment["all_satisfied"] else "incomplete",
            "method": "semantic", "requirements": assessment["requirements"],
            "observation": semantic_digest(self._candidate_raw_page),
        }
        self._trace_emit("review.applied", component="verification", decision_id=decision["decision_id"],
                         payload={"assessment": assessment, "verification": self.state["verification"]})
        supported = {ref for requirement in assessment["requirements"]
                     if requirement["status"] == "satisfied" for ref in requirement["evidence_refs"]}
        for fact in current_facts:
            if fact["ref"] in supported:
                self._historical_evidence[fact["ref"]] = {
                    **fact, "historical": True, "observed_after_step": len(self.state["history"]),
                }
        while len(self._historical_evidence) > 24:
            self._historical_evidence.pop(next(iter(self._historical_evidence)))
        allowed = assessment["action_assessment"] == "allow"
        if decision["choice"] == "DONE":
            allowed = assessment["all_satisfied"]
        elif decision["choice"] == "BLOCKED":
            # A policy stop gets a bounded chance to recover from missing evidence.
            allowed = False
        if allowed:
            self._reviewed_decision = decision["decision_id"]
            self.state["progress_feedback"] = self._base_progress_feedback()
            self._recovery_context = None
            authorized_payload = {**payload, "previous_requirements": assessment["requirements"],
                                  "progress_feedback": self.state["progress_feedback"]}
            self._review_authorization = hashlib.sha256(
                json.dumps(authorized_payload, sort_keys=True, ensure_ascii=False).encode(),
            ).hexdigest()
            return
        if terminal:
            self._terminal_rejection = terminal_evidence
        digest = semantic_digest(self._candidate_raw_page)
        attempts = self._recovery_counts.get(digest, 0) + 1
        self._recovery_counts[digest] = attempts
        self.state["progress_feedback"] = {
            **self._base_progress_feedback(),
            "feedback": assessment["feedback"],
            "requirements": assessment["requirements"],
            "rejected_action": action.get("label") if action else decision["choice"],
            "reason": "requirements_unverified" if terminal else "action_not_supported_by_evidence",
            "action_was_executed": False,
        }
        self.state.update(decision=None, status="ready", stop_reason=None)
        if attempts >= 3:
            self.state.update(status="unverified", stop_reason="verification_no_progress")
        elif action and assessment["action_assessment"] == "reconsider":
            self._rejected_actions.setdefault(digest, set()).add(action["id"])
            while len(self._rejected_actions) > 8:
                self._rejected_actions.pop(next(iter(self._rejected_actions)))
        self._install_page(self._candidate_raw_page)
        log_event("action_discarded", decision, page,
                  reason="review_" + assessment["action_assessment"])
        if self.state["status"] == "unverified":
            log_event("run_stopped", decision, page, status="unverified", reason=self.state["stop_reason"])

    def _execute(self, body, config):
        state = self.state
        decision, page = self._consume_decision(body)
        selected = decision["choice"]
        if (getattr(self, "verification_enabled", False)
                and self._reviewed_decision != decision.get("decision_id")):
            raise ValueError("Verify the current decision before execution")
        if selected in {"DONE", "BLOCKED"}:
            if not state["browser"].fresh(page):
                state["status"] = "ready"
                raise StalePage("Page changed since the decision. Choose again.")
            state["status"] = "done" if selected == "DONE" else "blocked"
            state["stop_reason"] = (
                page.get("candidate_coverage", {}).get("termination_reason") or "model_blocked"
            ) if selected == "BLOCKED" else None
            state["plan_index"] = int(selected == "DONE")
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            if getattr(self, "_subtask_budget", None) is None:
                log_event("run_stopped", decision, page, status=state["status"])
            return
        action = next(a for a in page["actions"] if a["id"] == selected)
        max_actions = getattr(self, "max_actions", MAX_STEPS)
        if len(state["history"]) >= max_actions:
            state["status"] = "blocked"
            state["stop_reason"] = "action_budget"
            raise RunBudgetExceeded(f"Stopped at the {max_actions}-action demo budget")
        text, helper = None, None
        if action["kind"] == "fill":
            if not state["browser"].fresh(page):
                raise StalePage("Page changed before text generation. Choose again.")
            # Capture the complete helper input before invocation. Feedback and
            # effect lists can otherwise mutate the cached retry context in place.
            context = deepcopy(field_context(
                state["goal"], action, {**page, "progress_feedback": state.get("progress_feedback", {})},
                state["history"],
            ))
            if self.pending_text and self.pending_text[0] == context:
                _, text, helper = self.pending_text
                self._trace_emit("text.cached", component="text", decision_id=decision.get("decision_id"),
                                 payload={"input": context, "text": text})
            else:
                hooks = getattr(self, "execution_hooks", None)
                state["text_model_calls"] = state.get("text_model_calls", len(state["text_calls"])) + 1
                before_hook = getattr(hooks, "before_text", None)
                if before_hook is not None:
                    before_hook()
                try:
                    field_label = json.dumps(action.get("label", "el campo"), ensure_ascii=False)
                    with self.trace.call("text", "field_value",
                                         semantic_intent=f"Auxiliar de texto prepara un valor para {field_label}.",
                                         payload=context,
                                         decision_id=decision.get("decision_id"),
                                         **getattr(self, "_trace_fields", {})) as call:
                        text, helper = TextValue(generator=field_text).invoke(context, config=config)
                        call.result({"text": text, "metadata": helper},
                                    semantic_summary=f"Auxiliar de texto generó el contenido para {field_label}.",
                                    model=helper.get("model"))
                finally:
                    after_hook = getattr(hooks, "after_text", None)
                    if after_hook is not None:
                        after_hook()
                self.pending_text = (context, text, helper)
                state["text_calls"].append({**helper, "field": action["label"], "value": text})
        self._check_time_budget()
        # Browser.act checks freshness immediately before input, including after text generation.
        hooks = getattr(self, "execution_hooks", None)
        attempt = hooks.before_action(action, page, text) if hooks is not None else None
        trace_attempt = attempt or uuid4().hex
        self._trace_emit("browser.started", attempt_id=trace_attempt, decision_id=decision.get("decision_id"),
                         payload={"action": action, "text": text, "url": page.get("url")})
        try:
            state["browser"].act(action, page, text=text)
        except StalePage:
            # Browser guarantees this exception can only precede input.
            if hooks is not None:
                hooks.rejected(attempt)
            self._trace_emit("browser.discarded", attempt_id=trace_attempt,
                             decision_id=decision.get("decision_id"), reason="stale_observation")
            raise
        except BaseException as error:
            self._trace_emit("browser.uncertain", attempt_id=trace_attempt,
                             decision_id=decision.get("decision_id"), error_type=type(error).__name__)
            raise
        self.pending_text = None
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        # Record execution before observing. A stale post-action observation must not erase the action.
        state["history"].append(
            {
                "step": len(state["history"]) + 1,
                "action": action["label"],
                **({"context": bounded_context(action["context"])} if action.get("context") else {}),
                "kind": action["kind"],
                "choice": selected,
                "probability": decision["probabilities"][selected],
                "confidence": decision["confidence"],
                "latency_ms": decision["latency_ms"],
                "text": text,
                "text_helper": helper["model"] if helper else None,
                "text_latency_ms": helper["latency_ms"] if helper else 0,
                "operation": decision["operation"],
                "target": decision["target"],
                **({"node": action["node"]} if action.get("node") is not None else {}),
                "page_changed": None,
                "url": page["url"],
                **{key: action[key] for key in ("href", "href_truncated") if key in action},
                "usage": decision["usage"],
                "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                "elapsed_ms": state["elapsed_ms"],
            }
        )
        log_event("action_executed", decision, page, step=state["history"][-1]["step"])
        self._trace_emit("browser.input_returned", attempt_id=trace_attempt,
                         decision_id=decision.get("decision_id"), payload=state["history"][-1])
        if hooks is not None:
            hooks.dispatched(attempt, state["history"][-1])
        # Preserve actual source observations before a navigation makes them
        # unavailable. A failed post-action read cannot erase this packet.
        source_page = getattr(self, "_candidate_raw_page", page)
        if action["kind"] != "wait" or getattr(self, "_pending_navigation_source", None) is None:
            self._pending_navigation_source = {
                "url": source_page["url"],
                "facts": navigation_source(source_page, action, len(state["history"])),
            }
        # Always take a new DOM snapshot after input, even if the page appears unchanged.
        try:
            observed = self._observe()
        except BaseException as error:
            self._trace_emit("browser.observation_failed", attempt_id=trace_attempt,
                             decision_id=decision.get("decision_id"), error_type=type(error).__name__)
            raise
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        state["history"][-1].update(
            page_changed=observed["fingerprint"] != page["fingerprint"],
            url=observed["url"],
            elapsed_ms=state["elapsed_ms"],
            effects=compare_pages(getattr(self, "_candidate_raw_page", page), observed, action),
        )
        no_effect_click = self._reject_no_effect_action(
            action, observed, state["history"][-1]["effects"],
        )
        if (not no_effect_click and semantic_digest(page) != semantic_digest(observed)
                and state.get("progress_feedback", {}).get("reason") == "no_observed_effect"):
            state["progress_feedback"] = self._base_progress_feedback()
        # An unobserved region may merely have scrolled out of view. It is not
        # evidence of a committed/closed panel and must not force another review.
        self._review_due = any(effect["kind"] == "navigated"
                               for effect in state["history"][-1]["effects"])
        self._retire_recovery(action, page, observed, decision.get("decision_id"))
        self._install_page(observed)
        if hooks is not None:
            hooks.observed(attempt, observed, state["history"][-1])
        self._trace_emit("browser.observed", attempt_id=trace_attempt,
                         decision_id=decision.get("decision_id"), payload=state["history"][-1])
        digest = progress_digest(observed)
        visits = getattr(self, "_semantic_visits", [])
        visits.append(digest)
        self._semantic_visits = visits[-12:]
        visits_count = self._semantic_visits.count(digest)
        if visits_count >= 3 and action["kind"] != "wait":
            if not no_effect_click:
                self.state["progress_feedback"] = {
                    **self._base_progress_feedback(),
                    "feedback": ("The same observable state has recurred three times. "
                                 "Check unmet requirements and effects."),
                    "reason": "repeated_state",
                }
            self._recovery_context = None
            self._review_due = True
        elif visits_count < 3 and state.get("progress_feedback", {}).get("reason") == "repeated_state":
            state["progress_feedback"] = self._base_progress_feedback()
            self._trace_emit("progress.cycle_cleared", decision_id=decision.get("decision_id"))
        if state["record"]:
            (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                base64.b64decode(state["page"]["screenshot"])
            )
        repeated = state["history"][state.get("verified_progress_step", 0):][-3:]
        state["status"] = (
            "blocked"
            if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
            else "ready"
        )
        if state["status"] == "blocked":
            state["stop_reason"] = "no_progress"
        if visits_count >= 5 and action["kind"] != "wait":
            state.update(status="blocked", stop_reason="repeated_state")

    def run(self, *, config=None):
        """Stream completed turns while LangGraph routes the agent until it stops."""
        if self.state["status"] in {"done", "blocked", "unverified", "yielded"}:
            return
        # Each turn has at most five nodes. The model/action budgets remain the
        # primary limits; this also bounds pages that never settle enough to choose.
        config = {
            "recursion_limit": getattr(self, "candidate_limits", DEFAULT_LIMITS).max_queries * 9 + 10,
            **(config or {}),
        }
        # This worker has no checkpointer. Override a supervisor's inherited
        # synchronous durability, which otherwise waits for a nonexistent
        # checkpoint future in LangGraph. Only the supervisor is persistent.
        with self._trace_execution(), warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="`durability` has no effect when no checkpointer is present")
            for update in self.graph.stream(
                {"mode": "run", "body": {}, "run": self.state, "stale": False},
                config=config,
                stream_mode="updates",
                durability="async",
            ):
                if "publish" in update:
                    self._trace_progress()
                    yield {**update["publish"]["snapshot"], "trace": self.trace.info()}

    def _trace_progress(self):
        self._trace_emit("worker.progress", component="worker",
                         status=self.state["status"], stop_reason=self.state.get("stop_reason"),
                         metrics={key: self.state.get(key, 0) for key in (
                             "model_calls", "model_http_attempts", "text_model_calls",
                             "verification_calls", "verification_http_attempts", "candidate_windows",
                         )}, actions=len(self.state["history"]),
                         payload={"verification": self.state.get("verification", {})})

    @contextmanager
    def _trace_execution(self):
        with self.trace.bind(**getattr(self, "_trace_fields", {})):
            try:
                yield
            except BaseException as error:
                self._trace_emit("worker.failed" if isinstance(error, Exception) else "worker.interrupted",
                                 component="worker", error_type=type(error).__name__)
                self._trace_progress()
                raise

    def close(self):
        """Explicitly close the tab owned by this agent."""
        try:
            self.browser.close()
        finally:
            self._close_trace()

    def disconnect(self):
        """Release control and leave the current browser tab open."""
        try:
            self.browser.disconnect()
        finally:
            self._close_trace()

    def _close_trace(self):
        if getattr(self, "_owns_trace", False):
            state = getattr(self, "state", {})
            self.trace.close(status=state.get("status", "initialization_failed"),
                             stop_reason=state.get("stop_reason"))

    def _release_browser(self):
        if self.close_tab_on_exit:
            self.close()
        else:
            self.disconnect()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._release_browser()
