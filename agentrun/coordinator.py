"""Version two coordination: models review outcomes; code owns the work queue.

This mixin reuses the browser journal and bounded evidence reader. Its model
responses never select the next item or replay an already verified item step.
"""

import json
import logging
import math
import time
from copy import deepcopy

from pydantic import ValidationError

from AgentBrowser.evidence import _controls
from AgentBrowser.model import ProviderError

from .supervisor_policy import SupervisorPolicy

LOG = logging.getLogger("agentrun")
MAX_PROVIDER_RETRIES = 2
MAX_PROVIDER_RETRY_DELAY = 300
MAX_PROVIDER_WAIT_SECONDS = MAX_PROVIDER_RETRIES * MAX_PROVIDER_RETRY_DELAY
PROVIDER_CLOCK_RESOLUTION = 1e-6  # UTC checkpoints lose sub-microsecond monotonic precision.
DEFAULT_PROVIDER_RETRY_DELAY = 120
MAX_AUDIT_REPLACEMENTS = 2
MAX_DISCOVERY_RECOVERIES = 2
LINK_CATALOG_RESERVE_BYTES = 24 * 1024


class CoordinatorMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ensure_contract_state()
        self._save()

    def _ensure_contract_state(self):
        defaults = {
            "contract_version": 2, "contract": None, "contract_phase": "initialize",
            "task_runs": {}, "active_work": None, "read_recoveries": 0,
            "coordinator_stalls": 0, "audit_replacements": 0,
            "audit_history": [], "plan_revision": None,
            "discovery_recoveries": {},
            "provider_retry_count": 0, "provider_retry": None, "provider_wait_seconds": 0.0,
        }
        for key, value in defaults.items():
            self.state.setdefault(key, deepcopy(value))
        # Preserve an invalid marker rather than resetting corrupted counters.
        # Non-finite numbers cannot be written to the strict JSON journal.
        for container, keys in (
            (self.state, ("provider_retry_count", "provider_wait_seconds", "provider_retry")),
            (self.state.get("provider_retry"), ("next_retry_at", "delay_seconds", "wait_accounted_at")),
        ):
            if isinstance(container, dict):
                for key in keys:
                    value = container.get(key)
                    if type(value) is float and not math.isfinite(value):
                        container[key] = "invalid_non_finite_number"

    def _load_state(self):
        state = self.store.load_state(migrate=True)
        if state is not None:
            self.state = state
            self._ensure_contract_state()
        return state

    def _record_page(self, page):
        # JSON object ordering is not observation ordering after a restart.
        # Retain first-seen URL order separately, before links are registered
        # as relevant items by the supervisor. Older states use recorded
        # observation timestamps and within-page order as their fallback.
        order = self.state.get("observed_url_order")
        if order is None:
            facts = sorted(self.state["evidence"].values(), key=lambda fact: (
                fact.get("observed_at", 0), fact.get("observed_order", 0),
            ))
            order = self.state["observed_url_order"] = list(dict.fromkeys(
                fact["href"] for fact in facts
                if fact.get("kind") == "control" and fact.get("href") and not fact.get("href_truncated")
            ))
        seen = set(order)
        for control in _controls(page).values():
            href = control.get("href")
            if href and not control.get("href_truncated") and href not in seen:
                order.append(href)
                seen.add(href)
        # The base observer saves evidence and this order in the same state.
        super()._record_page(page)

    def _accept_feedback(self, feedback):
        super()._accept_feedback(feedback)
        self.state["coordinator_stalls"] = 0
        # The next review sees the answer; neither completed steps nor budgets
        # are reset. A pending current step may be repaired using this answer.

    def snapshot(self):
        result = super().snapshot()
        result.update({key: deepcopy(self.state.get(key)) for key in (
            "contract_version", "contract_phase", "task_runs", "active_work", "provider_error",
            "provider_retry_count", "provider_retry", "provider_wait_seconds",
        )})
        result["provider_wait_limit_seconds"] = MAX_PROVIDER_WAIT_SECONDS
        return result

    def _metrics(self):
        return {**super()._metrics(), "supervisor_phase_calls": deepcopy(
            self.state.get("supervisor_phase_calls", {}),
        )}

    def _stop(self, reason, *, status="blocked"):
        self.state.update(status=status, stop_reason=reason, question="")
        self.state["verification"] = {"status": "incomplete"}
        LOG.info("Detenido: %s", reason, extra={"agentrun_agent": "supervisor"})
        self._save()
        self.trace.emit("run.stopped", component="coordinator", status=status, stop_reason=reason,
                        call_id=getattr(self, "_last_supervisor_call_id", None),
                        payload={"provider_error": self.state.get("provider_error"), "metrics": self._metrics()})
        self._active_start = None

    def _pause(self, reason, question):
        # Only model-validated questions reach input(). Technical conditions
        # keep their machine-readable reason without pretending an answer helps.
        if reason == "supervisor_feedback":
            return super()._pause(reason, question)
        self._stop(reason, status="unverified" if reason == "uncertain_action" else "blocked")

    def _observed_links(self):
        links = super()._observed_links()
        registered = {url for item in self.state["items"].values()
                      for url in [item["url"], *item.get("aliases", [])]}
        positions = {url: index for index, url in enumerate(self.state.get("observed_url_order", []))}
        # Relevance is still decided by the supervisor. Prioritize unregistered
        # observed URLs without filtering them by site, label or guessed type.
        return sorted(links, key=lambda link: (
            link["href"] in registered, positions.get(link["href"], len(positions)),
        ))

    def _trim_payload(self, payload):
        # Completed reviews repeat the entire wrapped task instruction. Keep
        # outcome, criteria and provenance; the full record stays in the store.
        for review in payload["subtask_reviews"].values():
            task = review.get("task") or {}
            if "instruction" in task:
                task.pop("instruction")
                task["instruction_omitted"] = True
        # Reserve a bounded prefix of the prioritized URL catalog before
        # trimming evidence. Otherwise a growing history can erase all links.
        reserved, size = 0, 2
        for link in payload["observed_links"]:
            size += len(json.dumps(link, ensure_ascii=False).encode()) + 2
            if size > LINK_CATALOG_RESERVE_BYTES:
                break
            reserved += 1
        while len(json.dumps(payload, ensure_ascii=False).encode()) > 120 * 1024:
            if len(payload["observed_links"]) > reserved:
                payload["observed_links"].pop()
                payload["observed_links_omitted"] += 1
            elif payload["evidence"]:
                payload["evidence"].pop()
                refs = {fact["ref"] for fact in payload["evidence"]}
                payload["current_evidence_refs"] = [ref for ref in payload["current_evidence_refs"] if ref in refs]
                payload["next_evidence_offset"] = payload["evidence_offset"] + len(payload["evidence"])
            else:
                # Do not silently sacrifice the reserved links to oversized
                # static context. The existing hard context guard stops it.
                break

    def _discovery_progress(self):
        return {loop["id"]: {
            "limit": loop["limit"],
            "registered": sum(item["loop_id"] == loop["id"] for item in self.state["items"].values()),
            "completed": sum(item["loop_id"] == loop["id"] and item["status"] == "completed"
                             for item in self.state["items"].values()),
            "recoveries": self.state["discovery_recoveries"].get(loop["id"], 0),
        } for loop in (self.state["contract"] or {}).get("loops", [])}

    def _payload(self):
        self._ensure_contract_state()
        previously_sent = set(self.state["sent_refs"])
        payload = super()._payload()
        payload.update({
            "contract_version": 2, "phase": self.state["contract_phase"],
            "contract": deepcopy(self.state["contract"]),
            "task_runs": deepcopy(self.state["task_runs"]),
            "active_work": deepcopy(self.state["active_work"]),
            "read_recoveries": self.state["read_recoveries"],
            "migration_pending": self.state.get("migration_pending", False),
            "migration_orphan_attempts": self.state.get("migration_orphan_attempts", []),
            "discovery_progress": self._discovery_progress(),
        })
        if self.state["contract_phase"] in {"initialize", "audit"}:
            payload["audit_history"] = deepcopy(self.state["audit_history"])
            payload["plan_revision"] = deepcopy(self.state["plan_revision"])
        actions = ["continue", "feedback", "stop"]
        if (self.state["active_work"] and (self.state.get("last_result") or {}).get("task")
                and self.state["corrections"] < 2):
            actions.append("repair")
        if payload["next_evidence_offset"] is not None:
            actions.append("inspect")
        if self.state["read_recoveries"] < 2:
            actions.append("observe")
        payload["available_actions"] = actions
        payload["feedback_reasons"] = ["missing_user_information", "external_intervention"]
        if self.store.attempts(unresolved_only=True):
            payload["feedback_reasons"].append("uncertain_effect")
        self._trim_payload(payload)
        if not payload["evidence"] and payload["next_evidence_offset"] == payload["evidence_offset"]:
            payload["available_actions"] = [action for action in actions if action != "inspect"]
        elif payload["next_evidence_offset"] is not None and "inspect" not in actions:
            actions.append("inspect")
        self.state["sent_refs"] = sorted(previously_sent | {
            fact["ref"] for fact in [*payload["evidence"], *payload["observed_links"]]
        })
        return payload

    def _call_supervisor(self, payload):
        self.state["supervisor_calls"] += 1
        counts = self.state.setdefault("supervisor_phase_calls", {})
        counts[payload["phase"]] = counts.get(payload["phase"], 0) + 1
        self._save()
        before = getattr(self.supervisor, "http_attempts", 0)
        try:
            if isinstance(self.supervisor, SupervisorPolicy):
                self.supervisor.register_trace_secrets(self.trace)
            phase = payload["phase"]
            descriptions = {
                "initialize": "Supervisor prepara el plan: extrae requisitos, tareas y dependencias del objetivo.",
                "audit": "Supervisor audita el plan frente al objetivo antes de delegar trabajo.",
                "review": "Supervisor revisa la evidencia reciente y decide si continúa, corrige, delega o finaliza.",
            }
            if phase == "initialize" and payload.get("plan_revision"):
                descriptions["initialize"] = "Supervisor corrige el plan con las observaciones de la auditoría."
            with self.trace.call("supervisor", phase, semantic_intent=descriptions.get(
                                     phase, "Supervisor analiza el estado de la tarea."), payload=payload,
                                 phase=payload["phase"],
                                 delegation_id=(self.state.get("last_result") or {}).get("delegation_id")) as call:
                self._last_supervisor_call_id = call.call_id
                answer = (self.supervisor.invoke(payload) if hasattr(self.supervisor, "invoke")
                          else self.supervisor(payload))
                outcomes = {
                    "initialize": "Supervisor terminó de preparar el plan de trabajo.",
                    "audit": "Supervisor terminó la auditoría del plan.",
                    "review": "Supervisor devolvió una propuesta; se validará antes de aplicarla.",
                }
                if phase == "initialize" and payload.get("plan_revision"):
                    outcomes["initialize"] = "Supervisor devolvió el plan corregido; necesita validación y auditoría."
                call.result(answer, semantic_summary=outcomes.get(phase, "Supervisor terminó su revisión."),
                            usage=getattr(self.supervisor, "last_usage", None))
        finally:
            self.state["supervisor_http_attempts"] += getattr(self.supervisor, "http_attempts", 0) - before
        usage = getattr(self.supervisor, "last_usage", None)
        if usage:
            self.state["supervisor_usage"].append({**deepcopy(usage), "phase": payload["phase"]})
        return answer

    def _schedule_provider_retry(self, error):
        """Reserve a bounded supervisor-only retry; never dispatch worker input."""
        metadata = error.metadata
        if not (error.status_code == 402
                and metadata.get("provider_limit_source") == "openrouter_in_flight_budget"
                and metadata.get("provider_limit_reason") == "in_flight_budget_exhausted"):
            return False
        self._save()
        count = self.state["provider_retry_count"]
        if type(count) is not int or not 0 <= count <= MAX_PROVIDER_RETRIES or not self._valid_provider_wait():
            self._stop("supervisor_provider_retry_state_invalid")
            return True
        if self.state["provider_retry_count"] >= MAX_PROVIDER_RETRIES:
            self._stop("supervisor_provider_retry_exhausted")
            return True
        delay = metadata.get("provider_retry_after_seconds", DEFAULT_PROVIDER_RETRY_DELAY)
        if delay > MAX_PROVIDER_RETRY_DELAY:
            self._stop("supervisor_provider_retry_delay_exceeded")
            return True
        reason = self._budget_reason(execution=False)
        if reason:
            self._stop(reason)
            return True
        if delay > MAX_PROVIDER_WAIT_SECONDS - self.state["provider_wait_seconds"]:
            self._stop("supervisor_provider_wait_budget")
            return True
        started = time.time()
        self.state["provider_retry_count"] += 1
        self.state["provider_retry"] = {
            "next_retry_at": started + delay, "delay_seconds": delay, "wait_accounted_at": started,
            "phase": self.state["contract_phase"],
        }
        monotonic = time.monotonic()
        self._provider_wait_timer = {
            "pending": self.state["provider_retry"], "deadline": monotonic + delay, "accounted": monotonic,
        }
        self.state.update(status="waiting_provider", stop_reason=None, question="")
        self._save()
        self._pause_provider_clock()
        self.trace.emit("provider.retry_scheduled", component="supervisor",
                        retry_count=self.state["provider_retry_count"], **self.state["provider_retry"])
        LOG.info("Supervisor: presupuesto temporal del proveedor; reintento %s/%s en %s s.",
                 self.state["provider_retry_count"], MAX_PROVIDER_RETRIES, delay,
                 extra={"agentrun_agent": "supervisor"})
        return True

    def _valid_provider_wait(self):
        spent = self.state.get("provider_wait_seconds")
        return type(spent) in (int, float) and 0 <= spent <= MAX_PROVIDER_WAIT_SECONDS

    def _pause_provider_clock(self):
        if self._active_start is not None:
            self._provider_clock_paused = True
            self._active_start = None

    def _provider_wait_timer_for(self, pending):
        timer = getattr(self, "_provider_wait_timer", None)
        if timer is not None and timer["pending"] is pending:
            return timer
        # UTC is only a checkpoint for crossing process boundaries. Account for
        # offline time up to that deadline; a backwards clock keeps the complete
        # remaining wait instead of inventing elapsed time or extending it.
        remaining = pending["next_retry_at"] - pending["wait_accounted_at"]
        utc = time.time()
        offline = min(remaining, max(0, utc - pending["wait_accounted_at"]))
        monotonic = time.monotonic()
        pending = {**pending, "wait_accounted_at": utc, "next_retry_at": utc + remaining - offline}
        # Keep the counter and its checkpoint coherent before trace callbacks or
        # any other interruptible work. run.finally may save immediately here.
        self.state.update(provider_retry=pending, provider_wait_seconds=min(
            MAX_PROVIDER_WAIT_SECONDS, self.state["provider_wait_seconds"] + offline,
        ))
        timer = self._provider_wait_timer = {
            "pending": pending, "deadline": monotonic + remaining - offline, "accounted": monotonic,
        }
        return timer

    def _account_provider_wait(self, pending, timer):
        # A live cooldown uses monotonic time. Rewrite its UTC checkpoint from
        # the remaining duration so clock corrections cannot shorten a retry,
        # hide waiting time, or leave a misleading deadline for a later resume.
        monotonic = time.monotonic()
        until = max(timer["accounted"], min(monotonic, timer["deadline"]))
        self.state["provider_wait_seconds"] = min(MAX_PROVIDER_WAIT_SECONDS,
                                                   self.state["provider_wait_seconds"] + until - timer["accounted"])
        timer["accounted"] = until
        remaining = max(0, timer["deadline"] - monotonic)
        utc = time.time()
        pending.update(wait_accounted_at=utc, next_retry_at=utc + remaining)
        self._save()

    def _wait_provider_retry(self):
        """Keep cooldown separate from active work, then refresh with a read only."""
        self._ensure_contract_state()
        pending = self.state.get("provider_retry")
        if pending is None:
            return
        if not isinstance(pending, dict):
            self._stop("supervisor_provider_retry_state_invalid")
            return
        deadline = pending.get("next_retry_at")
        delay_seconds = pending.get("delay_seconds")
        if (type(deadline) not in (int, float) or deadline <= 0
                or type(delay_seconds) not in (int, float)
                or not 0 <= delay_seconds <= MAX_PROVIDER_RETRY_DELAY
                or pending.get("phase") != self.state["contract_phase"]
                or type(self.state["provider_retry_count"]) is not int
                or not 1 <= self.state["provider_retry_count"] <= MAX_PROVIDER_RETRIES
                or not self._valid_provider_wait()):
            self._stop("supervisor_provider_retry_state_invalid")
            return
        # Old pending retries already retain their original delay and deadline.
        # Derive their start once; never reset the cooldown on process restart.
        accounted = pending.get("wait_accounted_at", deadline - delay_seconds)
        if (type(accounted) not in (int, float)
                or not deadline - delay_seconds <= accounted <= deadline):
            self._stop("supervisor_provider_retry_state_invalid")
            return
        if (deadline - accounted > MAX_PROVIDER_WAIT_SECONDS - self.state["provider_wait_seconds"]
                + PROVIDER_CLOCK_RESOLUTION):
            self._stop("supervisor_provider_wait_budget")
            return
        pending["wait_accounted_at"] = accounted
        self._save()
        self._pause_provider_clock()
        timer = self._provider_wait_timer_for(pending)
        pending = timer["pending"]
        self.state.update(status="waiting_provider", stop_reason=None, question="")
        self.trace.emit("provider.retry_wait_started", component="supervisor",
                        retry_count=self.state["provider_retry_count"], next_retry_at=deadline,
                        provider_wait_seconds=self.state["provider_wait_seconds"])
        try:
            while True:
                self._account_provider_wait(pending, timer)
                reason = self._budget_reason(execution=False)
                if reason:
                    self._stop(reason)
                    return
                delay = max(0, timer["deadline"] - time.monotonic())
                if delay > MAX_PROVIDER_RETRY_DELAY:
                    self._stop("supervisor_provider_retry_delay_exceeded")
                    return
                if delay > MAX_PROVIDER_WAIT_SECONDS - self.state["provider_wait_seconds"] + PROVIDER_CLOCK_RESOLUTION:
                    self._stop("supervisor_provider_wait_budget")
                    return
                if not delay:
                    break
                time.sleep(min(delay, 1))
        finally:
            # Ctrl+C during sleep must persist those seconds too. Keep the active
            # clock suspended until a later invocation completes this cooldown.
            self._account_provider_wait(pending, timer)
        if getattr(self, "_provider_clock_paused", False):
            self._active_start = time.perf_counter()
            self._provider_clock_paused = False
        # The page can change during cooldown. Discard proposals and cached text,
        # retain execution history, and observe before another supervisor review.
        self.trace.emit("provider.retry_observation_started", component="supervisor")
        try:
            if self.worker is None or self.browser is None:
                raise ValueError("The original browser is unavailable")
            self.worker.pending_text = None
            self.worker.state["decision"] = None
            self.worker.state["started_at"] = time.perf_counter() - self.state["elapsed_seconds"]
            self.browser.observation_deadline = time.monotonic() + max(
                0, self.limits.max_seconds - self.state["elapsed_seconds"],
            )
            self.worker._install_page(self.worker._observe())
        except Exception as error:
            self.trace.emit("provider.retry_observation_failed", component="supervisor",
                            error_type=type(error).__name__)
            self._stop("observation_unavailable")
            return
        self._save()
        reason = self._budget_reason(execution=False)
        if reason:
            self._stop(reason)
            return
        self.state["provider_retry"] = None
        self._provider_wait_timer = None
        self.state.update(status="ready", stop_reason=None, question="")
        self._save()
        self.trace.emit("provider.retry_ready", component="supervisor",
                        retry_count=self.state["provider_retry_count"], observation_seq=self.state["observation_seq"],
                        provider_wait_seconds=self.state["provider_wait_seconds"])

    def _decide(self):
        from .supervisor_contract import validate_progress_review, validate_response

        self._ensure_contract_state()
        self._save()
        count = self.state["provider_retry_count"]
        if type(count) is not int or not 0 <= count <= MAX_PROVIDER_RETRIES or not self._valid_provider_wait():
            self._stop("supervisor_provider_retry_state_invalid")
            return
        reason = self._budget_reason(execution=False)
        if reason:
            self._stop(reason)
            return
        if self.state.get("provider_retry") is not None:
            self.state.update(status="waiting_provider", stop_reason=None, question="")
            self._save()
            self._pause_provider_clock()
            return
        payload = self._payload()
        # The evidence reader has a bounded first slice, but static state can
        # grow as well; retain the existing hard upper bound on a model input.
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 128 * 1024:
            self._stop("supervisor_context_budget")
            return
        # Retain only a schema-validated response for local correction. Raw
        # provider errors and malformed tool arguments are not repair context.
        response = None
        try:
            response = validate_response(self._call_supervisor(payload), payload["phase"])
            self.state.pop("provider_error", None)
            self._save()
            if self.state["elapsed_seconds"] >= self.limits.max_seconds:
                self._stop("time_budget")
                return
            if payload["phase"] == "initialize":
                self._initialize_contract(response["contract"])
            elif payload["phase"] == "audit":
                self._audit_contract(response)
            else:
                context = {**payload, "evidence": self.state["evidence"], "sent_refs": self.state["sent_refs"],
                           "attempt_observations": self.state["attempt_observations"]}
                review = validate_progress_review(response, context)
                saved = deepcopy(self.state)
                try:
                    self._apply_progress(review)
                    self._validate_dispatch(review, payload)
                    self._log_review_decision(review)
                    # Journal reconciliation and its verified progress must
                    # survive a crash together, before follow-up dispatch.
                    self.state.pop("validation_error", None)
                    self.state["validation_retries"] = 0
                    self._save(reconciliations=review["reconcile_attempts"])
                except Exception:
                    self.state = saved
                    raise
                self._dispatch_review(review, payload)
            self.state.pop("validation_error", None)
            self.state["validation_retries"] = 0
            self._save()
            self.trace.emit("coordinator.review_applied", component="coordinator", phase=payload["phase"],
                            call_id=getattr(self, "_last_supervisor_call_id", None),
                            progress_revision=self.state["progress_revision"], payload={
                                "response": response, "active_work": self.state["active_work"],
                                "requirements": self.state["requirements"], "task_runs": self.state["task_runs"],
                            })
        except ProviderError as error:
            self.state["provider_error"] = error.metadata
            if error.kind == "invalid_response":
                truncated = error.metadata.get("provider_diagnostic", {}).get("error_code") == "output_token_limit"
                self._invalid_decision("Supervisor output reached its token limit" if truncated
                                       else "Invalid version two supervisor tool response",
                                       output_token_phase=self.state["contract_phase"] if truncated else None)
            elif self._schedule_provider_retry(error):
                return
            else:
                if (error.metadata.get("provider_limit_source") == "openrouter_credits"
                        and error.metadata.get("provider_limit_reason") == "weight_exceeds_budget"):
                    LOG.info("Supervisor: el coste máximo estimado de la petición (prompt y tokens de salida) "
                             "supera el presupuesto del proveedor; esperar no resuelve este límite.",
                             extra={"agentrun_agent": "supervisor"})
                self._stop("supervisor_provider_error")
        except (ValueError, TypeError, KeyError) as error:
            if isinstance(error, ValidationError):
                detail = "Invalid coordinator schema: " + ", ".join(
                    ".".join(str(part) for part in issue["loc"]) + " (" + issue["type"] + ")"
                    for issue in error.errors(include_input=False, include_context=False)[:5]
                )
            else:
                # Validators generate local, bounded diagnostics; never include
                # a provider exception or a rejected field value in the log.
                detail = str(error)[:240] if isinstance(error, ValueError) else "Invalid coordinator structure"
            self._invalid_decision(detail, rejected_decision=response)

    @staticmethod
    def _log_review_decision(review):
        """Explain the validated review in terms useful to a person watching the run."""
        actions = {
            "continue": "continuar con el siguiente trabajo pendiente",
            "inspect": "leer el siguiente bloque de evidencia ya observada",
            "observe": "volver a observar la página para obtener evidencia nueva",
            "repair": "encargar a Jev que corrija el paso incompleto",
            "feedback": "pausar y pedir una respuesta al usuario",
            "stop": "detener la ejecución sin darla por verificada",
        }
        requirements = review.get("requirement_updates", [])
        satisfied = sum(update.get("status") == "satisfied" for update in requirements)
        pending = sum(update.get("status") != "satisfied" for update in requirements)
        subtask = review.get("subtask_review")
        status = subtask.get("status") if subtask else None
        subtask_label = {"satisfied": "subtarea verificada", "unsatisfied": "subtarea incompleta",
                         "unknown": "subtarea aún no verificable"}.get(status)
        details = []
        if subtask_label:
            details.append(subtask_label)
        if requirements:
            details.append(f"requisitos: {satisfied} satisfechos, {pending} pendientes")
        context = f" ({'; '.join(details)})" if details else ""
        action = actions.get(review["next_action"], "continuar")
        LOG.info("Revisión del supervisor%s; siguiente paso: %s.", context, action,
                 extra={"agentrun_agent": "supervisor"})

    def _initialize_contract(self, contract):
        from .supervisor_contract import repair_goal_quote_punctuation, validate_contract

        contract, quote_repairs = repair_goal_quote_punctuation(contract, self.state["goal"])
        contract = validate_contract(contract, self.state["goal"], evidence=self.state["evidence"],
                                     sent_refs=self.state["sent_refs"])
        if quote_repairs:
            self.trace.emit("coordinator.plan_quote_repaired", component="coordinator",
                            phase=self.state["contract_phase"],
                            call_id=getattr(self, "_last_supervisor_call_id", None),
                            payload={"repairs": quote_repairs, "contract": contract})
            for repair in quote_repairs:
                LOG.info("Cita del objetivo corregida localmente en %s: retirado el signo final %r "
                         "añadido al copiarla; el plan sigue pendiente de auditoría.",
                         repair["field"], repair["original"][-1], extra={"agentrun_agent": "supervisor"})
        previous = self.state["contract"]
        self.state["contract"] = contract
        self.state["contract_phase"] = "audit"
        self.state["plan_revision"] = None
        if previous is not None:
            self._log_plan_corrections(previous, contract)
        self._log_contract_summary(contract, "Plan corregido" if previous is not None else "Plan preparado")

    @staticmethod
    def _log_contract_summary(contract, heading):
        LOG.info("%s:", heading, extra={"agentrun_agent": "supervisor"})
        for requirement in contract.get("requirements", []):
            LOG.info("  • Requisito: %s", requirement["description"],
                     extra={"agentrun_agent": "supervisor"})
        for task in contract.get("tasks", []):
            dependencies = task.get("depends_on", [])
            suffix = f" (depende de: {', '.join(dependencies)})" if dependencies else ""
            LOG.info("  • Tarea: %s%s", task["instruction"], suffix,
                     extra={"agentrun_agent": "supervisor"})
        for loop in contract.get("loops", []):
            limit = loop.get("limit")
            bound = f"; límite {limit}" if limit is not None else ""
            LOG.info("  • Bucle: %s%s", loop["description"], bound,
                     extra={"agentrun_agent": "supervisor"})
            LOG.info("    • Descubrimiento: %s", loop["discovery"]["instruction"],
                     extra={"agentrun_agent": "supervisor"})
            for step in loop.get("steps", []):
                LOG.info("    • Paso: %s", step["instruction"],
                         extra={"agentrun_agent": "supervisor"})

    @staticmethod
    def _log_audit_problems(problems):
        LOG.warning("Motivos del rechazo:", extra={"agentrun_agent": "supervisor"})
        for problem in problems:
            LOG.warning("  • %s", problem, extra={"agentrun_agent": "supervisor"})

    @staticmethod
    def _log_plan_corrections(before, after):
        old_tasks = {task["id"]: task for task in before.get("tasks", [])}
        old_loops = {loop["id"]: loop for loop in before.get("loops", [])}
        changes = []
        for task in after.get("tasks", []):
            previous = old_tasks.get(task["id"])
            if previous and previous != task:
                if previous["instruction"] != task["instruction"]:
                    changes.append(f"Tarea «{task['id']}»: {task['instruction']}")
                if previous["success_criteria"] != task["success_criteria"]:
                    changes.append(f"Criterios de «{task['id']}»: {'; '.join(task['success_criteria'])}")
                if (previous["instruction"] == task["instruction"]
                        and previous["success_criteria"] == task["success_criteria"]):
                    changes.append(f"Tarea «{task['id']}»: dependencias o referencias actualizadas")
            elif not previous:
                changes.append(f"Nueva tarea: {task['instruction']}")
        for loop in after.get("loops", []):
            previous = old_loops.get(loop["id"])
            if previous and previous != loop:
                changes.append(f"Bucle «{loop['id']}»: actualizado")
            elif not previous:
                changes.append(f"Nuevo bucle: {loop['description']}")
        LOG.info("Correcciones propuestas:", extra={"agentrun_agent": "supervisor"})
        if changes:
            for change in changes:
                LOG.info("  • %s", change, extra={"agentrun_agent": "supervisor"})
        else:
            LOG.info("  • El plan devuelto no cambia tareas ni bucles.",
                     extra={"agentrun_agent": "supervisor"})

    def _audit_contract(self, response):
        if response["approved"]:
            LOG.info("Auditoría aprobada; el coordinador puede programar el trabajo.",
                     extra={"agentrun_agent": "supervisor"})
        else:
            replacement = response.get("replacement_contract")
            attempt = self.state["audit_replacements"] + 1
            problems = response.get("problems", [])
            self._log_audit_problems(problems)
            if self.state["audit_replacements"] >= MAX_AUDIT_REPLACEMENTS:
                if replacement is not None:
                    self._log_plan_corrections(self.state["contract"], replacement)
                reason = f"se agotaron las {MAX_AUDIT_REPLACEMENTS} correcciones permitidas"
                LOG.warning("Auditoría %d/%d rechazada; %s.", attempt, MAX_AUDIT_REPLACEMENTS + 1, reason,
                            extra={"agentrun_agent": "supervisor"})
                self._stop("plan_audit_failed", status="unverified")
                return
            if replacement is None:
                # A rejected audit is feedback for the planner, not approval and
                # not proof that the goal cannot be planned. Reserve the shared
                # correction slot before the next model call, also across resume.
                self.state["plan_revision"] = {
                    "problems": deepcopy(problems), "correction": attempt,
                    "instruction": "Revise the current contract against these audit findings and the original goal. "
                    "Return a complete initialize_plan contract, preserving valid work and user values. "
                    "Assess each finding against the contract; do not add constraints for a dismissed concern. "
                    "This is planning only; the revised contract must receive a new approved audit before execution.",
                }
                self.state["contract_phase"] = "initialize"
                LOG.info("Auditoría %d/%d rechazada sin plan corregido; "
                         "se solicita la corrección %d/%d al planificador.",
                         attempt, MAX_AUDIT_REPLACEMENTS + 1, attempt, MAX_AUDIT_REPLACEMENTS,
                         extra={"agentrun_agent": "supervisor"})
            else:
                # Local validation must succeed before retaining either the new
                # contract or audit feedback (invalid replies get their own retry).
                self._initialize_contract(replacement)
                LOG.info("Auditoría %d/%d rechazada; se aplica la corrección %d/%d y se volverá a auditar.",
                         attempt, MAX_AUDIT_REPLACEMENTS + 1, attempt, MAX_AUDIT_REPLACEMENTS,
                         extra={"agentrun_agent": "supervisor"})
            self.state["audit_history"].append({"correction": attempt, "problems": deepcopy(problems)})
            self.state["audit_replacements"] += 1
            return
        contract = self.state["contract"]
        # Historical state is never interpreted as permission to repeat an
        # unassociated effect. Migration preserves it for explicit diagnosis.
        if self.state.get("migration_orphan_attempts"):
            self._stop("migration_unreconciled", status="unverified")
            return
        if self.state.get("migration_pending") and (
            self.state["items"] or self.state["last_result"] or self.state.get("worker", {}).get("history")
        ):
            self._stop("migration_requires_reconciliation", status="unverified")
            return
        self.state["requirements"] = deepcopy(contract["requirements"])
        self.state["plan"] = [task["instruction"] for task in contract["tasks"]]
        self.state["loops"] = {loop["id"]: {
            "id": loop["id"], "goal_quote": loop["goal_quote"], "description": loop["description"],
            "exhausted": False, "evidence_refs": [], "item_refs": [],
        } for loop in contract["loops"]}
        self.state["migration_pending"] = False
        self.state["contract_phase"] = "review"
        # Review the already observed links before asking Jev to discover any.

    def _loop_contract(self, loop_id):
        return next(loop for loop in self.state["contract"]["loops"] if loop["id"] == loop_id)

    def _current_template(self):
        work = self.state["active_work"]
        if work is None:
            return None
        if work["kind"] == "task":
            return next(task for task in self.state["contract"]["tasks"] if task["id"] == work["template_id"])
        loop = self._loop_contract(work["loop_id"])
        if work["kind"] == "discover":
            return loop["discovery"]
        return next(step for step in loop["steps"] if step["id"] == work["template_id"])

    def _apply_progress(self, review):
        progress = False
        requirements = {req["id"]: req for req in self.state["requirements"]}
        for update in review["requirement_updates"]:
            old = requirements[update["id"]]
            progress |= update["status"] == "satisfied" and old["status"] != "satisfied"
            old.update(update)
        for discovery in review["discovered"]:
            loop_id = discovery["loop_id"]
            limit = self._loop_contract(loop_id)["limit"]
            positions = {url: index for index, url in enumerate(self.state.get("observed_url_order", []))}

            def observed_order(ref):
                url = self._item_fact(ref)["href"]
                return positions.get(url, len(positions))

            for ref in sorted(discovery["item_refs"], key=observed_order):
                existing = [item for item in self.state["items"].values() if item["loop_id"] == loop_id]
                if limit is not None and len(existing) >= limit:
                    break
                before = len(self.state["items"])
                item_id = self._register_item(ref, loop_id)
                item = self.state["items"][item_id]
                item.setdefault("completed_steps", {})
                item.setdefault("registration_order", max(
                    (other.get("registration_order", -1) for other in self.state["items"].values()), default=-1,
                ) + 1)
                progress |= len(self.state["items"]) > before
                if len(self.state["items"]) > before:
                    self.state["discovery_recoveries"][loop_id] = 0
                refs = self.state["loops"][loop_id]["item_refs"]
                if ref not in refs:
                    refs.append(ref)
        subtask = review.get("subtask_review")
        work = self.state["active_work"]
        if subtask and subtask["status"] == "satisfied" and work:
            template = self._current_template()
            record = {"delegation_id": subtask["delegation_id"], "evidence_refs": subtask["evidence_refs"]}
            if work["kind"] == "task":
                self.state["task_runs"][template["id"]] = record
            elif work["kind"] == "item":
                item = self.state["items"][work["item_id"]]
                destination_ref = subtask.get("destination_ref")
                if destination_ref:
                    self._adopt_destination(item, destination_ref)
                if not item.get("visited_refs"):
                    raise ValueError("An item step cannot finish before its destination has been observed")
                if template.get("verification_scope") == "destination":
                    urls = {item["url"], *item.get("aliases", [])}
                    after = self.state["last_result"]["started_observation"]
                    if not any(self.state["evidence"][ref].get("observed_url") in urls
                               and self.state["evidence"][ref]["observed_at"] > after
                               for ref in subtask["evidence_refs"]):
                        raise ValueError("This item step requires fresh evidence at its destination")
                item.setdefault("completed_steps", {})[template["id"]] = record
                item["evidence_refs"] = list(dict.fromkeys([
                    *item["evidence_refs"], *record["evidence_refs"],
                ]))
                steps = self._loop_contract(work["loop_id"])["steps"]
                if all(step["id"] in item["completed_steps"] for step in steps):
                    item["status"] = "completed"
                else:
                    item["status"] = "pending"
            self.state.setdefault("subtask_reviews", {})[subtask["delegation_id"]] = {
                **subtask, "task": deepcopy(self.state["last_result"]["task"]),
            }
            self.state["active_work"] = None
            progress = True
        elif work and work["kind"] == "discover" and progress:
            # The coordinator owns newly registered links, even if the worker
            # yielded rather than claimed its observation task complete.
            self.state["active_work"] = None
        for update in review["loop_reviews"]:
            loop = self.state["loops"][update["loop_id"]]
            spec = self._loop_contract(update["loop_id"])
            items = [item for item in self.state["items"].values() if item["loop_id"] == spec["id"]]
            if update["exhausted"]:
                if spec["limit"] is not None and len(items) < spec["limit"]:
                    raise ValueError("A bounded loop has not discovered its requested item count")
                coverage = self.state["page"].get("candidate_coverage", {})
                if spec["limit"] is None and coverage.get("extraction_complete") is False:
                    raise ValueError("Incomplete extraction cannot establish full loop coverage")
            progress |= update["exhausted"] and not loop["exhausted"]
            loop.update(exhausted=update["exhausted"], evidence_refs=update["evidence_refs"])
        if work and work["kind"] == "discover" and self.state["loops"][work["loop_id"]]["exhausted"]:
            self.state["active_work"] = None
        if progress:
            self.state["progress_revision"] += 1
            self.state["corrections"] = 0
            self.state["coordinator_stalls"] = 0
        self.state["last_review_progress"] = progress

    def _adopt_destination(self, item, ref):
        self._refs([ref], required=True, after=item["started_observation"])
        if ref not in item.get("navigation_destinations", {}):
            raise ValueError("Redirect destination was not observed after opening this item")
        destination = item["navigation_destinations"][ref]
        for other_id, other in list(self.state["items"].items()):
            if other_id == item["item_id"] or destination not in [other["url"], *other.get("aliases", [])]:
                continue
            if other["started_observation"] is not None or other["loop_id"] != item["loop_id"]:
                raise ValueError("Redirect identity conflicts with previously attempted work")
            self.state.setdefault("item_aliases", {})[other_id] = item["item_id"]
            del self.state["items"][other_id]
        item["aliases"] = list(dict.fromkeys([*item.get("aliases", []), destination]))
        item["visited_refs"] = list(dict.fromkeys([*item.get("visited_refs", []), ref]))
        spec = self._loop_contract(item["loop_id"])
        distinct = sum(other["loop_id"] == item["loop_id"] for other in self.state["items"].values())
        if spec["limit"] is not None and distinct < spec["limit"]:
            self.state["loops"][item["loop_id"]].update(exhausted=False, evidence_refs=[])

    def _ordered_loop_items(self, loop_id):
        refs = self.state["loops"][loop_id]["item_refs"]

        def order(item):
            fallback = refs.index(item["source_ref"]) if item["source_ref"] in refs else len(refs)
            unfinished = item["status"] != "completed" and (
                item.get("completed_steps") or item.get("started_observation") is not None
            )
            return (0 if unfinished else 1, item.get("registration_order", fallback))

        return sorted((item for item in self.state["items"].values() if item["loop_id"] == loop_id), key=order)

    def _validate_dispatch(self, review, payload):
        action = review["next_action"]
        if action not in payload["available_actions"]:
            raise ValueError("The requested recovery action is not available")
        if action == "repair":
            template = self._current_template()
            if template is None:
                raise ValueError("Repair requires an unfinished current step")
            if not set(template["success_criteria"]) <= set(review["repair"]["success_criteria"]):
                raise ValueError("A repair cannot remove the original success criteria")
            reconciled = {item["attempt_id"] for item in review["reconcile_attempts"]}
            if any(attempt["id"] not in reconciled for attempt in self.store.attempts(unresolved_only=True)):
                raise ValueError("Resolve uncertain attempts with read-only evidence before any delegation")

    def _dispatch_review(self, review, payload):
        action = review["next_action"]
        if action not in payload["available_actions"]:
            raise ValueError("The requested recovery action is not available")
        if action == "inspect":
            self.state["evidence_offset"] = payload["next_evidence_offset"]
        elif action == "observe":
            self._observe_recovery()
        elif action == "feedback":
            self._pause("supervisor_feedback", review["feedback"]["question"])
        elif action == "stop":
            reason = "worker_provider_error" if (self.state.get("last_result") or {}).get("provider_error") else (
                "supervisor_stopped"
            )
            self._stop(reason, status="blocked" if reason == "worker_provider_error" else "unverified")
        elif action == "repair":
            template = self._current_template()
            if template is None:
                raise ValueError("Repair requires an unfinished current step")
            repair = review["repair"]
            if not set(template["success_criteria"]) <= set(repair["success_criteria"]):
                raise ValueError("A repair cannot remove the original success criteria")
            if self.state["corrections"] >= 2:
                self._stop("recovery_exhausted", status="unverified")
                return
            self.state["corrections"] += 1
            self._queue_work(self.state["active_work"], {**template, **repair}, repairing=True)
        else:
            if self.store.attempts(unresolved_only=True):
                if self.state["read_recoveries"] < 2:
                    self._observe_recovery()
                else:
                    self._stop("uncertain_action", status="unverified")
                return
            self._schedule(review)

    def _observe_recovery(self):
        if self.state["read_recoveries"] >= 2:
            raise ValueError("The read-only recovery budget is exhausted")
        self.state["read_recoveries"] += 1
        self._save()
        self.trace.emit("recovery.observation_started", component="coordinator",
                        recovery_number=self.state["read_recoveries"])
        try:
            self.worker.pending_text = None
            self.worker.state["decision"] = None
            self.worker._install_page(self.browser.observe(screenshot=False))
        except Exception as error:
            self.trace.emit("recovery.observation_failed", component="coordinator", error_type=type(error).__name__)
            if self.state["read_recoveries"] >= 2:
                self._stop("observation_unavailable")

    def _loop_complete(self, loop_id):
        if not self.state["loops"][loop_id]["exhausted"]:
            return False
        spec = self._loop_contract(loop_id)
        items = [item for item in self.state["items"].values() if item["loop_id"] == loop_id]
        return (all(item["status"] == "completed" for item in items)
                and (spec["limit"] is None or len(items) >= spec["limit"]))

    def _dependencies_satisfied(self, dependencies):
        return all(key in self.state["task_runs"] or (key in self.state["loops"] and self._loop_complete(key))
                   for key in dependencies)

    def _schedule(self, review):
        if self.state.get("migration_pending"):
            self._stop("migration_requires_reconciliation", status="unverified")
            return
        work = self.state["active_work"]
        if work:
            last = self.state.get("last_result") or {}
            if last.get("status") == "yielded":
                self._queue_work(work, self._current_template())
            elif (work["kind"] == "discover" and last.get("status") == "claimed_done"
                  and (review.get("subtask_review") or {}).get("status") in {"unsatisfied", "unknown"}):
                self._recover_discovery()
            elif self.state["read_recoveries"] < 2:
                self._observe_recovery()
            else:
                self._stop("step_not_verified", status="unverified")
            return
        contract = self.state["contract"]
        for task in contract["tasks"]:
            if task["id"] not in self.state["task_runs"] and self._dependencies_satisfied(task["depends_on"]):
                self._queue_work({"kind": "task", "template_id": task["id"]}, task)
                return
        for loop in contract["loops"]:
            if not self._dependencies_satisfied(loop["depends_on"]):
                continue
            items = self._ordered_loop_items(loop["id"])
            for item in items:
                if item["status"] == "completed":
                    continue
                step = next(step for step in loop["steps"] if step["id"] not in item.get("completed_steps", {}))
                self._queue_work({"kind": "item", "template_id": step["id"], "loop_id": loop["id"],
                                  "item_id": item["item_id"]}, step)
                return
            if self.state["loops"][loop["id"]]["exhausted"]:
                continue
            if loop["limit"] is not None and len(items) >= loop["limit"]:
                self.state["loops"][loop["id"]].update(exhausted=True, evidence_refs=list(dict.fromkeys(
                    ref for item in items for ref in item["evidence_refs"]
                )))
                continue
            self._queue_work({"kind": "discover", "template_id": loop["discovery"]["id"],
                              "loop_id": loop["id"]}, loop["discovery"])
            return
        completed = (all(task["id"] in self.state["task_runs"] for task in contract["tasks"])
                     and all(self._loop_complete(loop_id) for loop_id in self.state["loops"])
                     and all(item["status"] == "completed" for item in self.state["items"].values())
                     and all(req["status"] == "satisfied" for req in self.state["requirements"]))
        if completed and not self.store.attempts(unresolved_only=True):
            self.state.update(status="done", stop_reason=None, question="")
            self.state["verification"] = {
                "status": "satisfied", "method": "coordinator_v2",
                "requirements": deepcopy(self.state["requirements"]),
                "evidence_refs": list(dict.fromkeys(ref for req in self.state["requirements"]
                                                    for ref in req["evidence_refs"])),
            }
            LOG.info("Finalizado: todos los pasos y resultados están verificados",
                     extra={"agentrun_agent": "supervisor"})
            return
        self.state["coordinator_stalls"] += 1
        if self.state["coordinator_stalls"] >= 2:
            self._stop("outcomes_not_verified", status="unverified")

    def _recover_discovery(self):
        work = self.state["active_work"]
        loop_id = work["loop_id"]
        count = self.state["discovery_recoveries"].get(loop_id, 0)
        if count >= MAX_DISCOVERY_RECOVERIES:
            self._stop("discovery_not_verified", status="unverified")
            return
        self.state["discovery_recoveries"][loop_id] = count + 1
        template = deepcopy(self._current_template())
        template["instruction"] += (
            "\nThe supervisor did not verify the previous DONE. Continue discovering missing relevant links "
            "from the current page with fresh observations and decisions. Already completed items do not count "
            "as new discoveries. Do not process items, repeat their effects or declare scope complete merely "
            "because the current viewport contains only known links."
        )
        progress = self._discovery_progress()[loop_id]
        LOG.info("Descubrimiento incompleto: %s de %s elementos registrados; continuación %d/%d "
                 "sin repetir los elementos completados.", progress["registered"],
                 progress["limit"] if progress["limit"] is not None else "alcance completo",
                 count + 1, MAX_DISCOVERY_RECOVERIES, extra={"agentrun_agent": "supervisor"})
        self.trace.emit("coordinator.discovery_recovery", component="coordinator", loop_id=loop_id,
                        recovery_number=count + 1, payload=progress)
        # A new bounded discovery decision, never replay of a recorded input.
        # _dispatch_review has already barred unresolved effects and _queue_work
        # enforces global budgets. The counter survives resume and only resets
        # when a new distinct relevant item is registered for this loop.
        self._queue_work(work, template)

    def _queue_work(self, work, template, *, repairing=False):
        if self.store.attempts(unresolved_only=True):
            raise ValueError("Resolve uncertain attempts with read-only evidence before any delegation")
        reason = self._budget_reason()
        if reason and reason != "supervisor_budget":
            self._stop(reason)
            return
        work = deepcopy(work)
        item_id = work.get("item_id")
        if item_id:
            item = self.state["items"][item_id]
            if template["id"] in item.get("completed_steps", {}):
                raise ValueError("A verified item step cannot be delegated again")
            item["status"] = "active"
            if item["started_observation"] is None:
                item["started_observation"] = self.state["observation_seq"]
        task = {
            "id": template["id"], "instruction": (
                "Execute ONLY the bounded task below. Return DONE as soon as its success criteria hold. "
                "Do not start another item or another workflow step. Global constraints restrict this task; "
                "they do not authorize additional actions.\nTASK:\n" + template["instruction"]
            ),
            "success_criteria": template["success_criteria"],
            "scope": "general" if work["kind"] == "task" else work["kind"],
            "item_ref": item_id, "loop_id": work.get("loop_id"),
        }
        constraints = self.state["contract"]["execution_constraints"]
        if constraints:
            task["instruction"] += "\nGLOBAL EXECUTION CONSTRAINTS:\n" + "\n".join(constraints)
        if work["kind"] == "discover":
            progress = self._discovery_progress()[work["loop_id"]]
            task["instruction"] += (
                f"\nDISCOVERY PROGRESS: {progress['registered']} distinct relevant items are registered; "
                f"{progress['completed']} are completed. Requested limit: {progress['limit']}. "
                "Expose remaining relevant links; registration and completion are owned by the coordinator."
            )
        self.state["active_work"] = work
        self.state["active_item"] = item_id
        self.state["delegation_sequence"] += 1
        accumulated = self.state["progress_revision"] > self.state["delegated_progress_revision"]
        delegation = {
            "id": f"{self.run_id}:{self.state['delegation_sequence']}", "input": task,
            "progress": accumulated, "started_observation": self.state["observation_seq"],
            "started_step": len(self.worker.state["history"]),
        }
        self.store.save_delegation(delegation["id"], task)
        self.trace.emit("delegation.queued", component="coordinator", delegation_id=delegation["id"],
                        item_id=item_id, template_id=template["id"], payload=delegation)
        self.state["pending_delegation"] = delegation
        self.state["delegated_progress_revision"] = self.state["progress_revision"]
        if accumulated:
            self.state["corrections"] = 0
        if not repairing:
            self.state["read_recoveries"] = 0

