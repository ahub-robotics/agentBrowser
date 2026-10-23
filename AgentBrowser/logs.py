"""Compact action outcomes and optional decision traces."""

import json
import logging
import unicodedata

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .model import provider_limit_metadata
from .observability import current_trace, redact

LOGGER = logging.getLogger("agentrun")


class AgentRunRichHandler(logging.Handler):
    """Render concise, component-colored console messages without trace IDs."""

    _agents = {
        "jev": ("JEV", "bright_cyan"),
        "supervisor": ("SUPERVISOR", "#d946ef"),
        "text": ("AUXILIAR DE TEXTO", "bright_yellow"),
        "verification": ("VERIFICACIÓN", "bright_blue"),
        "browser": ("NAVEGADOR", "bright_green"),
        "agent": ("AGENTRUN", "white"),
    }

    def __init__(self):
        super().__init__()
        self.console = Console(stderr=False, soft_wrap=True, width=120, highlight=False,
                                force_terminal=True, color_system="truecolor")

    def emit(self, record):
        try:
            message = record.getMessage()
            key = getattr(record, "agentrun_agent", None) or self._infer_agent(message)
            label, color = self._agents.get(key, self._agents["agent"])
            line = Text()
            line.append(self.formatTime(record) if hasattr(self, "formatTime") else
                        __import__("datetime").datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                        style="dim")
            line.append("  ")
            line.append(label, style=f"bold {color}")
            line.append("  ")
            line.append(message, style=color)
            self.console.print(line, soft_wrap=True)
            if record.exc_info:
                self.console.print_exception(show_locals=False)
        except Exception:
            self.handleError(record)

    @staticmethod
    def _infer_agent(message):
        lowered = message.lower()
        if lowered.startswith(("jev ", "jev:", "↳ jev")):
            return "jev"
        if lowered.startswith(("supervisor ", "supervisor:", "supervisor →")):
            return "supervisor"
        if lowered.startswith(("auxiliar de texto", "texto ")):
            return "text"
        return "agent"

_PROVIDER_CAUSES = {
    "authentication": "credenciales rechazadas",
    "billing": "crédito o límite de gasto",
    "rate_limit": "límite de peticiones",
    "unavailable": "proveedor no disponible",
    "timeout": "tiempo agotado",
    "connection": "conexión fallida",
    "request": "solicitud rechazada",
    "invalid_response": "respuesta inválida",
    "unknown": "fallo del proveedor",
}
_LIMIT_CAUSES = {
    "openrouter_credits": "crédito insuficiente",
    "openrouter_key_limit": "límite de gasto de la clave",
    "openrouter_in_flight_budget": "presupuesto temporal de peticiones",
    "openrouter_guardrail_budget": "límite de gasto de la política",
    "openrouter_workspace_budget": "presupuesto de la organización",
}
_VERIFICATION_CAUSES = {
    "verification_unconfigured": "sin configurar",
    "verification_invalid_response": "respuesta inválida",
    "verification_budget_insufficient": "petición demasiado grande",
    "verification_budget": "límite de revisiones",
    "verification_no_progress": "sin progreso verificable",
    "no_new_evidence": "sin evidencia nueva",
}


def configure_logging():
    """Install the component-colored console handler, even with root handlers present."""
    if not any(isinstance(handler, AgentRunRichHandler) for handler in LOGGER.handlers):
        LOGGER.addHandler(AgentRunRichHandler())
    # A root handler usually colors by severity and can make every component
    # look alike. AgentRun owns its console rendering once its handler is set.
    LOGGER.propagate = False
    if LOGGER.level == logging.NOTSET:
        LOGGER.setLevel(logging.INFO)


