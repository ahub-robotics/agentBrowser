"""Persistent, sequential LangGraph supervision of the native Jev worker."""

import hashlib
import json
import logging
import os
import tempfile
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from AgentBrowser.agent import Agent
from AgentBrowser.browser import Browser
from AgentBrowser.candidates import DEFAULT_LIMITS
from AgentBrowser.evidence import _controls, evidence_view, execution_history, semantic_digest
from AgentBrowser.logs import configure_logging, log_delegation
from AgentBrowser.model import ProviderError
from AgentBrowser.observability import NULL_TRACE, TraceConfig, TraceContext
from AgentBrowser.usage import summarize_model_usage

from .coordinator import CoordinatorMixin
from .supervisor_policy import SupervisorDecision, SupervisorPolicy
from .supervisor_store import RunStore

LOG = logging.getLogger("agentrun")
WORKER_KEYS = (
    "history", "decisions", "text_calls", "text_model_calls", "candidate_controls", "candidate_windows",
    "model_calls", "model_http_attempts", "verified_progress_step",
)
TERMINAL = {"done", "blocked", "unverified", "waiting_feedback", "browser_unavailable"}
MAX_FACTS = 6000
EVIDENCE_SLICE = 120
OBSERVED_LINK_LIMIT = 80


class InvalidSupervisorDecision(ValueError):
    """A local validation message safe to return to the model and console."""


@dataclass(frozen=True)
class SupervisorLimits:
    max_actions: int = 180
    max_decisions: int = 360
    max_windows: int = 360
    max_seconds: float = 900
    max_supervisor_calls: int = 60
    delegation_actions: int = 12
    delegation_decisions: int = 24

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "max_seconds":
                valid = type(value) in (int, float) and 0 < value < float("inf")
            else:
                valid = type(value) is int and value > 0
            if not valid:
                raise ValueError(f"{name} must be finite and positive")


def raise_saved_limits(saved, overrides):
    """Apply explicit monotonic budget increases when resuming a saved run."""
    limits = SupervisorLimits(**saved)
    if not isinstance(overrides, dict):
        raise ValueError("limit_overrides must be a mapping")
    unknown = set(overrides) - set(asdict(limits))
    if unknown:
        raise ValueError("Unknown budget override: " + ", ".join(sorted(unknown)))
    updated = replace(limits, **overrides)
    lowered = [name for name in overrides if getattr(updated, name) < getattr(limits, name)]
    if lowered:
        raise ValueError("Resume can only raise saved budgets: " + ", ".join(sorted(lowered)))
    return updated


class GraphState(TypedDict):
    supervision: dict


class _ExecutionHooks:
    def __init__(self, owner):
        self.owner = owner

    def before_decision(self):
        self.owner._save()

    def after_decision(self):
        self.owner._save()

    def before_text(self):
        self.owner._save()

    def after_text(self):
        self.owner._save()

    def before_action(self, action, page, text):
        owner = self.owner
        if owner._action_count() >= owner.limits.max_actions:
            raise ValueError("Global action budget exhausted")
        attempt = owner.store.reserve_attempt(
            owner.state["pending_delegation"]["id"], owner.state.get("active_item"), action, page, text,
        )
        owner.state["attempt_observations"][attempt] = owner.state["observation_seq"]
        owner._save()
        owner.trace.emit("attempt.reserved", component="browser", attempt_id=attempt,
                         delegation_id=owner.state["pending_delegation"]["id"],
                         item_id=owner.state.get("active_item"), observation_seq=owner.state["observation_seq"])
        return attempt

    def dispatched(self, token, history_entry):
        history_entry["attempt_id"] = token
        self.owner.store.mark_dispatched(token, history_entry)
        self.owner._save()
        self.owner.trace.emit("attempt.dispatched", component="browser", attempt_id=token,
                              observation_seq=self.owner.state["observation_seq"],
                              delegation_id=(self.owner.state.get("pending_delegation") or {}).get("id"),
                              payload=history_entry)

    def observed(self, token, page, history_entry):
        self.owner.store.mark_observed(token, page, history_entry)
        self.owner._save()
        self.owner.trace.emit("attempt.observed", component="browser", attempt_id=token,
                              observation_seq=self.owner.state["observation_seq"], payload=history_entry)

    def rejected(self, token):
        self.owner.store.mark_rejected(token)
        self.owner._save()
        self.owner.trace.emit("attempt.rejected", component="browser", attempt_id=token,
                              observation_seq=self.owner.state["observation_seq"])


