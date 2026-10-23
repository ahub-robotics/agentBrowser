"""Offline runtime parity, action boundaries and default trace diagnostics."""

import json
import logging
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_agent import decision, page
from test_verification_workflow import review

from agentrun import Agent, TraceConfig
from agentrun.observability import TraceReader
from AgentBrowser import agent as loop
from AgentBrowser import logs, model, observability
from AgentBrowser.browser import fingerprint


class OfflineBrowser:
    def __init__(self):
        self.page = page()
        self.inputs = []
        self.reads = 0
        self.on_observe = None

    def observe(self, **_):
        self.reads += 1
        if self.on_observe is not None:
            self.on_observe()
        return deepcopy(self.page)

    def fresh(self, _):
        return True

    def act(self, action, _, *, text=None):
        self.inputs.append((action["id"], text))
        if action["kind"] == "fill":
            self.page["actions"][0]["value"] = text
        else:
            self.page["text"] = "Result visibly confirmed"
        self.page["fingerprint"] = fingerprint(self.page)

    def disconnect(self):
        pass


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model.CLIENT, "post", Mock(side_effect=AssertionError("No paid APIs")))


def install_policy(monkeypatch):
    choices = [decision(), {**decision("e3"), "operation": "CLICK", "target": "2"},
               {**decision("DONE"), "operation": "DONE", "target": None}]
    chooser = Mock(side_effect=choices)
    text = Mock(return_value=("full private field value", {"model": "offline", "latency_ms": 1}))
    monkeypatch.setattr(loop, "choose", chooser)
    monkeypatch.setattr(loop, "field_text", text)
    reviewer = Mock(side_effect=lambda p: review(p, status="satisfied"))
    return chooser, text, reviewer


def test_default_records_complete_direct_run_with_order_and_no_console_payload(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO, logger="agentrun")
    chooser, text, reviewer = install_policy(monkeypatch)
    browser = OfflineBrowser()
    with Agent(browser.page["url"], "private full goal", browser=browser, verifier=reviewer) as agent:
        assert agent.trace.info()["enabled"]
        reader = TraceReader("artifacts/traces", "unused")

        def assert_input_was_logged():
            if browser.inputs:
                events = reader.read(agent.trace.run_id)["events"]
                assert events[-1]["event"] == "browser.input_returned"

        browser.on_observe = assert_input_was_logged
        result = list(agent.run())[-1]
        run_id = agent.trace.run_id
    snapshot = reader.read(run_id, details=True)
    events = snapshot["events"]
    assert result["status"] == "done" and result["verification"]["status"] == "satisfied"
    assert chooser.call_count == 3 and text.call_count == 1 and reviewer.call_count == 2
    assert browser.inputs == [("e1", "full private field value"), ("e3", None)]
    assert snapshot["summary"]["metrics"]["model_calls"] == 3
    assert all(event["run_id"] == run_id for event in events)
    assert len({event["session_id"] for event in events}) == 1
    selected = [event for event in events if event["event"] == "decision.selected"]
    assert len(selected) == 3 and all(event["call_id"] and event["observation_seq"] for event in selected)
    for selection in selected:
        choice = next(event for event in events if event["event"] == "model_choice"
                      and event["decision_id"] == selection["decision_id"])
        assert choice["observation_seq"] == selection["observation_seq"]
    assert [event["observation_seq"] for event in selected] == [1, 2, 3]
    attempts = [event for event in events if event["event"] == "browser.started"]
    for attempt in attempts:
        group = [event for event in events if event.get("attempt_id") == attempt["attempt_id"]]
        assert [event["event"] for event in group] == [
            "browser.started", "browser.input_returned", "browser.observed",
        ]
        assert all(event["decision_id"] == attempt["decision_id"] for event in group)
    raw = json.dumps(snapshot)
    assert "private full goal" in raw and "full private field value" in raw
    assert "private full goal" not in caplog.text and "full private field value" not in caplog.text
    assert run_id not in caplog.text


