"""Offline decision logs: observed selections, execution boundaries, and safe fields."""

import json
import logging
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_agent import page as legacy_page
from test_candidate_workflow import InventoryBrowser, inventory_page, response

from AgentBrowser import agent as loop
from AgentBrowser import logs, model
from AgentBrowser.browser import StalePage
from AgentBrowser.candidates import DEFAULT_LIMITS


def events(caplog):
    return [record.agentrun_event for record in caplog.records if hasattr(record, "agentrun_event")]


@pytest.fixture
def logged_agent(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    monkeypatch.setenv("TYPESAFE_API_KEY", "credential-not-for-logs")
    calls = []

    def create(operation="SELECT", target="1:2", page=None):
        browser = InventoryBrowser(page or inventory_page(count=5))
        monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))

        def post(_url, _key, body):
            body.http_attempts += 1
            calls.append(body)
            return response(body, operation, target)

        monkeypatch.setattr(model, "post_json", post)
        agent = loop.Agent(
            "https://example.test/options", "private-goal-not-for-logs",
            candidate_limits=replace(DEFAULT_LIMITS, max_options_per_select=2),
            verification=False, trace=False,  # Exercise the original compact logger independently of tracing.
        )
        return agent, browser, calls

    return create


def test_prediction_logs_only_selected_observed_element_then_execution(logged_agent, caplog):
    agent, browser, calls = logged_agent()
    predicted = agent.command("predict")
    selected = [event for event in events(caplog) if event["event"] == "model_choice"]
    assert len(selected) == 1
    record = selected[0]
    assert record["event"] == "model_choice"
    assert record["operation"] == "SELECT" and record["target"] == "1:2"
    assert record["choice"] == predicted["decision"]["choice"]
    assert record["decision_id"] == predicted["decision"]["decision_id"]
    assert record["inventory_id"] == predicted["decision"]["inventory_id"]
    assert record["window_id"] == record["model_call"] == 1
    assert record["confidence"] == record["target_confidence"] == 1
    assert record["element"] == {
        "label": "Destination → Option 1", "kind": "select", "role": "combobox", "node": 1, "option": 1,
    }
    summaries = [item for item in caplog.records if item.levelno == logging.INFO]
    assert [item.getMessage() for item in summaries] == [
        'Jev decide seleccionar en "Destination → Option 1"',
        'Jev propone seleccionar en "Destination → Option 1"',
    ]
    assert not hasattr(summaries[0], "agentrun_event")
    assert browser.inputs == []

    observe = browser.observe

    def observe_after_log(**kwargs):
        assert events(caplog)[-1]["event"] == "action_executed"
        assert agent.state["history"][-1]["choice"] == record["choice"]
        return observe(**kwargs)

    browser.observe = observe_after_log
    agent.command("act", agent.decision_body())
    executed = events(caplog)[-1]
    assert executed["event"] == "action_executed" and executed["step"] == 1
    assert executed["decision_id"] == record["decision_id"]
    assert executed["element"] == record["element"]
    assert [item.getMessage() for item in caplog.records if item.levelno == logging.INFO] == [
        'Jev decide seleccionar en "Destination → Option 1"',
        'Jev propone seleccionar en "Destination → Option 1"',
        'Jev ejecutó seleccionar en "Destination → Option 1"',
    ]
    assert len(calls) == len(browser.inputs) == 1
    assert browser.selected == "value-1"
    assert "not-observed" not in caplog.text
    assert "private-goal-not-for-logs" not in caplog.text
    assert "credential-not-for-logs" not in caplog.text
    assert "value-1" not in caplog.text


@pytest.mark.parametrize("error", [StalePage("Changed before input"), RuntimeError("Input result unknown")])
def test_rejected_or_ambiguous_mutation_does_not_log_execution(logged_agent, caplog, error):
    agent, browser, _ = logged_agent()
    agent.command("predict")
    browser.act = Mock(side_effect=error)
    with pytest.raises(type(error)):
        agent.command("act", agent.decision_body())
    assert not any(event["event"] == "action_executed" for event in events(caplog))
    assert agent.state["history"] == []
    browser.act.assert_called_once()


def test_failed_post_action_observation_preserves_execution_log(logged_agent, caplog):
    agent, browser, _ = logged_agent()
    agent.command("predict")
    browser.observe = Mock(side_effect=StalePage("Changed after input"))
    with pytest.raises(StalePage):
        agent.command("act", agent.decision_body())
    assert events(caplog)[-1]["event"] == "action_executed"
    assert sum(event["event"] == "action_executed" for event in events(caplog)) == 1
    assert not any(event["event"] == "action_discarded" for event in events(caplog))
    assert len(agent.state["history"]) == len(browser.inputs) == 1


