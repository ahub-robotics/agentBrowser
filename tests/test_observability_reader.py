"""Offline trace inspection, cursors, historical reads and credential-safe export."""

import gzip
import hashlib
import json
import sqlite3
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentrun import cli
from agentrun.observability import TraceReader, TraceReadError
from AgentBrowser.observability import TraceConfig, TraceContext


def historical(root, run_id="saved-1", *, status="blocked", schema=1):
    path = root / run_id / "state.sqlite"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE supervisor_schema(singleton INTEGER,version INTEGER);
        CREATE TABLE supervisor_runs(run_id TEXT,state_json TEXT,target_id TEXT,updated_at REAL);
        CREATE TABLE supervisor_attempts(id TEXT,run_id TEXT,delegation_id TEXT,item_key TEXT,
            action_key TEXT,state TEXT,record_json TEXT,created_at REAL,updated_at REAL);
        CREATE TABLE supervisor_delegations(run_id TEXT,id TEXT,input_json TEXT,result_json TEXT,updated_at REAL);
    """)
    connection.execute("INSERT INTO supervisor_schema VALUES(1,?)", (schema,))
    state = {"goal": "Inspect every item", "contract_version": 2, "status": status, "supervisor_calls": 3,
             "worker": {"model_calls": 4, "model_http_attempts": 5}, "contract_phase": "review",
             "stop_reason": "provider_error", "api_key": "stored-secret", "progress_revision": 2}
    connection.execute("INSERT INTO supervisor_runs VALUES(?,?,?,?)", (run_id, json.dumps(state), "tab", 123.0))
    for number, action_status in enumerate(("observed", "dispatched", "rejected")):
        connection.execute("INSERT INTO supervisor_attempts VALUES(?,?,?,?,?,?,?,?,?)", (
            str(number), run_id, "delegation-1", "item-1", "action", action_status,
            json.dumps({"before": {}, "after": None, "authorization": "stored-authorization"}), number, number,
        ))
    connection.execute("INSERT INTO supervisor_delegations VALUES(?,?,?,?,?)", (
        run_id, "delegation-1", json.dumps({"instruction": "Inspect"}), None, 1,
    ))
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def recorded(tmp_path):
    trace_root, state_root = tmp_path / "traces", tmp_path / "state"
    trace = TraceContext(TraceConfig(directory=trace_root), run_id="saved-1")
    return trace, TraceReader(trace_root, state_root)


def test_live_writer_payloads_and_logical_calls_are_read_without_confusing_http(recorded):
    trace, reader = recorded
    trace.emit("call.started", component="jev", call_id="call-1", payload={"prompt": "Complete input"})
    trace.emit("http.response", component="jev", call_id="call-1", payload={"raw": "Complete output"})
    snapshot = reader.read("saved-1", details=True)
    assert snapshot["summary"]["open_calls"] == 1
    assert snapshot["events"][1]["payload"]["prompt"] == "Complete input"
    trace.emit("call.completed", component="jev", call_id="call-1")
    assert reader.read("saved-1")["summary"]["open_calls"] == 0


def test_failed_and_interrupted_calls_close_only_matching_component(recorded):
    trace, reader = recorded
    for component in ("jev", "text"):
        trace.emit("call.started", component=component, call_id="shared")
    trace.emit("call.failed", component="text", call_id="shared")
    assert reader.read("saved-1")["summary"]["open_calls"] == 1
    trace.emit("call.interrupted", component="jev", call_id="shared")
    assert reader.read("saved-1")["summary"]["open_calls"] == 0


def test_open_calls_do_not_depend_on_wall_clock_order(recorded):
    trace, reader = recorded
    trace.emit("call.started", component="jev", call_id="clock-change")
    trace.emit("call.completed", component="jev", call_id="clock-change")
    path = trace.directory / "events" / f"{trace.session_id}.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[-1]["timestamp"] = "2000-01-01T00:00:00Z"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    assert reader.read("saved-1")["summary"]["open_calls"] == 0


def test_missing_manifest_session_is_reported_as_partial(recorded):
    trace, reader = recorded
    resumed = TraceContext(TraceConfig(directory=reader.trace_dir), run_id="saved-1", resumed=True)
    (trace.directory / "events" / f"{trace.session_id}.jsonl").unlink()
    snapshot = reader.read("saved-1")
    assert snapshot["coverage"] == "partial"
    assert any("manifest has no readable" in warning for warning in snapshot["warnings"])
    assert snapshot["events"][0]["session_id"] == resumed.session_id


def test_direct_summary_uses_latest_recorded_progress_without_inventing_counters(recorded):
    trace, reader = recorded
    trace.emit("worker.progress", status="blocked", stop_reason="provider_error",
               metrics={"model_calls": 2, "model_http_attempts": 4}, actions=1)
    trace.close(status="blocked")
    summary = reader.read("saved-1")["summary"]
    assert summary["status"] == "blocked"
    assert summary["stop_reason"] == "provider_error"
    assert summary["metrics"] == {"model_calls": 2, "model_http_attempts": 4, "actions": 1}


@pytest.mark.parametrize("verification", ["disabled", "satisfied", "unknown"])
def test_done_and_recorded_verification_are_separate_in_summary_and_cli(recorded, capsys, verification):
    trace, reader = recorded
    trace.emit("worker.progress", status="done", payload={"verification": {"status": verification}})
    summary = reader.read("saved-1")["summary"]
    assert summary["status"] == "done" and summary["verification_status"] == verification
    assert cli.main(["traces", "show", "saved-1", "--trace-dir", str(reader.trace_dir)]) == 0
    output = capsys.readouterr().out
    assert f"Recorded verification: {verification}" in output
    assert "independent outcome verification is not inferred" in output


def test_supervised_summary_does_not_promote_worker_done_to_global_done(recorded):
    _, reader = recorded
    trace = TraceContext(TraceConfig(directory=reader.trace_dir), run_id="saved-1", mode="supervised", resumed=True)
    trace.emit("run.progress", status="ready", payload={"verification": {"status": "incomplete"}})
    trace.emit("worker.progress", component="worker", status="done",
               payload={"verification": {"status": "disabled"}})
    summary = reader.read("saved-1")["summary"]
    assert summary["status"] == "ready"
    assert summary["verification_status"] == "incomplete"


def test_historical_verification_has_priority_over_newer_trace_claims(recorded):
    trace, reader = recorded
    historical(reader.state_dir, status="done")
    trace.emit("worker.progress", status="done", payload={"verification": {"status": "satisfied"}})
    summary = reader.read("saved-1")["summary"]
    assert summary["status"] == "done" and summary["verification_status"] == "unknown"


def test_cursor_waits_for_partial_line_and_handles_resumed_session_without_duplicates(recorded):
    trace, reader = recorded
    events, cursor, warnings = reader.events("saved-1")
    assert len(events) == 1 and not warnings
    path = trace.directory / "events" / f"{trace.session_id}.jsonl"
    event = {**events[0], "seq": 2, "event_id": f"{trace.session_id}:2", "event": "call.started"}
    body = json.dumps(event).encode()
    with path.open("ab") as stream:
        stream.write(body[:50])
    events, next_cursor, warnings = reader.events("saved-1", cursor=cursor)
    assert not events and next_cursor == cursor and "incomplete" in warnings[0]
    with path.open("ab") as stream:
        stream.write(body[50:] + b"\n")
    resumed = TraceContext(TraceConfig(directory=reader.trace_dir), run_id="saved-1", resumed=True)
    events, next_cursor, warnings = reader.events("saved-1", cursor=cursor)
    assert {event["event_id"] for event in events} == {f"{trace.session_id}:2", f"{resumed.session_id}:1"}
    assert not warnings
    assert reader.events("saved-1", cursor=next_cursor)[0] == []


def test_invalid_interior_line_is_reported_and_does_not_hide_later_events(recorded):
    trace, reader = recorded
    path = trace.directory / "events" / f"{trace.session_id}.jsonl"
    with path.open("ab") as stream:
        stream.write(b"not-json\n")
    trace.emit("call.failed", call_id="bad")
    snapshot = reader.read("saved-1")
    assert snapshot["coverage"] == "partial"
    assert any("invalid event" in warning for warning in snapshot["warnings"])
    assert snapshot["events"][-1]["event"] == "call.failed"


def test_snapshot_has_fixed_event_boundaries_while_writer_continues(recorded, monkeypatch):
    trace, reader = recorded
    original = reader.events
    written = False

    def read_then_append(*args, **kwargs):
        nonlocal written
        result = original(*args, **kwargs)
        if not written:
            written = True
            trace.emit("call.started", call_id="later")
        return result

    monkeypatch.setattr(reader, "events", read_then_append)
    assert len(reader.read("saved-1")["events"]) == 1
    assert len(reader.read("saved-1")["events"]) == 2


def test_historical_read_does_not_migrate_or_construct_a_run_store(tmp_path, monkeypatch):
    path = historical(tmp_path / "state")
    before = path.read_bytes()
    monkeypatch.setattr("agentrun.supervisor_store.RunStore", Mock(side_effect=AssertionError("writer used")))
    reader = TraceReader(tmp_path / "missing-traces", tmp_path / "state")
    snapshot = reader.read("saved-1")
    assert snapshot["coverage"] == "partial"
    assert snapshot["summary"]["metrics"] == {"actions": 2, "jev_calls": 4, "jev_http_attempts": 5,
                                               "supervisor_calls": 3}
    assert snapshot["summary"]["phase"] == "review"
    assert snapshot["journal"]["delegations"][0]["input"] == {"instruction": "Inspect"}
    assert path.read_bytes() == before
    assert not (tmp_path / "missing-traces").exists()
    assert not (tmp_path / "state" / ".supervisor-locks").exists()
    assert "stored-secret" not in json.dumps(snapshot)


def test_reader_can_observe_committed_wal_while_writer_keeps_transaction_open(tmp_path):
    path = historical(tmp_path / "state")
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE supervisor_runs SET updated_at=999")
    reader = TraceReader(tmp_path / "traces", tmp_path / "state")
    assert reader.read("saved-1")["journal"]["updated_at"] == 123
    writer.commit()
    assert reader.read("saved-1")["journal"]["updated_at"] == 999
    writer.close()


def test_custom_state_path_requires_explicit_configured_root(recorded, tmp_path):
    trace, reader = recorded
    other = tmp_path / "other"
    path = historical(other)
    manifest = trace.directory / "manifest.json"
    content = json.loads(manifest.read_text())
    content["state_path"] = str(path)
    manifest.write_text(json.dumps(content))
    snapshot = reader.read("saved-1")
    assert snapshot["journal"] is None
    assert any("outside --state-dir" in warning for warning in snapshot["warnings"])
    assert TraceReader(reader.trace_dir, other).read("saved-1")["journal"] is not None


def test_unsupported_historical_schema_is_not_initialized(recorded):
    _, reader = recorded
    path = historical(reader.state_dir, schema=999)
    before = path.read_bytes()
    snapshot = reader.read("saved-1")
    assert snapshot["journal"] is None and snapshot["coverage"] == "partial"
    assert path.read_bytes() == before


def test_export_includes_referenced_sanitized_payloads_and_snapshot_only(recorded, tmp_path, monkeypatch):
    trace, reader = recorded
    historical(reader.state_dir)
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "credential-sentinel")
    trace.emit("call.completed", payload={"prompt": "Complete ordinary field text", "api_key": "secret",
                                          "response": "credential-sentinel", "url": "https://u:p@test/?token=hidden"})
    (trace.directory / ".env").write_text("NEVER_EXPORT=secret")
    (trace.directory / "blobs" / ("0" * 64 + ".json.gz")).write_bytes(gzip.compress(b'"orphan"'))
    output = tmp_path / "evidence.zip"
    manifest = reader.export("saved-1", output)
    assert manifest["atomic_with_journal"] is False
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert len([name for name in names if name.startswith("blobs/")]) == 1
        assert not any(name.endswith((".sqlite", ".env")) for name in names)
        all_data = b"".join(gzip.decompress(archive.read(name)) if name.endswith(".gz") else archive.read(name)
                            for name in names)
        for forbidden in (b"credential-sentinel", b"stored-secret", b"stored-authorization", b"NEVER_EXPORT"):
            assert forbidden not in all_data
        assert b"Complete ordinary field text" in all_data
        event = json.loads(archive.read("events.jsonl").splitlines()[-1])
        body = gzip.decompress(archive.read(event["payload_ref"]["path"]))
        assert hashlib.sha256(body).hexdigest() == event["payload_ref"]["sha256"]
    with pytest.raises(TraceReadError, match="already exists"):
        reader.export("saved-1", output)


def test_missing_blob_is_declared_in_details_and_export(recorded, tmp_path):
    trace, reader = recorded
    trace.emit("call.completed", payload={"response": "text"})
    for path in (trace.directory / "blobs").iterdir():
        path.unlink()
    assert reader.read("saved-1", details=True)["coverage"] == "partial"
    manifest = reader.export("saved-1", tmp_path / "partial.zip")
    assert manifest["coverage"] == "partial" and any("missing" in warning for warning in manifest["warnings"])


@pytest.mark.parametrize("broken", [gzip.compress(b"content")[:-4], b"\x1f\x8b\x08\x00" + b"\x00" * 20])
def test_corrupt_gzip_is_partial_coverage_instead_of_an_uncaught_error(recorded, tmp_path, broken):
    trace, reader = recorded
    trace.emit("call.completed", payload={"response": "text"})
    for path in (trace.directory / "blobs").iterdir():
        path.write_bytes(broken)
    assert reader.read("saved-1", details=True)["coverage"] == "partial"
    assert reader.export("saved-1", tmp_path / "corrupt.zip")["coverage"] == "partial"


def test_failed_export_does_not_leave_an_apparently_finished_archive(recorded, tmp_path, monkeypatch):
    _, reader = recorded
    destination = tmp_path / "failed.zip"
    monkeypatch.setattr(zipfile.ZipFile, "writestr", Mock(side_effect=OSError("disk full")))
    with pytest.raises(TraceReadError):
        reader.export("saved-1", destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".agentrun-export-*"))


@pytest.mark.parametrize("run_id", ["../outside", "a/b", "..", "", "/tmp/run"])
def test_run_identifier_traversal_is_rejected(recorded, run_id):
    _, reader = recorded
    with pytest.raises(TraceReadError):
        reader.read(run_id)


def test_blob_paths_and_symlinks_cannot_escape_the_run(recorded, tmp_path):
    trace, reader = recorded
    trace.emit("call.completed", payload={"response": "text"})
    ref = reader.read("saved-1")["events"][-1]["payload_ref"]
    with pytest.raises(TraceReadError, match="reference"):
        reader.blob("saved-1", {**ref, "path": "../../secret.json.gz"})
    path = trace.directory / ref["path"]
    path.unlink()
    outside = tmp_path / "outside.gz"
    outside.write_bytes(gzip.compress(b"{}"))
    path.symlink_to(outside)
    with pytest.raises(TraceReadError, match="escapes"):
        reader.blob("saved-1", ref)


def test_trace_cli_routes_before_environment_and_never_constructs_agents(recorded, monkeypatch, capsys):
    trace, reader = recorded
    trace.emit("call.failed", component="supervisor", call_id="bad", delegation_id="d-1", payload={"full": "detail"})
    forbidden = Mock(side_effect=AssertionError("Must not execute"))
    monkeypatch.setattr(cli, "load_environment", forbidden)
    monkeypatch.setattr(cli, "Agent", forbidden)
    monkeypatch.setattr(cli, "SupervisedAgent", forbidden)
    assert cli.main(["traces", "show", "saved-1", "--trace-dir", str(reader.trace_dir), "--details",
                     "--component", "supervisor", "--delegation", "d-1", "--errors", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["events"]) == 1 and data["events"][0]["payload"] == {"full": "detail"}
    forbidden.assert_not_called()


def test_tail_prints_cursor_and_can_resume_without_repeating_events(recorded, capsys):
    trace, reader = recorded
    args = ["traces", "--trace-dir", str(reader.trace_dir), "tail", "saved-1", "--json"]
    assert cli.main(args) == 0
    first = capsys.readouterr()
    cursor = first.err.split("Cursor: ")[-1].strip()
    trace.emit("call.started", call_id="next")
    assert cli.main([*args, "--cursor", cursor]) == 0
    second = capsys.readouterr()
    assert len(second.out.splitlines()) == 1
    assert json.loads(second.out)["call_id"] == "next"


def test_mid_line_cursor_fails_without_skipping_a_record(recorded, capsys):
    trace, reader = recorded
    cursor = json.dumps({trace.session_id: 10})
    assert cli.main(["traces", "tail", "saved-1", "--trace-dir", str(reader.trace_dir),
                     "--cursor", cursor]) == 2
    result = capsys.readouterr()
    assert not result.out and "Trace read failed" in result.err


def test_tail_follow_reads_new_session_and_returns_cursor_when_interrupted(recorded, capsys, monkeypatch):
    trace, reader = recorded
    count = 0

    def wait(_seconds):
        nonlocal count
        count += 1
        if count == 1:
            trace.emit("call.completed", call_id="arrived")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr("agentrun.observability.time.sleep", wait)
    assert cli.main(["traces", "tail", "saved-1", "--trace-dir", str(reader.trace_dir),
                     "--follow", "--json"]) == 130
    result = capsys.readouterr()
    assert len(result.out.splitlines()) == 2
    assert json.loads(result.out.splitlines()[-1])["call_id"] == "arrived"
    assert "Cursor:" in result.err


def test_cli_list_includes_historical_runs_and_export_works_without_environment(tmp_path, monkeypatch, capsys):
    historical(tmp_path / "state")
    forbidden = Mock(side_effect=AssertionError("Environment must not be loaded"))
    monkeypatch.setattr(cli, "load_environment", forbidden)
    base = ["traces", "--trace-dir", str(tmp_path / "traces"), "--state-dir", str(tmp_path / "state")]
    assert cli.main([*base, "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["run_id"] == "saved-1" and listed[0]["coverage"] == "partial"
    assert cli.main([*base, "export", "saved-1", "--output", str(tmp_path / "history.zip")]) == 0
    forbidden.assert_not_called()


@pytest.mark.parametrize("mode", ["direct", "supervised", "resume"])
@pytest.mark.parametrize("flag", ["--no-trace", "--trace-dir"])
def test_run_trace_flags_forward_only_explicit_options(monkeypatch, tmp_path, mode, flag):
    monkeypatch.setattr(cli, "load_environment", Mock())
    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.return_value = iter([{"status": "done", "verification": {"status": "satisfied"}}])
    constructor = Mock(return_value=instance)
    constructor.resume.return_value = instance
    monkeypatch.setattr(cli, "Agent" if mode == "direct" else "SupervisedAgent", constructor)
    args = ["run", "--resume", "saved-1"] if mode == "resume" else [
        "run", "--url", "https://example.test", "--goal", "Inspect",
    ]
    if mode == "supervised":
        args.append("--supervised")
    args += [flag] if flag == "--no-trace" else [flag, str(tmp_path)]
    assert cli.main(args) == 0
    passed = (constructor.resume if mode == "resume" else constructor).call_args.kwargs["trace"]
    assert passed is False if flag == "--no-trace" else passed == TraceConfig(directory=str(tmp_path))


@pytest.mark.parametrize("flag", ["--no-trace", "--trace-dir"])
def test_live_example_forwards_explicit_trace_flags_without_running_models(monkeypatch, tmp_path, flag):
    spec = spec_from_file_location("agentrun_example_traces", Path(__file__).resolve().parents[1] / "test.py")
    example = module_from_spec(spec)
    spec.loader.exec_module(example)
    monkeypatch.setattr(example, "load_environment", Mock())
    instance = Mock()
    instance.run.return_value = iter([{"status": "done", "elapsed_ms": 1,
                                      "verification": {"status": "satisfied"}}])
    constructor = Mock()
    constructor.return_value.__enter__ = Mock(return_value=instance)
    constructor.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(example, "DirectAgent", constructor)
    args = ["--direct", flag] + ([str(tmp_path)] if flag == "--trace-dir" else [])
    assert example.main(args) == 0
    passed = constructor.call_args.kwargs["trace"]
    assert passed is False if flag == "--no-trace" else passed == TraceConfig(directory=str(tmp_path))
