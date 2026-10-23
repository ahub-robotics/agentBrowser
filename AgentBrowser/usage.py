"""Per-model usage and provider-reported or published-rate cost summaries."""

from collections import defaultdict
from math import isfinite

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# TypeSafe's published price: $0.042 per million input tokens; output is free.
# Keep versioned IDs explicit so a future alias/model price change cannot silently
# alter past summaries. Both current versions have this published rate.
PUBLISHED_INPUT_USD_PER_MILLION = {
    "jev-1.13.0": 0.042,
    "jev-1.14.0": 0.042,
    "jev-latest": 0.042,
    "jev-preview": 0.042,
}


def _tokens(usage):
    if not isinstance(usage, dict):
        return 0, 0, 0
    incoming = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    outgoing = usage.get("output_tokens", usage.get("completion_tokens", 0))
    total = usage.get("total_tokens", 0)
    return tuple(value if type(value) is int and value >= 0 else 0
                 for value in (incoming, outgoing, total))


def _reported_cost(usage):
    if not isinstance(usage, dict):
        return None
    for key in ("cost", "cost_usd", "total_cost"):
        value = usage.get(key)
        if type(value) in (int, float) and isfinite(value) and value >= 0:
            return float(value)
    return None


def summarize_model_usage(state):
    """Aggregate calls, reported costs, and costs derivable from published rates."""
    worker = state.get("worker") if isinstance(state.get("worker"), dict) else state
    records = []

    def add(component, model, usage, phase=None):
        records.append({"component": component, "model": model or "unknown", "usage": usage or {},
                        "phase": phase})

    for decision in worker.get("decisions", []):
        add("Jev", decision.get("model"), decision.get("usage"))
    for call in worker.get("text_calls", []):
        add("Auxiliar de texto", call.get("model"), call.get("usage"))
    for review in worker.get("verification_reviews", state.get("verification_reviews", [])):
        add("Verificador", review.get("model"), review.get("usage"))
    for usage in state.get("supervisor_usage", []):
        if isinstance(usage, dict):
            add("Supervisor", usage.get("model"), usage.get("usage", usage), usage.get("phase"))

    metrics = state.get("metrics", {})
    if not metrics:
        metrics = {
            "jev_calls": worker.get("model_calls", len(worker.get("decisions", []))),
            "text_calls": worker.get("text_model_calls", len(worker.get("text_calls", []))),
            "verification_calls": worker.get("verification_calls", len(worker.get("verification_reviews", []))),
            "supervisor_calls": state.get("supervisor_calls", 0),
        }
    expected = {
        "Jev": metrics.get("jev_calls", worker.get("model_calls", len(worker.get("decisions", [])))),
        "Auxiliar de texto": metrics.get(
            "text_calls", worker.get("text_model_calls", len(worker.get("text_calls", []))),
        ),
        "Verificador": metrics.get("verification_calls", worker.get(
            "verification_calls", len(worker.get("verification_reviews", [])),
        )),
        "Supervisor": metrics.get("supervisor_calls", state.get("supervisor_calls", 0)),
    }
    groups = defaultdict(lambda: {
        "calls": 0, "metered_calls": 0, "input_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "known_cost_usd": 0.0, "missing_cost_calls": 0,
        "provider_cost_calls": 0, "published_rate_calls": 0,
    })
    for record in records:
        group = groups[(record["component"], record["model"], record["phase"])]
        group["calls"] += 1
        usage = record["usage"]
        if usage:
            group["metered_calls"] += 1
        incoming, outgoing, total = _tokens(usage)
        group["input_tokens"] += incoming
        group["output_tokens"] += outgoing
        group["total_tokens"] += total or incoming + outgoing
        cost = _reported_cost(usage)
        if cost is not None:
            group["known_cost_usd"] += cost
            group["provider_cost_calls"] += 1
        elif (record["component"] == "Jev" and record["model"] in PUBLISHED_INPUT_USD_PER_MILLION
              and isinstance(usage, dict) and any(key in usage for key in ("input_tokens", "prompt_tokens"))):
            rate = PUBLISHED_INPUT_USD_PER_MILLION[record["model"]]
            group["known_cost_usd"] += incoming * rate / 1_000_000
            group["published_rate_calls"] += 1
        else:
            group["missing_cost_calls"] += 1

    for component, count in expected.items():
        recorded = sum(group["calls"] for (name, _model, _phase), group in groups.items() if name == component)
        missing = max(0, count - recorded) if type(count) is int else 0
        if missing:
            group = groups[(component, "sin uso devuelto", None)]
            group["calls"] += missing
            group["missing_cost_calls"] += missing

    models = []
    for (component, model, phase), group in sorted(groups.items(), key=lambda item: tuple(
            "" if part is None else str(part) for part in item[0])):
        entry = {"component": component, "model": model, "phase": phase, **group}
        entry["cost_usd"] = (round(group["known_cost_usd"], 8)
                             if group["calls"] and not group["missing_cost_calls"] else None)
        entry["known_cost_usd"] = round(group["known_cost_usd"], 8)
        entry["cost_basis"] = (
            "provider_reported" if group["provider_cost_calls"] and not group["published_rate_calls"]
            else "published_rate" if group["published_rate_calls"] and not group["provider_cost_calls"]
            else "mixed" if group["provider_cost_calls"] or group["published_rate_calls"] else None
        )
        models.append(entry)
    calls = sum(model["calls"] for model in models)
    known_cost = round(sum(model["known_cost_usd"] for model in models), 8)
    missing_cost_calls = sum(model["missing_cost_calls"] for model in models)
    provider_cost_calls = sum(model["provider_cost_calls"] for model in models)
    published_rate_calls = sum(model["published_rate_calls"] for model in models)
    cost_basis = (
        "mixed" if provider_cost_calls and published_rate_calls
        else "provider_reported" if provider_cost_calls
        else "published_rate" if published_rate_calls
        else None
    )
    return {
        "models": models, "calls": calls, "known_cost_usd": known_cost,
        "cost_usd": known_cost if calls and not missing_cost_calls else (0.0 if not calls else None),
        "missing_cost_calls": missing_cost_calls,
        "cost_basis": cost_basis,
    }