def print_run_summary(state):
    """Render the live example's final state as a compact Rich panel."""
    status = state.get("status", "unknown")
    verification = state.get("verification", {}).get("status")
    duration = state.get("elapsed_ms", 0) / 1000
    wait = state.get("provider_wait_seconds", 0)
    color = "green" if status == "done" and verification == "satisfied" else (
        "yellow" if status in {"waiting_feedback", "waiting_provider"} else "red"
    )
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    table.add_row("Estado", Text(status, style=f"bold {color}"))
    table.add_row("Duración", f"{duration + wait:.1f} s")
    if wait:
        table.add_row("Tiempo activo / espera", f"{duration:.1f} s / {wait:.1f} s")
    if state.get("run_id"):
        table.add_row("Ejecución", state["run_id"])
    if verification:
        table.add_row("Verificación", verification)
    if state.get("question"):
        table.add_row("Pendiente", Text(state["question"], overflow="fold"))
    Console(soft_wrap=True).print(Panel(table, title="AgentRun · ejecución", border_style=color,
                                        expand=False, padding=(0, 1)))


def _label(value, limit=240):
    value = str(value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _single_line(message):
    # JSON escapes ASCII controls; escape Unicode display controls as well.
    return "".join(char if char.isprintable() else json.dumps(char, ensure_ascii=True)[1:-1] for char in message)


def _redact(value):
    trace = current_trace()
    return trace.redact(value) if trace is not None else redact(value)


def log_delegation(task, *, item_url=None, delegation_id=None):
    """Show the supervisor's instruction and criteria once at the worker boundary."""
    if not LOGGER.isEnabledFor(logging.INFO):
        return
    trace = current_trace()
    if trace is not None and trace.enabled:
        LOGGER.info("Asigna a Jev una tarea con %d criterios de éxito.", len(task["success_criteria"]),
                    extra={"agentrun_agent": "supervisor"})
        return
    # Keep the handoff complete, but never dump the task's surrounding state,
    # provider configuration, page contents or generated field values.
    safe, _ = _redact({"instruction": task["instruction"], "criteria": task["success_criteria"], "url": item_url})
    instruction = json.dumps(safe.get("instruction", "[REDACTED]"), ensure_ascii=False)
    criteria = json.dumps(safe.get("criteria", "[REDACTED]"), ensure_ascii=False)
    LOGGER.info("%s", _single_line("Asigna a Jev la tarea: " + instruction),
                extra={"agentrun_agent": "supervisor"})
    LOGGER.info("Criterios de éxito: %s", _single_line(criteria), extra={"agentrun_agent": "supervisor"})
    if item_url:
        element_url = _single_line(json.dumps(safe.get("url", "[REDACTED]"), ensure_ascii=False))
        LOGGER.info("Elemento de trabajo: %s", element_url, extra={"agentrun_agent": "supervisor"})


def _stop_details(details):
    """Keep provider failures useful without exposing exception/response text."""
    safe = {}
    status = details.get("status")
    if isinstance(status, str) and status in {"done", "blocked", "unverified"}:
        safe["status"] = status
    reason = details.get("reason")
    if isinstance(reason, str) and (reason in _VERIFICATION_CAUSES or reason == "verification_provider_error"):
        safe["reason"] = reason
    if reason == "verification_provider_error":
        kind = details.get("provider_error_kind")
        safe["provider_error_kind"] = kind if isinstance(kind, str) and kind in _PROVIDER_CAUSES else "unknown"
        http_status = details.get("provider_http_status")
        if type(http_status) is int and 400 <= http_status <= 599:
            safe["provider_http_status"] = http_status
        safe.update(provider_limit_metadata(
            http_status, details.get("provider_limit_source"), details.get("provider_limit_reason"),
        ))
    return safe


def _stop_cause(details):
    reason = details.get("reason")
    if reason == "verification_provider_error":
        cause = _PROVIDER_CAUSES[details["provider_error_kind"]]
        source = details.get("provider_limit_source")
        if source in _LIMIT_CAUSES:
            cause = _LIMIT_CAUSES[source]
        if details.get("provider_limit_reason") == "weight_exceeds_budget":
            cause = "coste de la petición superior al presupuesto temporal"
        if "provider_http_status" in details:
            cause += f", HTTP {details['provider_http_status']}"
    else:
        cause = _VERIFICATION_CAUSES.get(reason, "causa no disponible")
    return "revisor: " + cause


def _summary(event, decision, element, details):
    prefixes = {
        "model_choice": "Jev decide",
        "model_proposed": "Jev propone",
        "action_executed": "Jev ejecutó",
        "action_discarded": "Jev retira",
    }
    prefix = prefixes.get(event)
    if event == "run_stopped":
        prefix = {"done": "Tarea finalizada", "blocked": "Ejecución detenida"}.get(
            details.get("status"), "Ejecución no verificada")
    if prefix is None:
        return None
    operations = {
        "CLICK": "pulsar", "TYPE_TEXT": "escribir", "SELECT": "seleccionar",
        "SCROLL_DOWN": "desplazarse hacia abajo", "SCROLL_UP": "desplazarse hacia arriba",
        "RELOAD": "recargar la página",
        "WAIT": "esperar", "DONE": "dar la tarea por terminada", "BLOCKED": "detenerse",
        "MORE_CANDIDATES": "revisar más elementos",
    }
    operation = decision.get("operation") or decision.get("choice")
    summary = prefix if event == "run_stopped" else f"{prefix} {operations.get(operation, str(operation).lower())}"
    if element and event != "run_stopped":
        # Typography spaces are readable as spaces. Newlines, bidi controls and
        # other display controls still go through the single-line escaping path.
        label = "".join(" " if unicodedata.category(char) == "Zs" else char for char in element["label"])
        summary += " en " + json.dumps(_label(label, limit=120), ensure_ascii=False)
    if event == "action_discarded":
        reason = "página cambió" if details.get("reason") == "stale_observation" else "revisor"
        summary += f" ({reason})"
    elif event == "run_stopped" and details.get("status") == "unverified":
        summary += f" ({_stop_cause(details)})"
    return _single_line(summary)


def log_event(event, decision, page, **details):
    """Resolve only the validated choice against the window that the model saw."""
    trace = current_trace()
    if trace is not None:
        trace.emit(event, component="worker", decision_id=decision.get("decision_id"),
                   observation_seq=decision.get("observation_seq"),
                   payload={"decision": decision, "details": details})
    if not LOGGER.isEnabledFor(logging.INFO):
        return
    if event == "run_stopped":
        details = _stop_details(details)
    action = next((item for item in page["actions"] if item["id"] == decision["choice"]), None)
    # Global operations have an action ID, but no observed element target.
    element = None
    if action is not None and decision.get("target") is not None:
        element = {key: action[key] for key in ("kind", "node", "role", "option") if key in action}
        element["label"] = action.get("label", "")
        # Scrub before truncating or JSON escaping: both transformations can
        # otherwise turn a known credential into an unrecognizable fragment.
        element, _ = _redact(element)
        element["label"] = _label(element.get("label", "[REDACTED]"))
    summary = _summary(event, decision, element, details)
    if summary is not None:
        summary, _ = _redact(summary)
        if not isinstance(summary, str):
            summary = "[REDACTED]"
        LOGGER.info("%s", summary, extra={"agentrun_agent": "jev"})
    if not LOGGER.isEnabledFor(logging.DEBUG):
        return
    coverage = page.get("candidate_coverage", {})
    record = {
        "event": event,
        "decision_id": decision.get("decision_id"),
        "inventory_id": decision.get("inventory_id", coverage.get("inventory_id")),
        "window_id": decision.get("window_id", coverage.get("window_id")),
        "operation": decision.get("operation"),
        "target": decision.get("target"),
        "choice": decision["choice"],
        "element": element,
        "confidence": decision.get("confidence"),
        "target_confidence": decision.get("target_confidence"),
        "latency_ms": decision.get("latency_ms"),
        **details,
    }
    record, _ = _redact(record)
    LOGGER.debug("%s", _single_line(json.dumps(record, ensure_ascii=False)),
                 extra={"agentrun_event": record, "agentrun_agent": "jev"})