@pytest.mark.parametrize("operation", ["DONE", "BLOCKED", "WAIT"])
def test_targetless_decisions_do_not_report_an_element(logged_agent, caplog, operation):
    agent, browser, _ = logged_agent(operation, None)
    agent.command("tick")
    chosen, consumed = events(caplog)
    assert chosen["event"] == "model_choice" and chosen["operation"] == operation
    assert chosen["element"] is None and chosen["target"] is None
    assert consumed["element"] is None and consumed["decision_id"] == chosen["decision_id"]
    if operation == "WAIT":
        assert consumed["event"] == "action_executed" and len(browser.inputs) == 1
        summary = "Jev ejecutó esperar"
    else:
        assert consumed["event"] == "run_stopped" and consumed["status"] == operation.lower()
        assert browser.inputs == []
        summary = "Tarea finalizada" if operation == "DONE" else "Ejecución detenida"
    operation_text = {"DONE": "dar la tarea por terminada", "BLOCKED": "detenerse", "WAIT": "esperar"}[operation]
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.INFO] == [
        f'Jev decide {operation_text}', summary,
    ]


def test_reconsultation_logs_source_and_destination_without_browser_input(logged_agent, caplog):
    agent, browser, calls = logged_agent("MORE_CANDIDATES", None)
    result = agent.command("tick")
    chosen, advanced = events(caplog)
    assert chosen["operation"] == "MORE_CANDIDATES" and chosen["element"] is None
    assert advanced["event"] == "candidates_advanced"
    assert advanced["window_id"] == 1 and advanced["to_window_id"] == 2
    assert advanced["decision_id"] == chosen["decision_id"]
    assert advanced["element"] is None and browser.inputs == []
    assert len(calls) == result["model_calls"] == 1
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.INFO] == [
        "Jev decide revisar más elementos",
    ]


def test_text_helper_value_is_not_logged(logged_agent, monkeypatch, caplog):
    agent, browser, _ = logged_agent("TYPE_TEXT", "1", legacy_page())
    monkeypatch.setattr(loop, "field_text", Mock(return_value=(
        "private-generated-value", {"model": "test-helper", "latency_ms": 1},
    )))
    agent.command("tick")
    assert len(browser.inputs) == 1
    assert agent.state["history"][0]["text"] == "private-generated-value"
    assert "private-generated-value" not in caplog.text
    for event in events(caplog):
        assert event["element"]["label"] == "Search"
        assert not ({"goal", "request", "raw_answers", "text", "value"} & event.keys())
        assert "value" not in event["element"]


def test_log_lines_escape_control_characters_bound_labels_and_exclude_payloads(caplog):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    decision = {
        "choice": "observed", "operation": "CLICK", "target": "1", "confidence": 0.9,
        "target_confidence": 0.8, "latency_ms": 12, "decision_id": "decision-1",
        "inventory_id": "inventory-1", "window_id": 2,
        "request": {"secret": "request-secret"}, "raw_answers": {"secret": "answer-secret"},
    }
    page = {"actions": [{
        "id": "observed", "node": 0, "kind": "click", "label": "first\n\x1b[31m\u00a0\u0085\u2028" + "x" * 400,
        "role": "button", "value": "field-secret",
    }], "text": "page-secret"}
    logs.log_event("model_choice", decision, page, model_call=1)
    logged = [record for record in caplog.records if hasattr(record, "agentrun_event")][-1]
    message = logged.getMessage()
    parsed = json.loads(message)
    assert parsed == logged.agentrun_event
    assert "\n" not in message and "\x1b" not in message
    assert len(parsed["element"]["label"]) <= 240
    assert parsed["element"]["label"].endswith("…")
    assert parsed["element"]["node"] == 0
    assert all(secret not in message for secret in (
        "request-secret", "answer-secret", "field-secret", "page-secret",
    ))


