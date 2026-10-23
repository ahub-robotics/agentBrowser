"""Local observability preserves execution, full payloads and credential boundaries."""

import asyncio
import gzip
import hashlib
import json
import logging
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentrun.observability import _event_line
from AgentBrowser import observability
from AgentBrowser.observability import NULL_TRACE, TraceConfig, TraceContext, current_trace, redact


def context(tmp_path, **kwargs):
    return TraceContext(TraceConfig(directory=tmp_path / "traces"), **kwargs)


def events(trace):
    path = Path(trace.info()["directory"]) / "events" / f"{trace.session_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def payload(trace, event):
    ref = event["payload_ref"]
    raw = gzip.decompress((trace.directory / ref["path"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
    return json.loads(raw)


@pytest.mark.parametrize("configuration", [False, TraceConfig(enabled=False)])
def test_disabled_trace_does_not_write_or_log(tmp_path, monkeypatch, caplog, configuration):
    monkeypatch.chdir(tmp_path)
    mkdir = Mock(side_effect=AssertionError("Disabled traces cannot create directories"))
    monkeypatch.setattr(Path, "mkdir", mkdir)
    trace = TraceContext(configuration)
    with caplog.at_level(logging.INFO, logger="agentrun"), trace.bind(delegation_id="unused"):
        assert current_trace() is trace
        with trace.call("jev", "choose", payload={"text": "ignored"}) as call:
            call.result({"operation": "DONE"})
        assert trace.emit("anything") is None
    trace.close(status="done")
    trace.close()
    assert current_trace() is None and trace.info()["enabled"] is False
    assert not caplog.records
    mkdir.assert_not_called()
    assert NULL_TRACE.info()["enabled"] is False


def test_full_payload_is_redacted_then_atomically_written_before_event(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TRACE_API_KEY", "environment-secret")
    trace = context(tmp_path, run_id="writer", metadata={"api_key": "manifest-secret"})
    trace.register_secret("registered-secret")
    full_text = "Full page text. " * 15000
    original = {
        "page": full_text, "fields": ["environment-secret", "registered-secret"],
        "Authorization": "Bearer secret", "client": Mock(), "env": {"safe": "still omitted"},
        "item_key": "item-123", "action_key": "action-123", "usage": {"input_tokens": 123},
        "url": "https://user:password@example.test/items?q=visible&api_key=unknown-secret#token=fragment-secret",
    }
    identifier = trace.emit("observation", payload=original, password="event-secret", text="environment-secret")
    event = events(trace)[-1]
    assert event["event_id"] == identifier and event["redacted"] is True
    assert event["payload_ref"]["redacted"] is True
    safe = payload(trace, event)
    assert safe["page"] == full_text
    assert safe["item_key"] == "item-123" and safe["action_key"] == "action-123"
    assert safe["usage"] == {"input_tokens": 123}
    assert "q=visible" in safe["url"] and "example.test" in safe["url"]
    assert original["fields"] == ["environment-secret", "registered-secret"]
    raw = json.dumps(event) + json.dumps(safe) + (trace.directory / "manifest.json").read_text()
    for secret in ("environment-secret", "registered-secret", "unknown-secret", "fragment-secret",
                   "manifest-secret", "event-secret", "user:password", "Bearer secret"):
        assert secret not in raw
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in trace.directory.rglob("*") if path.is_file())
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in trace.directory.rglob("*") if path.is_dir())


def test_completed_jev_call_trace_has_human_readable_decision_and_target(tmp_path):
    trace = context(tmp_path, run_id="semantic")
    with trace.call("jev", "choose") as call:
        call.result(
            {"operation": "CLICK", "choice": "observed"},
            semantic_summary='Jev elige CLICK → "Abrir anuncio"', model="jev-1.13.0",
        )

    completed = next(row for row in events(trace) if row["event"] == "call.completed")
    assert completed["semantic_summary"] == 'Jev elige CLICK → "Abrir anuncio"'
    assert "Abrir anuncio" in _event_line(completed)
    assert completed["model"] == "jev-1.13.0"


def test_jev_call_start_and_result_are_semantic_in_console_and_trace(tmp_path, caplog):
    trace = context(tmp_path, run_id="semantic-console")
    intent = "Jev analiza 24 elementos observados para elegir operación y objetivo."
    decision = 'Jev elige CLICK → "Abrir anuncio"'
    with caplog.at_level(logging.INFO, logger="agentrun"), trace.call(
        "jev", "choose", semantic_intent=intent,
    ) as call:
        call.result({"operation": "CLICK"}, semantic_summary=decision, model="jev-1.13.0")

    messages = [record.getMessage() for record in caplog.records]
    assert any(intent in message for message in messages)
    assert not any("Trace started jev/choose" in message or "Trace completed jev/choose" in message
                   for message in messages)
    rows = events(trace)
    started = next(row for row in rows if row["event"] == "call.started")
    completed = next(row for row in rows if row["event"] == "call.completed")
    assert intent in _event_line(started)
    assert decision in _event_line(completed)


def test_same_sanitized_payload_deduplicates_blob_and_preserves_reserved_envelope(tmp_path):
    trace = context(tmp_path, run_id="canonical")
    first = trace.emit("one", payload={"b": 2, "a": 1}, run_id="injected", seq=999, event_id="fake")
    second = trace.emit("two", payload={"a": 1, "b": 2})
    records = events(trace)
    assert records[-2]["payload_ref"] == records[-1]["payload_ref"]
    assert len(list((trace.directory / "blobs").iterdir())) == 1
    assert records[-2]["run_id"] == "canonical" and records[-2]["seq"] == 2
    assert first == f"{trace.session_id}:2" and second == f"{trace.session_id}:3"


def test_nested_calls_correlate_and_restore_context_without_logging_payload(tmp_path, caplog):
    trace = context(tmp_path)
    with caplog.at_level(logging.INFO, logger="agentrun"), trace.bind(delegation_id="delegate-1"):
        with trace.call("supervisor", "review", payload={"secret_prose": "PRIVATE_PAYLOAD"},
                        phase="review", item_id="call-item") as parent:
            with trace.bind(decision_id="decision-1"):
                with trace.call("jev", "choose", attempt_id="call-attempt") as child:
                    trace.emit("http.response", component="jev", status_code=200)
                    child.result({"operation": "CLICK"}, usage={"output_tokens": 7})
            trace.emit("parent.event")
            parent.result({"next_action": "continue"})
    assert current_trace() is None
    rows = events(trace)
    nested = next(row for row in rows if row["event"] == "call.started" and row["component"] == "jev")
    assert nested["parent_call_id"] == parent.call_id
    assert nested["delegation_id"] == "delegate-1" and nested["decision_id"] == "decision-1"
    http = next(row for row in rows if row["event"] == "http.response")
    assert http["call_id"] == child.call_id
    assert http["delegation_id"] == "delegate-1" and http["decision_id"] == "decision-1"
    assert http["phase"] == "review" and http["item_id"] == "call-item" and http["attempt_id"] == "call-attempt"
    restored = next(row for row in rows if row["event"] == "parent.event")
    assert restored["call_id"] == parent.call_id and "decision_id" not in restored
    assert "attempt_id" not in restored and restored["item_id"] == "call-item"
    completed = [row for row in rows if row["event"] == "call.completed"]
    assert all(row["duration_ms"] >= 0 for row in completed)
    assert payload(trace, completed[0]) == {"operation": "CLICK"}
    assert completed[0]["usage"] == {"output_tokens": 7}
    assert "PRIVATE_PAYLOAD" not in caplog.text
    assert "Trace started" not in caplog.text and "Trace completed" not in caplog.text
    assert trace.run_id not in caplog.text


@pytest.mark.parametrize(("error", "event"), [(ValueError("PRIVATE_ERROR"), "call.failed"),
                                              (KeyboardInterrupt("PRIVATE_ERROR"), "call.interrupted")])
def test_call_errors_preserve_original_exception_without_messages_or_locals(tmp_path, error, event):
    trace = context(tmp_path)
    with pytest.raises(type(error)) as caught, trace.bind(item_id="item-1"):
        with trace.call("text", "generate"):
            private_local = "PRIVATE_LOCAL"
            assert private_local
            raise error
    assert caught.value is error and current_trace() is None
    record = events(trace)[-1]
    assert record["event"] == event and record["error_type"] == type(error).__name__
    assert record["stack"] and all(set(frame) == {"file", "line", "function"} for frame in record["stack"])
    assert "PRIVATE_ERROR" not in json.dumps(record) and "PRIVATE_LOCAL" not in json.dumps(record)


def test_context_is_isolated_between_async_tasks(tmp_path):
    one, two = context(tmp_path, run_id="one"), context(tmp_path, run_id="two")

    async def record(trace):
        with trace.bind(item_id=trace.run_id):
            await asyncio.sleep(0)
            assert current_trace() is trace
            trace.emit("task.event")

    async def run():
        await asyncio.gather(record(one), record(two))

    asyncio.run(run())
    assert current_trace() is None
    assert events(one)[-1]["item_id"] == "one"
    assert events(two)[-1]["item_id"] == "two"


def test_resume_creates_new_session_and_close_is_idempotent(tmp_path):
    trace = context(tmp_path, run_id="resume", state_path=tmp_path / "state.sqlite", metadata={"version": 2})
    trace.close(status="blocked")
    before = events(trace)
    trace.close(status="done")
    assert events(trace) == before
    resumed = context(tmp_path, run_id="resume", resumed=True)
    assert resumed.session_id != trace.session_id and resumed.run_id == trace.run_id
    assert events(resumed)[0]["event"] == "run.resumed" and events(resumed)[0]["seq"] == 1
    resumed.close(status="done")
    manifest = json.loads((trace.directory / "manifest.json").read_text())
    assert manifest["state_path"] == str(tmp_path / "state.sqlite")
    assert manifest["metadata"] == {"version": 2}
    assert [session["status"] for session in manifest["sessions"]] == ["blocked", "done"]
    assert len(list((trace.directory / "events").glob("*.jsonl"))) == 2


def test_blob_write_failure_does_not_publish_dangling_event_or_change_application(tmp_path, monkeypatch, capsys):
    trace = context(tmp_path)
    before = events(trace)
    monkeypatch.setattr(observability, "_atomic_write", Mock(side_effect=OSError("PRIVATE_FILESYSTEM_PATH")))
    assert trace.emit("lost", payload={"a": "b"}) is None
    with trace.call("jev", "choose") as call:
        call.result({"operation": "DONE"})
    trace.close(status="done")
    assert events(trace) == before
    assert trace.info()["degraded"] is True
    warning = capsys.readouterr().err
    assert warning.count("trace degraded") == 1 and "PRIVATE_FILESYSTEM_PATH" not in warning


def test_unwritable_destination_is_degraded_and_agent_code_still_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=PermissionError("PRIVATE_PATH")))
    trace = context(tmp_path)
    result = []
    with trace.call("supervisor", "initialize"):
        result.append("executed")
    trace.close(status="blocked")
    assert result == ["executed"] and trace.info()["degraded"] is True