class LegacySupervisedAgent:
    """One natural-language goal, one owned tab, resumable supervisor state.

    A supplied supervisor is a callable(payload)->SupervisorDecision-compatible
    dict, or an object with invoke(payload). Tests inject it and the native Jev
    policy/text providers; importing this module performs no external I/O.
    """

    def __init__(self, url, goal, *, run_id=None, state_dir="artifacts/supervised", limits=None,
                 supervisor=None, browser=None, close_tab_on_exit=False, trace=True):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("Supply one natural-language goal")
        run_id = run_id or uuid4().hex
        self._setup(run_id, state_dir, supervisor, close_tab_on_exit)
        try:
            if self.store.load_state() is not None:
                raise ValueError("Run already exists; use SupervisedAgent.resume")
            self.limits = limits or SupervisorLimits()
            self.state = {
                "run_id": run_id, "goal": goal.strip(), "initial_url": url, "target": None, "status": "ready",
                "stop_reason": None, "question": "", "plan": [], "requirements": [], "loops": {}, "items": {},
                "evidence": {}, "current_evidence_refs": [], "sent_refs": [], "evidence_offset": 0,
                "evidence_omitted": 0, "observation_seq": 0, "attempt_observations": {},
                "feedback": [], "last_result": None, "pending_delegation": None, "active_item": None,
                "delegation_sequence": 0, "supervisor_calls": 0, "supervisor_http_attempts": 0,
                "supervisor_usage": [], "elapsed_seconds": 0.0, "limits": asdict(self.limits),
                "corrections": 0, "progress_revision": 0, "delegated_progress_revision": 0,
                "item_aliases": {},
                "worker": {}, "semantic_visits": [], "verification": {"status": "pending"},
            }
            self._start_trace(trace)
            self._save()
            candidate_browser = browser if browser is not None else Browser(
                url, candidate_limits=self._candidate_limits(), enable_history=True,
            )
            self.store.bind_target(candidate_browser.target)
            self.browser = candidate_browser
            self.state["target"] = self.browser.target
            self._make_worker()
            self._save()
        except BaseException:
            self._release()
            raise

    def _setup(self, run_id, state_dir, supervisor, close_tab_on_exit):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 80 or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in run_id
        ):
            raise ValueError("Invalid run_id")
        if type(close_tab_on_exit) is not bool:
            raise ValueError("close_tab_on_exit must be a boolean")
        configure_logging()
        self.close_tab_on_exit = close_tab_on_exit
        self.run_id = run_id
        self.state_dir = Path(state_dir)
        self.worker = None
        self.browser = None
        self.state = {}
        self._trace = NULL_TRACE
        self._active_start = None
        self._running = False
        self._closed = False
        self.supervisor = supervisor if supervisor is not None else SupervisorPolicy()
        self.store = RunStore(self.state_dir / run_id / "state.sqlite", run_id,
                              lock_dir=Path(tempfile.gettempdir()) / f"agentrun-supervisor-{os.getuid()}")

    @property
    def trace(self):
        return getattr(self, "_trace", NULL_TRACE)

    def _start_trace(self, config, *, resumed=False):
        if config is True and self.state.get("trace_directory"):
            config = TraceConfig(directory=self.state["trace_directory"])
        self._trace = TraceContext(config, run_id=self.run_id, mode="supervised", resumed=resumed,
                                   secrets=(self.supervisor.trace_secrets()
                                            if isinstance(self.supervisor, SupervisorPolicy) else ()),
                                   state_path=str(self.store.path.absolute()), metadata={
                                       "goal": self.state.get("goal"), "initial_url": self.state.get("initial_url"),
                                   })
        if self.trace.enabled and not self.trace.degraded:
            self.state["trace_directory"] = str(self.trace.directory.parent.absolute())

    @classmethod
    def resume(cls, run_id, *, feedback=None, state_dir="artifacts/supervised", supervisor=None,
               browser=None, close_tab_on_exit=False, trace=True, limit_overrides=None):
        instance = cls.__new__(cls)
        instance._setup(run_id, state_dir, supervisor, close_tab_on_exit)
        try:
            instance.state = instance._load_state()
            if instance.state is None:
                raise ValueError("Unknown run_id")
            instance._start_trace(trace, resumed=True)
            instance.limits = SupervisorLimits(**instance.state["limits"])
            if limit_overrides is not None:
                instance.limits = raise_saved_limits(instance.state["limits"], limit_overrides)
                instance.state["limits"] = asdict(instance.limits)
                instance.trace.emit("budget.raised", component="supervisor", payload={
                    "limits": asdict(instance.limits),
                })
            if instance.state["status"] == "done":
                return instance
            if feedback is not None:
                instance._accept_feedback(feedback)
            if instance.state["status"] == "done" or (
                instance.state["status"] == "waiting_feedback" and feedback is None
            ):
                return instance
            target = instance.state["target"]
            if not target:
                instance.state.update(status="browser_unavailable", stop_reason="browser_unavailable",
                                      question="La ejecución no conserva una pestaña original a la que reconectarse.")
                instance._save()
                return instance
            instance.store.bind_target(target)
            try:
                if browser is not None and browser.target != target:
                    raise ValueError("Resume requires the original browser target")
                instance.browser = browser if browser is not None else Browser.attach(
                    target, candidate_limits=instance._candidate_limits(), enable_history=True,
                )
                instance._make_worker()
            except Exception:
                if browser is not None and browser.target != target:
                    raise
                instance.state.update(status="browser_unavailable", stop_reason="browser_unavailable",
                                      question="La pestaña original no está disponible; el progreso se ha conservado.")
                instance._save()
                return instance
            pending = instance.state.get("pending_delegation")
            if pending:
                record = instance.store.get_delegation(pending["id"])
                result = record.get("result") if record else None
                if result is None:
                    result = instance._worker_result("interrupted", "process_interrupted")
                    instance.store.save_delegation(pending["id"], pending["input"], result)
                instance.state["last_result"] = result
                instance.state["pending_delegation"] = None
            instance.state.update(
                status="waiting_provider" if instance.state.get("provider_retry") is not None else "ready",
                stop_reason=None, question="", evidence_offset=0,
            )
            instance._save()
            return instance
        except BaseException:
            instance._release()
            raise

    def _accept_feedback(self, feedback):
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValueError("Supply nonempty feedback")
        if len(feedback.strip()) > 4000:
            raise ValueError("Feedback must not exceed 4000 characters")
        self.state["feedback"].append(feedback.strip())
        self.state.update(corrections=0, validation_retries=0, status="ready", question="", stop_reason=None,
                          evidence_offset=0)
        self.state.pop("validation_error", None)
        self.state.pop("provider_error", None)
        self.trace.emit("feedback.received", component="supervisor", payload={"feedback": feedback.strip()})

    def _load_state(self):
        return self.store.load_state()

    def submit_feedback(self, feedback):
        """Accept a reply on the paused run; only reads occur until run() resumes."""
        if self._closed:
            raise ValueError("Agent has been disconnected")
        if self._running:
            raise RuntimeError("Finish consuming run() before submitting feedback")
        if self.state["status"] != "waiting_feedback":
            raise ValueError("This run is not waiting for feedback")
        self._accept_feedback(feedback)
        reason = self._budget_reason()
        if reason:
            self._pause(reason, "Se ha alcanzado un límite global. El progreso parcial está guardado.")
            return self.snapshot()
        self._save()
        try:
            if self.browser is None:
                if not self.state.get("target"):
                    raise ValueError("The original tab is unavailable")
                self.store.bind_target(self.state["target"])
                self.browser = Browser.attach(
                    self.state["target"], candidate_limits=self._candidate_limits(), enable_history=True,
                )
            if self.worker is None:
                self._make_worker()
            else:
                self.worker.pending_text = None
                self.worker.state.update(status="ready", decision=None, started_at=None, stop_reason=None)
                self.browser.observation_deadline = time.monotonic() + max(
                    0, self.limits.max_seconds - self.state["elapsed_seconds"],
                )
                self.worker._install_page(self.worker._observe())
        except Exception:
            self.state.update(status="browser_unavailable", stop_reason="browser_unavailable",
                              question="No se pudo observar la pestaña original. "
                                       "El feedback y el progreso están guardados.")
        self._save()
        return self.snapshot()

    def _candidate_limits(self):
        return replace(DEFAULT_LIMITS, max_queries=self.limits.max_decisions,
                       max_windows=self.limits.max_windows, max_seconds=self.limits.max_seconds)

    def _make_worker(self):
        saved = deepcopy(self.state.get("worker", {}))
        self.worker = Agent(
            self.state["initial_url"], self.state["goal"], browser=self.browser,
            verification=False, candidate_limits=self._candidate_limits(), max_actions=self.limits.max_actions,
            execution_hooks=_ExecutionHooks(self), observer=self._record_page, _trace_context=self.trace,
        )
        self.worker.state.update(saved)
        self.worker.state.update(status="ready", decision=None, started_at=None)
        self.worker.pending_text = None
        self.worker._semantic_visits = list(self.state.get("semantic_visits", []))
        known = {entry.get("attempt_id") for entry in self.worker.state["history"] if entry.get("attempt_id")}
        for attempt in self.store.attempts():
            entry = attempt.get("history")
            if entry and attempt["id"] not in known:
                self.worker.state["history"].append({**entry, "attempt_id": attempt["id"]})
                known.add(attempt["id"])

    def _record_page(self, page):
        self.state["observation_seq"] += 1
        seq = self.state["observation_seq"]
        facts = evidence_view(page)
        scope = facts[0]["ref"].split(":")[0]
        existing = {fact.get("node") for fact in facts if fact.get("kind") == "control"}
        for node, control in _controls(page).items():
            if control["node"] not in existing:
                facts.append({"ref": f"{scope}:node:{node}", "kind": "control", **control})
        # Coverage describes extraction, not merely the first fact slice.
        facts.append({
            "ref": f"{scope}:extraction", "kind": "coverage",
            "extraction": deepcopy(page.get("inventory", {}).get("coverage", {})),
            "candidate_coverage": deepcopy(page.get("candidate_coverage", {})),
        })
        current = []
        for fact in facts:
            fact = {**fact, "observed_at": seq, "observed_url": page.get("url", "")}
            self.state["evidence"][fact["ref"]] = fact
            current.append(fact["ref"])
        active = self.state["items"].get(self.state.get("active_item"))
        if (active and active["status"] != "completed" and active["started_observation"] is not None
                and seq > active["started_observation"]):
            destination = page.get("url", "")
            page_ref = f"{scope}:page:url"
            if destination == active["url"] and len(destination) <= 2000:
                active.setdefault("visited_refs", []).append(page_ref)
                active["visited_refs"] = list(dict.fromkeys(active["visited_refs"]))
                active["destination_evidence_refs"] = current
            attempts = self.store.attempts()
            latest = attempts[-1] if attempts else {}
            if (latest.get("item_key") == active["item_id"]
                    and latest.get("action", {}).get("href") == active["url"]
                    and latest.get("state") in {"dispatched", "observed"}
                    and destination != latest.get("url") and len(destination) <= 2000):
                active.setdefault("navigation_destinations", {})[page_ref] = destination
        self.state["current_evidence_refs"] = current
        self.state["page"] = {key: deepcopy(page.get(key)) for key in ("url", "title", "text", "readiness")}
        self.state["page"]["text"] = str(self.state["page"].get("text") or "")[:6000]
        self.state["page"]["candidate_coverage"] = deepcopy(
            page.get("candidate_coverage", page.get("inventory", {}).get("coverage", {})),
        )
        self.state["page_digest"] = semantic_digest(page)
        self.state["evidence_offset"] = 0
        pinned = set(current) | set(self._result_refs())
        for group in (self.state["requirements"], self.state["items"].values(), self.state["loops"].values()):
            for entry in group:
                pinned.update(entry.get("evidence_refs", []))
        for ref in list(self.state["evidence"]):
            if len(self.state["evidence"]) <= MAX_FACTS:
                break
            if ref not in pinned:
                del self.state["evidence"][ref]
                self.state["evidence_omitted"] += 1
        self.state["sent_refs"] = [ref for ref in self.state["sent_refs"] if ref in self.state["evidence"]]
        self._save()
        if self.worker is not None:
            self.worker._trace_fields["observation_seq"] = seq
        self.trace.emit("observation.recorded", component="browser", observation_seq=seq,
                        delegation_id=(self.state.get("pending_delegation") or {}).get("id"),
                        item_id=self.state.get("active_item"), payload={
                            "page": {key: value for key, value in page.items()
                                     if not key.startswith("_") and key != "screenshot"},
                            "evidence": [self.state["evidence"][ref] for ref in current],
                        })

    def _capture_worker(self):
        if self.worker is not None:
            self.state["worker"] = {key: deepcopy(self.worker.state[key])
                                    for key in WORKER_KEYS if key in self.worker.state}
            # Native decisions retain exact requests for the inspector. They
            # are not replayable and can be 96 KiB each; persist compact history.
            self.state["worker"]["decisions"] = [
                {key: value for key, value in decision.items() if key in {
                    "choice", "operation", "target", "confidence", "latency_ms", "http_attempts", "usage",
                    "model", "decision_id",
                }} for decision in self.state["worker"].get("decisions", [])
            ]
            self.state["semantic_visits"] = list(self.worker._semantic_visits)

    def _save(self, *, reconciliations=None):
        if not self.state:
            return
        if self._active_start is not None:
            now = time.perf_counter()
            self.state["elapsed_seconds"] += now - self._active_start
            self._active_start = now
        self._capture_worker()
        if reconciliations is None:
            self.store.save_state(self.state)
        else:
            self.store.save_state_with_reconciliations(self.state, reconciliations)
            for reconciliation in reconciliations:
                self.trace.emit("attempt.reconciled", component="coordinator", **reconciliation,
                                progress_revision=self.state["progress_revision"])

    def _action_count(self):
        return sum(attempt["state"] != "rejected" for attempt in self.store.attempts())

    def _metrics(self):
        worker = self.state.get("worker", {})
        return {
            "actions": self._action_count(), "jev_calls": worker.get("model_calls", 0),
            "jev_http_attempts": worker.get("model_http_attempts", 0),
            "text_calls": worker.get("text_model_calls", len(worker.get("text_calls", []))),
            "supervisor_calls": self.state.get("supervisor_calls", 0),
            "supervisor_http_attempts": self.state.get("supervisor_http_attempts", 0),
            "candidate_windows": worker.get("candidate_windows", 0),
        }

    def snapshot(self):
        return {
            "run_id": self.run_id, "status": self.state["status"], "stop_reason": self.state.get("stop_reason"),
            "trace": self.trace.info(),
            "question": self.state.get("question", ""), "elapsed_ms": round(self.state["elapsed_seconds"] * 1000),
            "plan": deepcopy(self.state["plan"]), "requirements": deepcopy(self.state["requirements"]),
            "items": deepcopy(self.state["items"]), "page": deepcopy(self.state.get("page", {})),
            "metrics": self._metrics(), "verification": deepcopy(self.state["verification"]),
            "model_usage": summarize_model_usage({**self.state, "metrics": self._metrics()}),
            "last_result": deepcopy(self.state.get("last_result")),
        }

    def _pause(self, reason, question):
        self.state.update(status="waiting_feedback", stop_reason=reason, question=question)
        self.state["verification"] = {"status": "incomplete"}
        LOG.info("Feedback necesario: %s", question, extra={"agentrun_agent": "supervisor"})
        self.trace.emit("run.paused", component="supervisor", reason=reason, payload={"question": question})
        self._save()
        # Paused time is not active execution, even if the consumer has not
        # yet closed/exhausted the stream publishing this terminal state.
        self._active_start = None

    def _budget_reason(self, *, execution=True):
        metrics = self._metrics()
        for key, limit, reason in (
            ("actions", self.limits.max_actions, "action_budget"),
            ("jev_calls", self.limits.max_decisions, "decision_budget"),
            ("candidate_windows", self.limits.max_windows, "window_limit"),
            ("supervisor_calls", self.limits.max_supervisor_calls, "supervisor_budget"),
        ):
            if metrics[key] >= limit and (execution or key == "supervisor_calls"):
                return reason
        if self.state["elapsed_seconds"] >= self.limits.max_seconds:
            return "time_budget"
        return None

    def _result_refs(self):
        """Keep the postconditions needed at a delegation boundary accessible."""
        last = self.state.get("last_result") or {}
        worker = self.worker.state if self.worker else self.state.get("worker", {})
        histories = [*last.get("history", []), *worker.get("history", [])[-16:]]
        refs = [effect["ref"] for entry in reversed(histories) for effect in entry.get("effects", [])
                if effect.get("ref") and effect.get("kind") not in {"control_observed", "opened"}]
        active = self.state["items"].get(self.state.get("active_item"), {})
        refs.extend(active.get("visited_refs", []))
        refs.extend(active.get("destination_evidence_refs", []))
        refs.extend(active.get("navigation_destinations", {}))
        if active.get("source_ref"):
            refs.append(active["source_ref"])
        refs.extend(last.get("evidence_refs", []))
        return list(dict.fromkeys(refs))

    def _goal_coverage(self):
        """A partial requirements list cannot silently narrow the original goal."""
        goal = self.state["goal"]
        covered = [not char.isalnum() for char in goal]
        for item in self.state["requirements"]:
            quote = item["goal_quote"]
            start = goal.find(quote)
            while start >= 0:
                covered[start:start + len(quote)] = [True] * len(quote)
                start = goal.find(quote, start + len(quote))
        missing = []
        start = None
        for index, matched in enumerate([*covered, True]):
            if not matched and start is None:
                start = index
            if matched and start is not None:
                missing.append(goal[start:index])
                start = None
        return {"complete": all(covered), "uncovered_words": missing[:200], "omitted_words": max(0, len(missing) - 200)}

    def _observed_links(self):
        """One complete, observed fact per URL; relevance remains the supervisor's job."""
        links = {}
        for fact in sorted(self.state["evidence"].values(), key=lambda fact: fact.get("observed_at", 0), reverse=True):
            href = fact.get("href")
            if (fact.get("kind") != "control" or not isinstance(href, str) or not href
                    or len(href) > 2000 or fact.get("href_truncated")
                    or urlparse(href).scheme not in {"http", "https"} or not urlparse(href).netloc):
                continue
            if href not in links:
                links[href] = {key: deepcopy(fact[key]) for key in (
                    "ref", "kind", "href", "label", "context", "observed_at", "observed_url",
                ) if key in fact}
        return list(links.values())

    def _feedback_reasons(self):
        reasons = ["missing_user_information"]
        if self.store.attempts(unresolved_only=True):
            reasons.append("uncertain_effect")
        if self.state["corrections"] >= 2:
            reasons.append("recovery_exhausted")
        if (self.state.get("last_result") or {}).get("provider_error"):
            reasons.append("provider_error")
        return reasons

    def _trim_payload(self, payload):
        """Bound a contiguous evidence slice, preserving its exact continuation."""
        while ((payload["evidence"] or payload["observed_links"])
               and len(json.dumps(payload, ensure_ascii=False).encode()) > 120 * 1024):
            if payload["observed_links"]:
                payload["observed_links"].pop()
                payload["observed_links_omitted"] += 1
            else:
                payload["evidence"].pop()
                refs = {fact["ref"] for fact in payload["evidence"]}
                payload["current_evidence_refs"] = [ref for ref in payload["current_evidence_refs"] if ref in refs]
                payload["next_evidence_offset"] = payload["evidence_offset"] + len(payload["evidence"])

    def _payload(self):
        current = self.state["current_evidence_refs"]
        pinned = []
        for group in (self.state["requirements"], self.state["items"].values(), self.state["loops"].values()):
            for item in group:
                pinned.extend(item.get("evidence_refs", []))
        overview = [ref for ref in current
                    if self.state["evidence"][ref]["kind"] in {"page", "coverage"}]
        preferred = list(dict.fromkeys([*overview, *self._result_refs(), *pinned, *current]))
        preferred = [ref for ref in preferred if ref in self.state["evidence"]]
        order = list(dict.fromkeys([*preferred, *reversed(self.state["evidence"])]))
        order = [ref for ref in order if ref in self.state["evidence"]]
        offset = self.state["evidence_offset"]
        # Do not pad a useful first slice with obsolete copies of current
        # controls. Historical versions remain available through inspect.
        size = min(EVIDENCE_SLICE, len(preferred)) if offset == 0 else EVIDENCE_SLICE
        refs = order[offset:offset + size]
        links = self._observed_links()
        payload = {
            **{key: deepcopy(self.state[key]) for key in (
                "goal", "plan", "requirements", "loops", "items", "last_result", "page", "feedback",
            )},
            "evidence": [self.state["evidence"][ref] for ref in refs],
            "current_evidence_refs": [ref for ref in refs if ref in current],
            "evidence_offset": offset, "evidence_total": len(order),
            "next_evidence_offset": offset + len(refs) if offset + len(refs) < len(order) else None,
            "evidence_omitted": self.state["evidence_omitted"], "metrics": self._metrics(),
            "limits": asdict(self.limits), "corrections": self.state["corrections"],
            "unresolved_attempts": self.store.attempts(unresolved_only=True),
            "validation_error": deepcopy(self.state.get("validation_error")),
            "goal_coverage": self._goal_coverage(),
            "subtask_reviews": deepcopy(self.state.get("subtask_reviews", {})),
            "observed_links": links[:OBSERVED_LINK_LIMIT],
            "observed_links_total": len(links),
            "observed_links_omitted": max(0, len(links) - OBSERVED_LINK_LIMIT),
            "feedback_reasons": self._feedback_reasons(),
        }
        # Fact count alone does not bound text-heavy pages. Reduce the current
        # slice, report its exact continuation, and retain all facts locally.
        self._trim_payload(payload)
        refs = [fact["ref"] for fact in payload["evidence"]]
        payload["available_actions"] = ["delegate", "finish", "feedback"]
        if payload["next_evidence_offset"] is not None:
            payload["available_actions"].append("inspect")
        self.state["sent_refs"] = list(set(self.state["sent_refs"]) | set(refs)
                                      | {link["ref"] for link in payload["observed_links"]})
        return payload

    def _refs(self, refs, *, required=False, after=None):
        if required and not refs:
            raise InvalidSupervisorDecision("Positive evidence is required")
        for ref in refs:
            if ref not in self.state["evidence"] or ref not in self.state["sent_refs"]:
                raise InvalidSupervisorDecision("Evidence reference was not supplied")
        if after is not None and not any(self.state["evidence"][ref]["observed_at"] > after for ref in refs):
            raise InvalidSupervisorDecision("New postcondition evidence is required")
        return refs

    def _item_for_ref(self, ref):
        if ref in self.state.get("item_aliases", {}):
            return self.state["item_aliases"][ref]
        if ref in self.state["items"]:
            return ref
        fact = self._item_fact(ref)
        href = fact.get("href")
        if (fact.get("kind") != "control" or not isinstance(href, str) or fact.get("href_truncated")
                or urlparse(href).scheme not in {"http", "https"}):
            raise InvalidSupervisorDecision("Item needs an observed complete HTTP link")
        for item_id, item in self.state["items"].items():
            if href in [item["url"], *item.get("aliases", [])]:
                return item_id
        return hashlib.sha256(href.encode()).hexdigest()[:24]

    def _item_fact(self, ref):
        # A URL is usable only as an exact alias for a complete link that was
        # actually supplied. The model cannot introduce a navigation target.
        if ref not in self.state["evidence"]:
            fact = next((fact for key, fact in self.state["evidence"].items()
                         if key in self.state["sent_refs"] and fact.get("kind") == "control"
                         and fact.get("href") == ref and not fact.get("href_truncated")), None)
            if fact is not None:
                ref = fact["ref"]
        self._refs([ref], required=True)
        return self.state["evidence"][ref]

    def _register_item(self, ref, loop_id):
        item_id = self._item_for_ref(ref)
        if item_id not in self.state["items"]:
            fact = self._item_fact(ref)
            self.state["items"][item_id] = {
                "item_id": item_id, "url": fact["href"], "loop_id": loop_id,
                "source_ref": fact["ref"], "status": "pending", "evidence_refs": [],
                "started_observation": None, "visited_refs": [], "navigation_destinations": {}, "aliases": [],
            }
        elif self.state["items"][item_id]["loop_id"] != loop_id:
            raise InvalidSupervisorDecision("An item cannot move between loops")
        return item_id

    def _apply_review(self, decision):
        old_requirements = {item["id"]: item for item in self.state["requirements"]}
        new_requirements = {item["id"]: item for item in decision["requirements"]}
        if (len(new_requirements) != len(decision["requirements"])
                or not old_requirements.keys() <= new_requirements.keys()):
            raise InvalidSupervisorDecision("Requirements cannot be removed or duplicated")
        progress = False
        for key, item in new_requirements.items():
            if item["goal_quote"] not in self.state["goal"]:
                raise InvalidSupervisorDecision("Requirement must quote the original goal")
            old = old_requirements.get(key)
            if old and old["goal_quote"] != item["goal_quote"]:
                raise InvalidSupervisorDecision("Requirement identity changed")
            self._refs(item["evidence_refs"], required=item["status"] == "satisfied")
            progress |= item["status"] == "satisfied" and (not old or old["status"] != "satisfied")
        for loop in decision["loops"]:
            if loop["goal_quote"] not in self.state["goal"]:
                raise InvalidSupervisorDecision("Loop must quote the original goal")
            old = self.state["loops"].get(loop["id"])
            if old and old["goal_quote"] != loop["goal_quote"]:
                raise InvalidSupervisorDecision("Loop identity changed")
            self._refs(loop["evidence_refs"], required=loop["exhausted"])
            for ref in loop["item_refs"]:
                self._register_item(ref, loop["id"])
            self.state["loops"][loop["id"]] = loop
        for review in decision["item_reviews"]:
            if review["item_id"] not in self.state["items"]:
                raise InvalidSupervisorDecision("Unknown item")
            item = self.state["items"][review["item_id"]]
            completed = review["status"] == "completed"
            if item["status"] == "completed" and not completed:
                raise InvalidSupervisorDecision("Completed items cannot be reset for execution")
            if completed and item["started_observation"] is None:
                raise InvalidSupervisorDecision("An unvisited item cannot be complete")
            self._refs(review["evidence_refs"], required=completed,
                       after=item["started_observation"] if completed else None)
            destination_ref = review.get("destination_ref")
            if destination_ref:
                self._refs([destination_ref], required=True, after=item["started_observation"])
                if destination_ref not in item.get("navigation_destinations", {}):
                    raise InvalidSupervisorDecision("Redirect destination was not observed after opening this item")
                destination = item["navigation_destinations"][destination_ref]
                for other_id, other in list(self.state["items"].items()):
                    if other_id == item["item_id"] or destination not in [other["url"], *other.get("aliases", [])]:
                        continue
                    if other["started_observation"] is not None or other["loop_id"] != item["loop_id"]:
                        raise InvalidSupervisorDecision("Redirect identity conflicts with previously attempted work")
                    self.state.setdefault("item_aliases", {})[other_id] = item["item_id"]
                    del self.state["items"][other_id]
                item.setdefault("aliases", []).append(destination)
                item.setdefault("visited_refs", []).append(destination_ref)
            if completed:
                if not item.get("visited_refs"):
                    raise InvalidSupervisorDecision("The item destination was never observed")
                destinations = {item["url"], *item.get("aliases", [])}
                if not any(self.state["evidence"][ref].get("observed_url") in destinations
                           for ref in review["evidence_refs"]):
                    raise InvalidSupervisorDecision("Completion needs evidence from the item destination")
            progress |= completed and item["status"] != "completed"
            item.update(status=review["status"], evidence_refs=review["evidence_refs"])
        unresolved = {attempt["id"]: attempt for attempt in self.store.attempts(unresolved_only=True)}
        for review in decision["reconcile_attempts"]:
            if review["attempt_id"] not in unresolved:
                raise InvalidSupervisorDecision("Unknown uncertain attempt")
            floor = self.state["attempt_observations"].get(review["attempt_id"], self.state["observation_seq"] - 1)
            self._refs(review["evidence_refs"], required=True, after=floor)
        review = decision.get("subtask_review")
        if review:
            last = self.state.get("last_result") or {}
            if review["delegation_id"] != last.get("delegation_id"):
                raise InvalidSupervisorDecision("Subtask review must identify the last delegation")
            satisfied = review["status"] == "satisfied"
            self._refs(review["evidence_refs"], required=satisfied,
                       after=last.get("started_observation") if satisfied else None)
            reviews = self.state.setdefault("subtask_reviews", {})
            previous = reviews.get(review["delegation_id"], {})
            progress |= satisfied and previous.get("status") != "satisfied"
            reviews[review["delegation_id"]] = {**review, "task": deepcopy(last.get("task"))}
        self.state["requirements"] = decision["requirements"]
        self.state["plan"] = decision["plan"]
        if progress:
            self.state["progress_revision"] += 1
            self.state["corrections"] = 0
        return progress

    def _validate_action(self, decision, payload):
        """Validate the entire intent before any journal reconciliation is committed."""
        action = decision["action"]
        if action in {"delegate", "finish"} and not self._goal_coverage()["complete"]:
            raise InvalidSupervisorDecision(
                "Requirements omit parts of the original goal; include every requested outcome and constraint",
            )
        if action == "inspect":
            expected = payload["next_evidence_offset"]
            if expected is None:
                raise InvalidSupervisorDecision(
                    "All retained evidence has been supplied; inspect cannot fetch new facts",
                )
        elif action == "finish":
            self._refs(decision["evidence_refs"], required=True)
            reconciled = {item["attempt_id"] for item in decision["reconcile_attempts"]}
            if (any(item["status"] != "satisfied" for item in self.state["requirements"])
                    or any(not loop["exhausted"] for loop in self.state["loops"].values())
                    or any(item["status"] != "completed" for item in self.state["items"].values())
                    or any(attempt["id"] not in reconciled
                           for attempt in self.store.attempts(unresolved_only=True))):
                raise InvalidSupervisorDecision("Completion is not supported for all outcomes")
        elif action == "delegate":
            task = decision["task"]
            if task is None:
                raise InvalidSupervisorDecision("Delegation needs a subtask")
            if task.get("scope") == "item":
                if not task.get("item_ref") or task.get("loop_id") not in self.state["loops"]:
                    raise InvalidSupervisorDecision("Item work requires an observed item_ref and a registered loop_id")
                self._register_item(task["item_ref"], task["loop_id"])
            if task["item_ref"]:
                item_id = self._item_for_ref(task["item_ref"])
                if item_id not in self.state["items"] or self.state["items"][item_id]["status"] == "completed":
                    raise InvalidSupervisorDecision("Delegate only a registered incomplete item")
        elif action == "feedback":
            reason = decision.get("feedback_reason")
            blocked = decision.get("blocked_requirement_ids", [])
            intervention = decision.get("user_intervention", "")
            # Real intent tools require both fields. Legacy injected policies
            # remain compatible; whenever supplied, the justification is checked.
            if blocked or intervention:
                if not blocked or not intervention.strip():
                    raise InvalidSupervisorDecision(
                        "Feedback needs pending requirements and a specific user intervention",
                    )
                requirements = {item["id"]: item for item in self.state["requirements"]}
                if any(key not in requirements or requirements[key]["status"] == "satisfied" for key in blocked):
                    raise InvalidSupervisorDecision(
                        "Feedback must concern known pending requirements; continue after completed work",
                    )
            if reason == "provider_error" and not (self.state.get("last_result") or {}).get("provider_error"):
                raise InvalidSupervisorDecision("No worker provider error is recorded")
            if reason == "uncertain_effect" and not self.store.attempts(unresolved_only=True):
                raise InvalidSupervisorDecision("No uncertain browser attempt exists; evaluate the observed outcome")
            if reason == "recovery_exhausted" and self.state["corrections"] < 2:
                raise InvalidSupervisorDecision(
                    "Recovery is not exhausted; use evidence to continue or correct the subtask",
                )
            if reason == "missing_user_information" and not decision.get("missing_information", "").strip():
                raise InvalidSupervisorDecision("Identify the missing user information, not a browser fact to verify")

    def _decide(self):
        self._save()
        reason = self._budget_reason(execution=False)
        if reason:
            self._pause(reason, "Se ha alcanzado un límite global. El progreso parcial está guardado.")
            return
        payload = self._payload()
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 128 * 1024:
            self._pause("supervisor_context_budget", "El contexto supera el límite. Reduce el alcance de la tarea.")
            return
        self.state["supervisor_calls"] += 1
        self._save()
        decision = None
        try:
            http_before = getattr(self.supervisor, "http_attempts", 0)
            try:
                if isinstance(self.supervisor, SupervisorPolicy):
                    self.supervisor.register_trace_secrets(self.trace)
                with self.trace.call(
                    "supervisor", "legacy_review",
                    semantic_intent="Supervisor revisa la evidencia disponible y decide el siguiente paso.",
                    payload=payload,
                ) as call:
                    self._last_supervisor_call_id = call.call_id
                    answer = (self.supervisor.invoke(payload) if hasattr(self.supervisor, "invoke")
                              else self.supervisor(payload))
                    call.result(answer,
                                semantic_summary="Supervisor terminó de revisar la evidencia y devolvió su decisión.",
                                usage=getattr(self.supervisor, "last_usage", None))
            finally:
                if isinstance(self.supervisor, SupervisorPolicy):
                    self.state["supervisor_http_attempts"] += self.supervisor.http_attempts - http_before
            decision = SupervisorDecision.model_validate(answer).model_dump()
            # Validate on a copy; malformed model output must not partly alter progress.
            saved = deepcopy(self.state)
            try:
                progress = self._apply_review(decision)
                self._validate_action(decision, payload)
            except Exception:
                self.state = saved
                raise
            for review in decision["reconcile_attempts"]:
                self.store.reconcile(review["attempt_id"], review["evidence_refs"])
            self.state.pop("validation_error", None)
            self.state["validation_retries"] = 0
            self._save()
            if self.state["elapsed_seconds"] >= self.limits.max_seconds:
                self._pause("time_budget", "Se ha alcanzado el límite de tiempo activo.")
                return
            action = decision["action"]
            if action == "inspect":
                self.state["evidence_offset"] = payload["next_evidence_offset"]
            elif action == "feedback":
                self._pause("supervisor_feedback", decision["question"] or "¿Cómo quieres continuar?")
            elif action == "finish":
                self._refs(decision["evidence_refs"], required=True)
                if (any(item["status"] != "satisfied" for item in self.state["requirements"])
                        or any(not loop["exhausted"] for loop in self.state["loops"].values())
                        or any(item["status"] != "completed" for item in self.state["items"].values())
                        or self.store.attempts(unresolved_only=True)):
                    raise InvalidSupervisorDecision("Completion is not supported for all outcomes")
                self.state.update(status="done", stop_reason=None, question="")
                self.state["verification"] = {
                    "status": "satisfied", "method": "supervisor", "evidence_refs": decision["evidence_refs"],
                    "requirements": deepcopy(self.state["requirements"]),
                }
                LOG.info("Finalizado: objetivo verificado por el supervisor",
                         extra={"agentrun_agent": "supervisor"})
            elif action == "delegate":
                if not decision["task"]:
                    raise InvalidSupervisorDecision("Delegation needs a subtask")
                if self.store.attempts(unresolved_only=True):
                    self._pause("uncertain_action",
                                "Hay una acción con resultado incierto. Necesito evidencia o feedback.")
                    return
                reason = self._budget_reason()
                # The current supervisor call is allowed to delegate at its
                # own last-call boundary; worker budgets remain independently enforced.
                if reason and reason != "supervisor_budget":
                    self._pause(reason, "Se ha alcanzado un límite global. El progreso parcial está guardado.")
                    return
                last = self.state["last_result"]
                accumulated_progress = self.state["progress_revision"] > self.state["delegated_progress_revision"]
                if last and last["status"] != "yielded" and not (progress or accumulated_progress):
                    if self.state["corrections"] >= 2:
                        self._pause("no_progress", "Dos correcciones no han resuelto el bloqueo. ¿Qué debo cambiar?")
                        return
                    self.state["corrections"] += 1
                task = decision["task"]
                item_id = self._item_for_ref(task["item_ref"]) if task["item_ref"] else None
                if item_id:
                    if item_id not in self.state["items"]:
                        raise InvalidSupervisorDecision("Register the observed item in its loop before delegating")
                    item = self.state["items"][item_id]
                    if item["status"] == "completed":
                        raise InvalidSupervisorDecision("Completed items cannot be delegated again")
                    item.update(status="active")
                    if item["started_observation"] is None:
                        item["started_observation"] = self.state["observation_seq"]
                self.state["active_item"] = item_id
                self.state["delegation_sequence"] += 1
                delegation = {
                    "id": f"{self.run_id}:{self.state['delegation_sequence']}",
                    "input": task,
                    "progress": self.state["progress_revision"] > self.state["delegated_progress_revision"],
                    "started_observation": self.state["observation_seq"],
                    "started_step": len(self.worker.state["history"]),
                }
                self.store.save_delegation(delegation["id"], task)
                self.state["pending_delegation"] = delegation
                self.state["delegated_progress_revision"] = self.state["progress_revision"]
            if isinstance(self.supervisor, SupervisorPolicy):
                self.state["supervisor_usage"].append(self.supervisor.last_usage)
            self._save()
        except ProviderError as error:
            if error.metadata.get("provider_error_kind") == "invalid_response":
                self._invalid_decision(
                    "Invalid intent tool response; call one tool with its required arguments, "
                    "including task to delegate",
                )
                return
            self.state["provider_error"] = error.metadata
            self._pause("supervisor_provider_error",
                        "No se pudo consultar al supervisor. Revisa el proveedor y reanuda.")
        except (ValueError, TypeError, KeyError) as error:
            # The rejected intent has caused no browser input. Give the model
            # one bounded correction with a safe, specific validation reason.
            if isinstance(error, ValidationError):
                detail = "Invalid decision schema: " + ", ".join(
                    ".".join(str(part) for part in issue["loc"]) + " (" + issue["type"] + ")"
                    for issue in error.errors(include_input=False, include_context=False)[:6]
                )
            elif isinstance(error, InvalidSupervisorDecision):
                detail = str(error)[:240]
            else:
                detail = "Invalid decision structure"
            self._invalid_decision(detail, rejected_decision=decision)

    def _invalid_decision(self, detail, *, rejected_decision=None, output_token_phase=None):
        last = self.state.get("last_result") or {}
        self.state["validation_error"] = {
            "reason": detail, "browser_action_executed": False,
            "rejected_decision": deepcopy(rejected_decision),
            "last_executed_delegation_id": last.get("delegation_id"),
            "instruction": "Repair only the rejected next decision. last_result is an earlier executed delegation.",
        }
        if self.state.get("contract_version") == 2:
            phase = self.state.get("contract_phase")
            repair_instructions = {
                "initialize": (
                    "Correct the indicated fields in rejected_decision and return a complete initialize_plan response. "
                    "Preserve the other goal-derived requirements, instructions and constraints. Every goal_quote "
                    "must be one exact, contiguous substring of goal; never join separate excerpts. "
                    "No browser work was dispatched for this rejected response."
                ),
                "audit": (
                    "Correct the indicated fields in rejected_decision and return a complete audit_plan response. "
                    "Any rejected replacement_contract was not installed. Preserve its other goal-derived "
                    "requirements, instructions and constraints. Every goal_quote must be one exact, contiguous "
                    "substring of goal; never join separate excerpts. No browser work was dispatched for this "
                    "rejected response."
                ),
                "review": (
                    "Correct the indicated fields in rejected_decision and return a complete review_progress response. "
                    "The rejected review was not applied. last_result is an earlier executed delegation; "
                    "use its evidence without repeating its browser actions."
                ),
            }
            if phase in repair_instructions:
                self.state["validation_error"].update(phase=phase, instruction=repair_instructions[phase])
        if output_token_phase is not None:
            # The retry allowance and its output budget must survive a restart
            # together, including a crash during this first save.
            self.state["validation_error"].update(output_token_limit=True, phase=output_token_phase)
        self.trace.emit("review.rejected", component="supervisor",
                        call_id=getattr(self, "_last_supervisor_call_id", None),
                        payload=self.state["validation_error"])
        if not self.state.get("validation_retries"):
            self.state["validation_retries"] = 1
            LOG.info("Supervisor: corrigiendo respuesta (%s)", detail,
                     extra={"agentrun_agent": "supervisor"})
            self._save()
        else:
            # A malformed model response is not a question the user can answer.
            self.state.update(status="unverified", stop_reason="supervisor_invalid_response", question="")
            self.state["verification"] = {"status": "incomplete"}
            LOG.info("Detenido: el supervisor no corrigió su respuesta (%s)", detail,
                     extra={"agentrun_agent": "supervisor"})
            self._save()
            self._active_start = None

    def _worker_result(self, status=None, reason=None):
        worker = self.worker.state if self.worker else self.state.get("worker", {})
        pending = self.state.get("pending_delegation") or {}
        history = worker.get("history", [])[pending.get("started_step", 0):][-16:]
        compact = execution_history(history)
        for record, entry in zip(compact, history, strict=True):
            record.update({key: entry[key] for key in ("url", "attempt_id", "page_changed") if key in entry})
            effects = entry.get("effects", [])
            record["effects"] = []
            for effect in effects[:6]:
                brief = {}
                truncated = []
                for key, value in effect.items():
                    if isinstance(value, str) and len(value) > 512:
                        brief[key] = value[:512]
                        truncated.append(key)
                    else:
                        brief[key] = value
                if truncated:
                    brief["truncated_fields"] = truncated
                record["effects"].append(brief)
            if len(effects) > 6:
                record["effects_omitted"] = len(effects) - 6
        return {
            "delegation_id": pending.get("id"), "task": pending.get("input"),
            "status": status or ("claimed_done" if worker.get("status") == "done" else worker.get("status", "blocked")),
            "stop_reason": reason or worker.get("stop_reason"),
            "item_id": self.state.get("active_item"), "page": deepcopy(self.state.get("page", {})),
            "history": compact,
            "evidence_refs": list(self.state["current_evidence_refs"]),
            "observation_seq": self.state["observation_seq"], "metrics": self._metrics(),
            "started_observation": pending.get("started_observation"),
        }

    def _delegate(self):
        pending = self.state["pending_delegation"]
        self.worker._trace_fields = {"delegation_id": pending["id"], "item_id": self.state.get("active_item"),
                                     "observation_seq": self.state["observation_seq"]}
        record = self.store.get_delegation(pending["id"])
        if record and record.get("result") is not None:
            self.state["last_result"] = record["result"]
            self.state["pending_delegation"] = None
            self._save()
            self.trace.emit("delegation.recovered", component="coordinator", delegation_id=pending["id"],
                            payload=record["result"])
            return
        task = pending["input"]
        active = self.state["items"].get(self.state.get("active_item"), {})
        instruction = (
            "CURRENT SUBTASK:\n" + task["instruction"]
            + "\nSUCCESS CRITERIA:\n" + "\n".join(task["success_criteria"])
            + ("\nOBSERVED ITEM URL:\n" + active["url"] if active else "")
            + "\nReturn DONE when this subtask appears complete; the supervisor verifies the result."
        )
        discovering = task.get("scope") == "discover"
        known_links = {link["href"] for link in self._observed_links()} if discovering else set()
        if discovering:
            instruction += (
                "\nOnly expose the next portion of relevant links with an observed navigation or scroll. "
                "The supervisor collects and registers links from snapshots; you cannot register items. "
                "Return control when new links are visible. Do not process or submit an item."
            )
        self.worker.state["started_at"] = time.perf_counter() - self.state["elapsed_seconds"]
        self.trace.emit("delegation.started", component="worker", delegation_id=pending["id"],
                        item_id=self.state.get("active_item"), payload={"instruction": instruction, "task": task})
        try:
            log_delegation(task, item_url=active.get("url"), delegation_id=pending["id"])
            turns = self.worker.run_subtask(
                instruction, progress_feedback={
                    "success_criteria": task["success_criteria"], "active_item": active,
                    "completed_items": [item["url"] for item in self.state["items"].values()
                                        if item["status"] == "completed"],
                },
                verified_progress=pending["progress"], action_limit=self.limits.delegation_actions,
                decision_limit=self.limits.delegation_decisions,
            )
            discovered = False
            try:
                for _ in turns:
                    self._save()
                    if discovering and any(link["href"] not in known_links for link in self._observed_links()):
                        discovered = True
                        break
            finally:
                turns.close()
            result = (self._worker_result("yielded", "observed_links_discovered")
                      if discovered else self._worker_result())
        except Exception as error:
            result = self._worker_result("blocked", "uncertain_action" if self.store.attempts(unresolved_only=True)
                                         else "worker_error")
            result["worker_error_type"] = type(error).__name__[:80]
            if isinstance(error, ProviderError):
                result["provider_error"] = error.metadata
                LOG.info("Jev: error del proveedor (%s%s)",
                         error.metadata.get("provider_error_kind", "unknown"),
                         f", HTTP {error.metadata['provider_http_status']}"
                         if error.metadata.get("provider_http_status") else "",
                         extra={"agentrun_agent": "jev"})
            else:
                LOG.info("Jev: fallo local (%s)", result["worker_error_type"],
                         extra={"agentrun_agent": "jev"})
            # Recover evidence using reads only. Never replay a failed browser input.
            try:
                self._record_page(self.browser.observe(screenshot=False))
                result["page"] = deepcopy(self.state["page"])
                result["evidence_refs"] = list(self.state["current_evidence_refs"])
            except Exception:
                pass
        self.store.save_delegation(pending["id"], pending["input"], result)
        self.state["last_result"] = result
        self.state["pending_delegation"] = None
        if self.store.attempts(unresolved_only=True) and self.state.get("active_item"):
            self.state["items"][self.state["active_item"]]["status"] = "uncertain"
        self._save()
        self.trace.emit("delegation.completed", component="worker", delegation_id=pending["id"],
                        item_id=self.state.get("active_item"), status=result["status"], payload=result)

    @property
    def graph(self):
        if not hasattr(self, "_graph"):
            graph = StateGraph(GraphState)

            def decide(_state):
                with self.trace.bind():
                    self._decide()
                return {"supervision": deepcopy(self.state)}

            def delegate(_state):
                with self.trace.bind():
                    self._delegate()
                return {"supervision": deepcopy(self.state)}

            def wait_provider(_state):
                # Only the v2 coordinator schedules this state. No worker input
                # is dispatched while its supervisor provider is cooling down.
                with self.trace.bind():
                    self._wait_provider_retry()
                return {"supervision": deepcopy(self.state)}

            graph.add_node("supervisor", decide)
            graph.add_node("jev", delegate)
            graph.add_node("provider_wait", wait_provider)
            graph.add_edge(START, "supervisor")
            graph.add_conditional_edges(
                "supervisor", lambda _state: END if self.state["status"] in TERMINAL else (
                    "provider_wait" if self.state["status"] == "waiting_provider" else (
                        "jev" if self.state["pending_delegation"] else "supervisor"
                    )
                ), [END, "jev", "supervisor", "provider_wait"],
            )
            graph.add_edge("jev", "supervisor")
            graph.add_conditional_edges(
                "provider_wait", lambda _state: END if self.state["status"] in TERMINAL else "supervisor",
                [END, "supervisor"],
            )
            self._graph = graph.compile(checkpointer=self.store.checkpointer, name="SupervisedAgentRun")
        return self._graph

    def run(self, *, config=None):
        if self._closed:
            raise ValueError("Agent has been disconnected")
        if self._running:
            raise RuntimeError("This run is already active")
        if self.state["status"] in TERMINAL:
            yield self.snapshot()
            return
        self._running = True
        self._active_start = time.perf_counter()
        try:
            graph_config = {**(config or {}), "recursion_limit": self.limits.max_supervisor_calls * 3 + 5,
                            "configurable": {**(config or {}).get("configurable", {}), "thread_id": self.run_id}}
            # Re-enter through the supervisor after process restart. Browser nodes
            # are never replayed from checkpoints; durable tool records reconcile work.
            for _ in self.graph.stream({"supervision": deepcopy(self.state)}, config=graph_config,
                                       stream_mode="updates", durability="sync"):
                self.trace.emit("run.progress", component="coordinator", status=self.state["status"],
                                stop_reason=self.state.get("stop_reason"), metrics=self._metrics(), payload={
                                    "active_work": self.state.get("active_work"),
                                    "verification": self.state["verification"],
                                    "requirements": self.state["requirements"],
                                    "provider_retry": self.state.get("provider_retry"),
                                    "provider_wait_seconds": self.state.get("provider_wait_seconds", 0),
                                })
                yield self.snapshot()
        except BaseException as error:
            self.trace.emit("run.failed" if isinstance(error, Exception) else "run.interrupted",
                            component="coordinator", error_type=type(error).__name__)
            raise
        finally:
            try:
                self._save()
            finally:
                self._active_start = None
                self._running = False
                self.trace.emit("run.yielded", component="coordinator", status=self.state["status"],
                                stop_reason=self.state.get("stop_reason"), metrics=self._metrics())

    def disconnect(self):
        if self.browser is not None:
            self.browser.disconnect()
        self._close_store()

    def close(self):
        if self.browser is not None:
            self.browser.close()
        self._close_store()

    def _close_store(self):
        if not self._closed:
            self._save()
            self.store.close()
            self._closed = True
            self.trace.close(status=self.state.get("status"), stop_reason=self.state.get("stop_reason"))

    def _release(self):
        try:
            self.close() if self.close_tab_on_exit else self.disconnect()
        finally:
            if not self._closed:
                self.store.close()
                self._closed = True
                self.trace.close(status=self.state.get("status"), stop_reason=self.state.get("stop_reason"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._release()


class SupervisedAgent(CoordinatorMixin, LegacySupervisedAgent):
    """Versioned plan, deterministic work queue and strictly typed model reviews."""