def test_default_console_logging_is_visible_and_not_duplicated(monkeypatch, capsys):
    isolated = logging.Logger("agentrun-test")
    monkeypatch.setattr(logs, "LOGGER", isolated)
    logs.configure_logging()
    logs.configure_logging()
    assert len(isolated.handlers) == 1
    assert isinstance(isolated.handlers[0], logs.AgentRunRichHandler)
    assert isolated.isEnabledFor(logging.INFO)
    decision = {
        "choice": "observed", "operation": "CLICK", "target": "1", "decision_id": "decision-1",
        "inventory_id": "inventory-1", "window_id": 1,
    }
    page = {"actions": [{"id": "observed", "node": 1, "kind": "click", "label": "Open search"}]}
    logs.log_event("model_choice", decision, page, model_call=1)
    logs.log_event("action_executed", decision, page, step=1)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert "JEV" in lines[0] and 'Jev decide pulsar en "Open search"' in lines[0]
    assert "JEV" in lines[1] and 'Jev ejecutó pulsar en "Open search"' in lines[1]
    assert not any(detail in "\n".join(lines) for detail in ("agentrun", "decision-1", "inventory-1", "{", "}"))


@pytest.mark.parametrize("reason, suffix", [
    ("stale_observation", "página cambió"),
    ("review_reconsider", "revisor"),
    ("review_unknown", "revisor"),
])
def test_discarded_proposals_report_their_outcome_without_claiming_execution(caplog, reason, suffix):
    caplog.set_level(logging.INFO, logger="agentrun")
    decision = {"choice": "observed", "operation": "CLICK", "target": "1"}
    page = {"actions": [{"id": "observed", "kind": "click", "label": "Aceptar"}]}
    logs.log_event("model_choice", decision, page)
    assert [record.getMessage() for record in caplog.records] == ['Jev decide pulsar en "Aceptar"']
    logs.log_event("action_discarded", decision, page, reason=reason)
    assert [record.getMessage() for record in caplog.records] == [
        'Jev decide pulsar en "Aceptar"', f'Jev retira pulsar en "Aceptar" ({suffix})',
    ]


@pytest.mark.parametrize("kind, http_status, cause", [
    ("billing", 402, "crédito o límite de gasto, HTTP 402"),
    ("authentication", 401, "credenciales rechazadas, HTTP 401"),
    ("authentication", 403, "credenciales rechazadas, HTTP 403"),
    ("rate_limit", 429, "límite de peticiones, HTTP 429"),
    ("unavailable", 503, "proveedor no disponible, HTTP 503"),
    ("timeout", None, "tiempo agotado"),
    ("connection", None, "conexión fallida"),
    ("request", 400, "solicitud rechazada, HTTP 400"),
    ("invalid_response", None, "respuesta inválida"),
    ("unknown", None, "fallo del proveedor"),
])
def test_unverified_provider_failure_has_one_brief_actionable_line(caplog, kind, http_status, cause):
    caplog.set_level(logging.INFO, logger="agentrun")
    decision = {"choice": "observed", "operation": "CLICK", "target": "1"}
    page = {"actions": [{"id": "observed", "kind": "click", "label": "Aceptar"}]}
    logs.log_event(
        "run_stopped", decision, page, status="unverified", reason="verification_provider_error",
        provider_error_kind=kind, provider_http_status=http_status,
    )
    assert [record.getMessage() for record in caplog.records] == [
        f'Ejecución no verificada (revisor: {cause})',
    ]


@pytest.mark.parametrize("reason, cause", [
    ("verification_unconfigured", "sin configurar"),
    ("verification_invalid_response", "respuesta inválida"),
    ("verification_budget_insufficient", "petición demasiado grande"),
    ("verification_budget", "límite de revisiones"),
    ("verification_no_progress", "sin progreso verificable"),
    ("no_new_evidence", "sin evidencia nueva"),
])
def test_unverified_local_failure_explains_the_cause(caplog, reason, cause):
    caplog.set_level(logging.INFO, logger="agentrun")
    logs.log_event(
        "run_stopped", {"choice": "DONE", "operation": "DONE"}, {"actions": []},
        status="unverified", reason=reason,
    )
    assert [record.getMessage() for record in caplog.records] == [f"Ejecución no verificada (revisor: {cause})"]


@pytest.mark.parametrize("kind, http_status", [
    ("secret-provider-message", "402 secret-response"),
    ({"message": "secret-provider-message"}, {"body": "secret-response"}),
    ("billing", True),
    ("billing", 999),
])
def test_provider_failure_logs_never_include_raw_errors_even_in_debug(caplog, kind, http_status):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    logs.log_event(
        "run_stopped", {"choice": "DONE", "operation": "DONE"}, {"actions": []},
        status="unverified", reason="verification_provider_error",
        provider_error_kind=kind, provider_http_status=http_status,
        message="secret-exception", response={"body": "secret-response"},
        api_key="secret-credential", provider_url="https://secret-endpoint.test",
    )
    assert "secret-" not in caplog.text
    detail = events(caplog)[0]
    assert detail["provider_error_kind"] in {"billing", "unknown"}
    assert "provider_http_status" not in detail
    assert not ({"message", "response", "api_key", "provider_url"} & detail.keys())
    assert len([record for record in caplog.records if record.levelno == logging.INFO]) == 1