def test_redaction_handles_cycles_opaque_clients_and_url_encoded_known_secrets(monkeypatch):
    monkeypatch.setenv("TEST_TRACE_API_KEY", "private/encoded?value")

    class Client:
        def __repr__(self):
            raise AssertionError("Never serialize client repr")

    cycle = {"opaque": Client(), "value": "private%2Fencoded%3Fvalue"}
    cycle["cycle"] = cycle
    safe, changed = redact(cycle)
    assert changed and safe["cycle"] == {"_trace_omitted": "cycle"}
    assert safe["opaque"] == {"_trace_omitted": "unsupported_type", "type": "Client"}
    assert "private" not in json.dumps(safe)


def test_trace_clock_does_not_consume_worker_fake_clock(tmp_path, monkeypatch):
    trace = context(tmp_path)
    fake = Mock(side_effect=AssertionError("Worker clock must not be consumed"))
    monkeypatch.setattr("time.perf_counter", fake)
    with trace.call("jev", "choose") as call:
        call.result({"operation": "DONE"})
    assert events(trace)[-1]["duration_ms"] >= 0
    fake.assert_not_called()


def test_reserved_call_result_fields_do_not_override_envelope_or_raise(tmp_path):
    trace = context(tmp_path)
    with trace.call("jev", "choose", event="ignored", duration_ms="ignored") as call:
        call.result({"operation": "DONE"}, event="ignored", component="ignored", payload="ignored",
                    operation="ignored", duration_ms="ignored", run_id="ignored")
    row = events(trace)[-1]
    assert row["event"] == "call.completed" and row["component"] == "jev" and row["operation"] == "choose"
    assert row["run_id"] == trace.run_id and isinstance(row["duration_ms"], (int, float))