def test_enabled_disabled_and_failed_writer_preserve_calls_and_actions(tmp_path, monkeypatch, capsys):
    outcomes = []
    for mode in ("disabled", "enabled", "failure"):
        chooser, text, reviewer = install_policy(monkeypatch)
        browser = OfflineBrowser()
        config = False if mode == "disabled" else TraceConfig(directory=tmp_path / mode)
        with Agent(browser.page["url"], "Find a book", browser=browser, verifier=reviewer, trace=config) as agent:
            with monkeypatch.context() as fault:
                if mode == "failure":
                    fault.setattr(observability, "_atomic_write", Mock(side_effect=OSError("Disk unavailable")))
                result = list(agent.run())[-1]
            outcomes.append((result["status"], result["verification"], chooser.call_count, text.call_count,
                             reviewer.call_count, browser.inputs, browser.reads, result["model_calls"],
                             result["text_model_calls"], result["verification_calls"]))
            assert agent.trace.degraded == (mode == "failure")
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert capsys.readouterr().err.count("trace degraded") == 1


def test_manual_commands_record_progress_even_with_logging_disabled(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.CRITICAL, logger="agentrun")
    chooser, _, reviewer = install_policy(monkeypatch)
    with Agent(page()["url"], "Find a book", browser=OfflineBrowser(), verifier=reviewer,
               trace=TraceConfig(directory=tmp_path)) as agent:
        predicted = agent.command("predict")
        assert predicted["status"] == "predicted"
        state = agent.command("act", agent.decision_body())
        assert state["status"] == "ready"
        events = TraceReader(tmp_path, tmp_path / "unused").read(agent.trace.run_id)["events"]
    assert chooser.call_count == 1
    progress = [event for event in events if event["event"] == "worker.progress"]
    assert [event["status"] for event in progress] == ["predicted", "ready"]
    assert progress[-1]["actions"] == 1
    assert any(event["event"] == "action_executed" for event in events)
    assert not caplog.records


def test_initial_connection_failure_closes_trace_without_retry(tmp_path, monkeypatch):
    browser = Mock(side_effect=RuntimeError("Do not leak exception content"))
    monkeypatch.setattr(loop, "Browser", browser)
    with pytest.raises(RuntimeError):
        Agent(page()["url"], "Find a book", trace=TraceConfig(directory=tmp_path))
    browser.assert_called_once()
    reader = TraceReader(tmp_path, tmp_path / "unused")
    run_id = reader.list_runs()[0]["run_id"]
    snapshot = reader.read(run_id, details=True)
    assert any(event["event"] == "browser.connection_failed" for event in snapshot["events"])
    assert snapshot["events"][-1]["event"] == "session.closed"
    assert snapshot["summary"]["status"] == "initialization_failed"
    assert "Do not leak" not in json.dumps(snapshot)


@pytest.mark.parametrize("secret", [
    "sdk-private-key-sentinel", 'sdk-"private\\key"-sentinel', "sdk-" + "private" * 100,
])
def test_registered_secrets_are_redacted_in_console_and_debug_record(tmp_path, caplog, secret):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    trace = observability.TraceContext(TraceConfig(directory=tmp_path), secrets=[secret])
    observed = page()
    observed["actions"][0]["label"] = secret
    with trace.bind():
        logs.log_event("action_executed", decision(), observed)
        logs.log_delegation({"instruction": "private instruction", "success_criteria": ["private criterion"]},
                            delegation_id="delegation-1")
    assert "sdk-" not in caplog.text
    assert "private instruction" not in caplog.text and "private criterion" not in caplog.text
    records = [record.agentrun_event for record in caplog.records if hasattr(record, "agentrun_event")]
    assert records[0]["element"]["label"] == "[REDACTED]"
    trace.close()


def test_console_redaction_failure_never_interrupts_after_action(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="agentrun")
    trace = observability.TraceContext(TraceConfig(directory=tmp_path))
    monkeypatch.setattr(observability, "redact", Mock(side_effect=RuntimeError("Redactor unavailable")))
    with trace.bind():
        logs.log_event("action_executed", decision(), page())
    assert trace.degraded and "[REDACTED]" in caplog.text
    assert "Search" not in caplog.text


def test_disabled_trace_creates_no_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with Agent(page()["url"], "Find a book", browser=OfflineBrowser(), verification=False, trace=False) as agent:
        assert not agent.snapshot()["trace"]["enabled"]
    assert not Path("artifacts/traces").exists()