@pytest.mark.parametrize("source, reason", [
    ("PRIVATE_SOURCE", "PRIVATE_REASON"),
    ({"PRIVATE_SOURCE": True}, ["PRIVATE_REASON"]),
    ("openrouter_in_flight_budget", {"PRIVATE_REASON": True}),
    ("openrouter_key_limit\nPRIVATE_SOURCE", "weight_exceeds_budget"),
    ("openrouter_key_limit", "\x1b[31mPRIVATE_REASON"),
])
def test_limit_diagnostic_logs_allow_only_exact_categories(caplog, source, reason):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    logs.log_event(
        "run_stopped", {"choice": "DONE", "operation": "DONE"}, {"actions": []},
        status="unverified", reason="verification_provider_error",
        provider_error_kind="billing", provider_http_status=402,
        provider_limit_source=source, provider_limit_reason=reason,
        message="PRIVATE_MESSAGE", raw="PRIVATE_RAW", remedy_hint="PRIVATE_HINT",
    )
    assert "PRIVATE" not in caplog.text
    detail = events(caplog)[0]
    assert "provider_limit_reason" not in detail
    assert not ({"raw", "message", "remedy_hint"} & detail.keys())
    if isinstance(source, str) and source in {"openrouter_in_flight_budget", "openrouter_key_limit"}:
        assert detail["provider_limit_source"] == source
    else:
        assert "provider_limit_source" not in detail


@pytest.mark.parametrize("status, source, reason, cause", [
    (402, "openrouter_credits", None, "crédito insuficiente"),
    (402, "openrouter_key_limit", None, "límite de gasto de la clave"),
    (402, "openrouter_in_flight_budget", "in_flight_budget_exhausted", "presupuesto temporal de peticiones"),
    (402, "openrouter_in_flight_budget", "weight_exceeds_budget",
     "coste de la petición superior al presupuesto temporal"),
    (403, "openrouter_guardrail_budget", None, "límite de gasto de la política"),
    (403, "openrouter_workspace_budget", None, "presupuesto de la organización"),
])
def test_known_limit_diagnostics_have_a_specific_brief_message(caplog, status, source, reason, cause):
    caplog.set_level(logging.INFO, logger="agentrun")
    logs.log_event(
        "run_stopped", {"choice": "DONE", "operation": "DONE"}, {"actions": []},
        status="unverified", reason="verification_provider_error",
        provider_error_kind="billing", provider_http_status=status,
        provider_limit_source=source, provider_limit_reason=reason,
    )
    assert [record.getMessage() for record in caplog.records] == [
        f"Ejecución no verificada (revisor: {cause}, HTTP {status})",
    ]


def test_unrecognized_stop_reason_is_not_dumped_to_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    logs.log_event(
        "run_stopped", {"choice": "DONE", "operation": "DONE"}, {"actions": []},
        status="unverified", reason="secret-provider-response",
    )
    assert "secret-provider-response" not in caplog.text
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.INFO] == [
        "Ejecución no verificada (revisor: causa no disponible)",
    ]


def test_real_repeated_actions_are_not_hidden_and_spaces_are_readable(caplog):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    decision = {"choice": "observed", "operation": "CLICK", "target": "1"}
    label = "20.000\u00a0€\n\x1b[31m\u2028\u202e"
    page = {"actions": [{"id": "observed", "kind": "click", "label": label}]}
    for step in (1, 2):
        logs.log_event("action_executed", {**decision, "decision_id": str(step)}, page, step=step)
    summaries = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert len(summaries) == 2
    assert summaries[0] == summaries[1]
    assert '20.000 €' in summaries[0] and "\\u00a0" not in summaries[0]
    assert all(char not in summaries[0] for char in ("\n", "\x1b", "\u2028", "\u202e"))
    assert all(record["element"]["label"] == label for record in events(caplog))