def print_model_usage(summary):
    """Print a compact Spanish usage and cost report."""
    if summary is None:
        return
    if not summary or not summary.get("calls"):
        Console().print(Panel("Sin llamadas registradas.", title="Consumo de modelos", border_style="dim"))
        return
    table = Table(box=None, show_header=True, header_style="bold cyan", expand=False, padding=(0, 1))
    table.add_column("Componente", style="bold")
    table.add_column("Modelo")
    table.add_column("Fase")
    table.add_column("Llamadas", justify="right")
    table.add_column("Entrada / salida", justify="right")
    table.add_column("Coste", justify="right")
    for model in summary["models"]:
        tokens = (f"{model['input_tokens']} entrada / {model['output_tokens']} salida tokens"
                  if model["metered_calls"] else "uso de tokens no devuelto")
        cost = (f"${model['cost_usd']:.6f} USD" if model["cost_usd"] is not None
                else "coste no informado")
        if model.get("cost_basis") == "published_rate":
            cost += " (tarifa pública TypeSafe)"
        elif model.get("cost_basis") == "mixed":
            cost += " (coste informado y tarifa pública)"
        phase = {"initialize": "plan", "audit": "auditoría", "review": "revisión"}.get(
            model.get("phase"), "—",
        )
        table.add_row(Text(str(model["component"])), Text(str(model["model"]), overflow="fold"), phase,
                      str(model["calls"]), Text(tokens), Text(cost, style="green" if model["cost_usd"] is not None
                                                                   else "yellow"))
    if summary["cost_usd"] is None:
        total = (f"Total no calculable: {summary['missing_cost_calls']} llamada(s) sin precio. "
                 f"Subtotal calculable: ${summary['known_cost_usd']:.6f} USD.")
    else:
        suffix = " · calculado con la tarifa pública de TypeSafe" \
            if summary.get("cost_basis") == "published_rate" else ""
        total = f"Coste total: ${summary['cost_usd']:.6f} USD{suffix}"
    Console().print(Panel.fit(table, title="Consumo de modelos", border_style="cyan", padding=(0, 1)))
    Console().print(Text(total, style="bold cyan"))