def test_malformed_url_still_redacts_credentials_without_removing_query_context():
    safe, changed = redact("See https://user:password@[malformed/path?q=visible&api_key=private-query#token=private-fragment")
    assert changed and "q=visible" in safe
    assert not any(secret in safe for secret in ("user:password", "private-query", "private-fragment"))


def test_constructor_secrets_redact_initial_manifest_before_first_write(tmp_path):
    secret = "injected-sdk-credential-only-in-memory"
    trace = context(tmp_path, secrets=(secret,), metadata={"goal": f"Goal containing {secret}"})
    manifest = (trace.directory / "manifest.json").read_text()
    assert secret not in manifest and "Goal containing [REDACTED]" in manifest
    safe, changed = trace.redact({"message": f"Received {secret}"})
    assert changed and safe == {"message": "Received [REDACTED]"}
    trace.register_secret("later-secret")
    assert trace.redact("later-secret")[0] == "[REDACTED]"


def test_context_redact_failure_omits_data_and_never_exposes_original(tmp_path, monkeypatch, capsys):
    trace = context(tmp_path)
    monkeypatch.setattr(observability, "redact", Mock(side_effect=RuntimeError("PRIVATE_ERROR")))
    safe, changed = trace.redact({"message": "PRIVATE_ORIGINAL"})
    assert changed and safe == {"_trace_omitted": "redaction_failed"}
    assert trace.info()["degraded"] is True
    assert "PRIVATE_ERROR" not in capsys.readouterr().err