@pytest.mark.parametrize("close_tab", [False, True])
def test_live_example_reports_only_final_duration_and_status(monkeypatch, capsys, close_tab):
    spec = spec_from_file_location("agentrun_live_example", Path(__file__).resolve().parents[1] / "test.py")
    example = module_from_spec(spec)
    spec.loader.exec_module(example)
    load_environment = Mock()
    monkeypatch.setattr(example, "load_environment", load_environment)
    agent = Mock()
    agent.run.return_value = iter([
        {"elapsed_ms": 283, "status": "ready"},
        {"elapsed_ms": 89871, "status": "done"},
    ])
    agent_type = Mock()
    agent_type.return_value.__enter__ = Mock(return_value=agent)
    agent_type.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(example, "Agent", agent_type)
    monkeypatch.setattr("sys.argv", ["test.py"])
    example.main(["--close-tab"] if close_tab else [])
    load_environment.assert_called_once_with()
    from agentrun import SupervisorLimits

    options = {"limits": SupervisorLimits(max_actions=300)}
    if close_tab:
        options["close_tab_on_exit"] = True
    agent_type.assert_called_once_with("https://www.coches.net", example.GOAL, **options)
    output = capsys.readouterr().out
    assert "AgentRun · ejecución" in output and "89.9 s" in output and "done" in output


@pytest.mark.parametrize("configured", ["self", "ancestor"])
def test_configuration_respects_existing_handlers_and_levels(monkeypatch, configured):
    parent = logging.Logger("configured-parent", logging.DEBUG)
    isolated = logging.Logger("agentrun-test", logging.WARNING)
    isolated.parent = parent
    destination = isolated if configured == "self" else parent
    handler = logging.NullHandler()
    destination.addHandler(handler)
    monkeypatch.setattr(logs, "LOGGER", isolated)
    logs.configure_logging()
    assert isolated.level == logging.WARNING and parent.level == logging.DEBUG
    assert destination.handlers == [handler]
    if configured == "ancestor":
        assert isolated.handlers == []


def test_configuration_preserves_explicit_level_without_existing_handlers(monkeypatch):
    isolated = logging.Logger("agentrun-test", logging.ERROR)
    monkeypatch.setattr(logs, "LOGGER", isolated)
    logs.configure_logging()
    assert isolated.level == logging.ERROR


def test_delegation_logs_full_instruction_and_criteria_without_surrounding_payload(caplog):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    instruction = "Comprueba los requisitos indicados. " * 8 + "Vuelve al listado al terminar."
    criteria = ["El resultado coincide con el objetivo.", "El listado vuelve a estar visible."]
    logs.log_delegation({
        "id": "subtask-1",
        "instruction": instruction,
        "success_criteria": criteria,
        "snapshot": {"text": "page-not-for-logs"},
        "credentials": {"api_key": "credential-not-for-logs"},
        "request": {"goal": "context-not-for-logs"},
    })
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Asigna a Jev la tarea: " + json.dumps(instruction, ensure_ascii=False),
        "Criterios de éxito: " + json.dumps(criteria, ensure_ascii=False),
    ]
    assert "Vuelve al listado al terminar." in messages[0]
    assert all("not-for-logs" not in message for message in messages)


def test_delegation_logs_escape_display_controls_without_inventing_lines(caplog):
    caplog.set_level(logging.INFO, logger="agentrun")
    logs.log_delegation({
        "instruction": "Comprueba\nel resultado\x1b[31m\u202e",
        "success_criteria": ["Coincide\tcon lo solicitado.", "Confirma\u2028el resultado."],
    })
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert all(not any(char in message for char in ("\n", "\t", "\x1b", "\u202e", "\u2028"))
               for message in messages)
    assert "\\n" in messages[0] and "\\u202e" in messages[0]
    assert "\\t" in messages[1] and "\\u2028" in messages[1]


def test_delegation_logging_respects_warning_level(caplog):
    caplog.set_level(logging.WARNING, logger="agentrun")
    logs.log_delegation({"instruction": "Abre un resultado.", "success_criteria": ["El detalle es visible."]})
    assert caplog.records == []


def test_delegation_logs_observed_item_url_without_truncation_or_control_injection(caplog):
    caplog.set_level(logging.INFO, logger="agentrun")
    url = "https://example.test/item/" + "a" * 250 + "\n\u202e"
    logs.log_delegation({
        "instruction": "Abre el elemento observado.", "success_criteria": ["El detalle es visible."],
    }, item_url=url)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert messages[-1].startswith('Elemento de trabajo: "https://example.test/item/')
    assert "a" * 250 in messages[-1]
    assert "\n" not in messages[-1] and "\u202e" not in messages[-1]
    assert "\\n" in messages[-1] and "\\u202e" in messages[-1]
