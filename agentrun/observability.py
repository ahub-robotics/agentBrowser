"""Local trace readers. Reading never initializes agents, credentials or stores."""

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

from AgentBrowser.observability import redact

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_LINE = 8 * 1024 * 1024
_MAX_BLOB = 64 * 1024 * 1024


class TraceReadError(ValueError):
    """An invalid request or unavailable local run, safe to show on the CLI."""


class TraceCursorError(TraceReadError):
    """An invalid cursor must not be mistaken for a partially readable source."""


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TraceReadError("Invalid run or session identifier.")
    return value


def _safe_path(root, *parts):
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise TraceReadError("A trace path escapes its configured directory.")
    return path


def _clean(value):
    return redact(value)[0]


def _display(value):
    """Escape control characters while keeping ordinary Unicode readable."""
    return "".join(char if char.isprintable() else json.dumps(char, ensure_ascii=True)[1:-1]
                   for char in str(value))


def _json(value):
    return json.dumps(_clean(value), ensure_ascii=True, indent=2, allow_nan=False)


def _is_error(event):
    name = str(event.get("event", "")).lower()
    return (any(word in name for word in ("error", "failed", "invalid", "rejected", "degraded"))
            or str(event.get("status")) in {"error", "failed", "invalid", "rejected"}
            or event.get("accepted") is False)


def _refs(value):
    """Only explicit payload_ref fields authorize reading a blob."""
    if isinstance(value, dict):
        ref = value.get("payload_ref")
        if isinstance(ref, dict):
            yield ref
        for key, child in value.items():
            if key != "payload_ref":
                yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


def _open_calls(events):
    pending, finished = {}, set()
    for event in events:
        call_id = event.get("call_id")
        if not call_id:
            continue
        key = (event.get("session_id"), event.get("component"), str(call_id))
        name = str(event.get("event", "")).lower()
        if name == "call.started":
            pending[key] = event
        elif name in {"call.completed", "call.failed", "call.interrupted"}:
            finished.add(key)
    return [event for key, event in pending.items() if key not in finished]


class TraceReader:
    """Read bounded JSONL pages and coherent journal snapshots without writer APIs."""

    def __init__(self, trace_dir="artifacts/traces", state_dir="artifacts/supervised"):
        self.trace_dir = Path(trace_dir).expanduser().absolute()
        self.state_dir = Path(state_dir).expanduser().absolute()

    def _run_dir(self, run_id):
        return _safe_path(self.trace_dir, _identifier(run_id))

    def manifest(self, run_id):
        path = _safe_path(self._run_dir(run_id), "manifest.json")
        if not path.exists():
            return {}, []
        try:
            if path.stat().st_size > _MAX_LINE:
                raise ValueError
            value = json.loads(path.read_text())
            if (not isinstance(value, dict) or value.get("run_id") != run_id or value.get("schema_version") != 1
                    or not isinstance(value.get("metadata", {}), dict)
                    or not isinstance(value.get("sessions", []), list)):
                raise ValueError
            return _clean(value), []
        except (OSError, ValueError, TypeError):
            return {}, ["Trace manifest is unreadable or has an unsupported schema."]

    def events(self, run_id, *, cursor=None, limit=1000, end_offsets=None):
        """Return complete lines and byte cursors; leave partial final lines unread."""
        root = self._run_dir(run_id)
        directory = _safe_path(root, "events")
        offsets = dict(cursor or {})
        if any(not isinstance(key, str) or not _ID.fullmatch(key) or type(value) is not int or value < 0
               for key, value in offsets.items()):
            raise TraceReadError("Invalid trace cursor.")
        warnings, events = [], []
        if not directory.exists():
            return events, offsets, warnings
        for candidate in sorted(directory.glob("*.jsonl")):
            session = candidate.stem
            if end_offsets is not None and session not in end_offsets:
                continue
            if not _ID.fullmatch(session):
                warnings.append("An invalid trace session filename was skipped.")
                continue
            try:
                path = _safe_path(root, "events", candidate.name)
                offset = offsets.get(session, 0)
                with path.open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    if end_offsets is not None:
                        size = min(size, end_offsets[session])
                    if offset > size:
                        warnings.append(f"Session {session} shrank; its cursor was reset.")
                        offset = 0
                    if offset:
                        stream.seek(offset - 1)
                        if stream.read(1) != b"\n":
                            raise TraceCursorError("Trace cursor is not at a line boundary.")
                    stream.seek(offset)
                    while len(events) < limit and stream.tell() < size:
                        start = stream.tell()
                        line = stream.readline(min(_MAX_LINE + 1, size - start))
                        if not line:
                            break
                        if len(line) > _MAX_LINE:
                            warnings.append(f"Session {session} has an oversized event at byte {start}.")
                            # Do not interpret the remainder as a new event.
                            while line and not line.endswith(b"\n") and stream.tell() < size:
                                line = stream.readline(min(_MAX_LINE + 1, size - stream.tell()))
                            offsets[session] = stream.tell()
                            continue
                        if not line.endswith(b"\n"):
                            warnings.append(f"Session {session} has an incomplete final line at byte {start}.")
                            break
                        offsets[session] = stream.tell()
                        try:
                            event = json.loads(line)
                            seq = event.get("seq") if isinstance(event, dict) else None
                            if (event.get("schema_version") != 1 or event.get("run_id") != run_id
                                    or event.get("session_id") != session or type(seq) is not int or seq < 1
                                    or event.get("event_id") != f"{session}:{seq}"
                                    or not all(isinstance(event.get(key), str)
                                               for key in ("event", "component", "timestamp"))):
                                raise ValueError
                            events.append(_clean(event))
                        except (ValueError, TypeError, AttributeError):
                            warnings.append(f"Session {session} has an invalid event at byte {start}.")
                if len(events) >= limit:
                    break
            except TraceCursorError:
                raise
            except (OSError, TraceReadError):
                warnings.append(f"Session {session} could not be read safely.")
        events.sort(key=lambda event: (str(event.get("timestamp", "")), event["session_id"], event["seq"]))
        return events, offsets, warnings

    def all_events(self, run_id):
        result, cursor, warnings = [], {}, []
        directory = _safe_path(self._run_dir(run_id), "events")
        ends = {}
        for path in directory.glob("*.jsonl"):
            try:
                safe = _safe_path(self._run_dir(run_id), "events", path.name)
                ends[path.stem] = safe.stat().st_size
            except (OSError, TraceReadError):
                warnings.append("A session file could not be included in the trace snapshot.")
        while True:
            page, next_cursor, issues = self.events(run_id, cursor=cursor, end_offsets=ends)
            result.extend(page)
            warnings.extend(issues)
            if next_cursor == cursor:
                break
            cursor = next_cursor
        result.sort(key=lambda event: (str(event.get("timestamp", "")), event["session_id"], event["seq"]))
        if len({event["event_id"] for event in result}) != len(result):
            warnings.append("Duplicate event identities were recorded; ordering and call coverage may be incomplete.")
        return result, cursor, list(dict.fromkeys(warnings))

    def blob(self, run_id, ref):
        digest = ref.get("sha256") if isinstance(ref, dict) else None
        if (not isinstance(digest, str) or not _HASH.fullmatch(digest)
                or ref.get("path") != f"blobs/{digest}.json.gz" or ref.get("encoding") != "gzip"
                or ref.get("media_type") != "application/json"):
            raise TraceReadError("Invalid payload reference.")
        path = _safe_path(self._run_dir(run_id), "blobs", digest + ".json.gz")
        try:
            with gzip.open(path, "rb") as stream:
                body = stream.read(_MAX_BLOB + 1)
            if len(body) > _MAX_BLOB:
                raise TraceReadError("Payload exceeds the reader's 64 MiB limit; it was not truncated silently.")
            if hashlib.sha256(body).hexdigest() != digest:
                raise TraceReadError("Payload checksum does not match its reference.")
            return _clean(json.loads(body))
        except (OSError, ValueError, EOFError, zlib.error) as error:
            if isinstance(error, TraceReadError):
                raise
            raise TraceReadError("Referenced payload is missing or unreadable.") from None

    def journal(self, run_id, manifest=None):
        """No RunStore construction: no schema setup, migrations or controller locks."""
        _identifier(run_id)
        path = _safe_path(self.state_dir, run_id, "state.sqlite")
        warnings = []
        supplied = (manifest or {}).get("state_path")
        if supplied:
            candidate = Path(supplied).expanduser().absolute()
            if candidate.resolve().is_relative_to(self.state_dir.resolve()):
                path = candidate
            else:
                warnings.append("Manifest state_path is outside --state-dir; that path was not read.")
        if not path.exists():
            return None, warnings
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            version = connection.execute("SELECT version FROM supervisor_schema WHERE singleton=1").fetchone()
            if not version or version[0] != 1:
                raise ValueError
            row = connection.execute(
                "SELECT state_json,updated_at FROM supervisor_runs WHERE run_id=?", (run_id,),
            ).fetchone()
            if row is None:
                return None, warnings
            state = json.loads(row["state_json"]) if row["state_json"] else {}
            if not isinstance(state, dict):
                raise ValueError
            attempts = []
            for entry in connection.execute(
                "SELECT * FROM supervisor_attempts WHERE run_id=? ORDER BY created_at,id", (run_id,),
            ):
                item = dict(entry)
                item["record"] = json.loads(item.pop("record_json"))
                attempts.append(item)
            delegations = []
            for entry in connection.execute(
                "SELECT * FROM supervisor_delegations WHERE run_id=? ORDER BY updated_at,id", (run_id,),
            ):
                item = dict(entry)
                item["input"] = json.loads(item.pop("input_json"))
                raw = item.pop("result_json")
                item["result"] = json.loads(raw) if raw else None
                delegations.append(item)
            return _clean({"state": state, "attempts": attempts, "delegations": delegations,
                           "updated_at": row["updated_at"], "read_at": time.time()}), warnings
        except (OSError, sqlite3.Error, ValueError, TypeError):
            warnings.append("Historical SQLite state is unreadable or its schema is unsupported; no migration ran.")
            return None, warnings
        finally:
            if connection is not None:
                connection.close()

    def read(self, run_id, *, details=False):
        manifest, warnings = self.manifest(run_id)
        events, cursor, issues = self.all_events(run_id)
        warnings.extend(issues)
        recorded_sessions = {event["session_id"] for event in events}
        for session in manifest.get("sessions", []):
            if isinstance(session, dict) and session.get("session_id") not in recorded_sessions:
                warnings.append("A session listed in the manifest has no readable complete events.")
        journal, issues = self.journal(run_id, manifest)
        warnings.extend(issues)
        if not manifest and not events and journal is None:
            raise TraceReadError("Run was not found in the configured trace/state directories.")
        if not events:
            warnings.append("Historical coverage is partial: original model requests and responses were not recorded.")
        if details:
            for event in events:
                if "payload_ref" in event:
                    try:
                        event["payload"] = self.blob(run_id, event["payload_ref"])
                    except TraceReadError as error:
                        warnings.append(str(error))
        state = (journal or {}).get("state", {})
        worker = state.get("worker", {})
        if not isinstance(worker, dict):
            worker = {}
            warnings.append("Historical worker counters have an unsupported shape.")
        metrics = {"actions": sum(item["state"] != "rejected" for item in journal["attempts"])} if journal else {}
        mode = manifest.get("mode") or next((event.get("mode") for event in reversed(events)
                                            if event.get("event") in {"run.started", "run.resumed"}), None)
        # Without a trustworthy mode, a local worker result is never a global result.
        progress_name = "worker.progress" if mode == "direct" else "run.progress"
        latest_progress = next((event for event in reversed(events) if event.get("event") == progress_name), {})
        latest_status = next((event.get("status") for event in reversed(events) if event.get("status")
                              and event.get("event") in {progress_name, "session.closed"}),
                             "unknown")
        verification = state.get("verification", {})
        if journal is None and "payload_ref" in latest_progress:
            try:
                payload = latest_progress.get("payload")
                if payload is None:
                    payload = self.blob(run_id, latest_progress["payload_ref"])
                verification = payload.get("verification", {}) if isinstance(payload, dict) else {}
            except TraceReadError as error:
                warnings.append(str(error))
        verification_status = verification.get("status", "unknown") if isinstance(verification, dict) else "unknown"
        if journal is None:
            if isinstance(latest_progress.get("metrics"), dict):
                metrics.update(latest_progress["metrics"])
            if "actions" in latest_progress:
                metrics["actions"] = latest_progress["actions"]
        for name, source, key in (
            ("jev_calls", worker, "model_calls"), ("jev_http_attempts", worker, "model_http_attempts"),
            ("text_calls", worker, "text_model_calls"), ("supervisor_calls", state, "supervisor_calls"),
            ("supervisor_http_attempts", state, "supervisor_http_attempts"),
        ):
            if key in source:
                metrics[name] = source[key]
        opened = _open_calls(events)
        return {"run_id": run_id, "manifest": manifest, "journal": journal, "events": events, "cursor": cursor,
                "summary": {"status": state.get("status", latest_status),
                            "goal": state.get("goal", manifest.get("metadata", {}).get("goal")),
                            "phase": state.get("contract_phase", latest_progress.get("phase")),
                            "stop_reason": state.get("stop_reason", latest_progress.get("stop_reason")),
                            "verification_status": verification_status,
                            "metrics": metrics, "open_calls": len(opened),
                            "updated_at": (journal or {}).get("updated_at", manifest.get("updated_at"))},
                "open_calls": opened, "coverage": "partial" if warnings or not events else "recorded_best_effort",
                "warnings": list(dict.fromkeys(warnings)), "read_at": time.time()}

    def list_runs(self):
        ids = set()
        for root in (self.trace_dir, self.state_dir):
            if root.is_dir():
                ids.update(path.name for path in root.iterdir() if _ID.fullmatch(path.name) and path.is_dir())
        result = []
        for run_id in sorted(ids):
            try:
                snapshot = self.read(run_id)
                result.append({"run_id": run_id, **snapshot["summary"], "coverage": snapshot["coverage"],
                               "warnings": snapshot["warnings"]})
            except TraceReadError:
                continue
        return result

    def export(self, run_id, output):
        snapshot = self.read(run_id)
        destination = Path(output).expanduser().absolute()
        if destination.suffix.lower() != ".zip":
            raise TraceReadError("Export --output must name a .zip file.")
        for root in (self._run_dir(run_id), _safe_path(self.state_dir, run_id)):
            if destination.resolve().is_relative_to(root.resolve()):
                raise TraceReadError("Export must be outside the source run directories.")
        blobs, warnings = {}, list(snapshot["warnings"])
        for event in snapshot["events"]:
            for ref in _refs(event):
                try:
                    payload = self.blob(run_id, ref)
                    # Re-serialize sanitized content and update references for export integrity.
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
                    digest = hashlib.sha256(body).hexdigest()
                    ref.update(sha256=digest, path=f"blobs/{digest}.json.gz")
                    blobs[digest] = body
                except TraceReadError as error:
                    warnings.append(str(error))
        manifest = {"schema_version": 1, "run_id": run_id, "exported_at": time.time(),
                    "coverage": "partial" if warnings else snapshot["coverage"],
                    "warnings": list(dict.fromkeys(warnings)), "event_cursors": snapshot["cursor"],
                    "journal_updated_at": (snapshot["journal"] or {}).get("updated_at"),
                    "journal_read_at": (snapshot["journal"] or {}).get("read_at"),
                    "atomic_with_journal": False, "trace_manifest": snapshot["manifest"],
                    "sanitized": True, "event_records": "normalized_complete_lines", "payload_refs_rehashed": True}
        # Exclusive creation prevents overwriting earlier evidence or a symlink target.
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".agentrun-export-", dir=destination.parent)
            with os.fdopen(descriptor, "wb") as stream:
                with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("manifest.json", _json(manifest))
                    archive.writestr("journal.json", _json(snapshot["journal"]))
                    archive.writestr("events.jsonl", "".join(
                        json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n" for event in snapshot["events"]
                    ))
                    for digest, body in blobs.items():
                        archive.writestr(f"blobs/{digest}.json.gz", gzip.compress(body, mtime=0))
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic exclusive publication: a failed export never looks complete.
            os.link(temporary, destination)
        except OSError:
            raise TraceReadError("Export destination cannot be created or already exists.") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return manifest


def add_trace_commands(commands):
    traces = commands.add_parser("traces", help="Read local traces and historical state without running agents.")
    traces.add_argument("--trace-dir", default="artifacts/traces")
    traces.add_argument("--state-dir", default="artifacts/supervised")
    subcommands = traces.add_subparsers(dest="trace_command", required=True)
    for name in ("list", "show", "tail", "export"):
        parser = subcommands.add_parser(name)
        parser.add_argument("--trace-dir", default=argparse.SUPPRESS)
        parser.add_argument("--state-dir", default=argparse.SUPPRESS)
        if name != "list":
            parser.add_argument("run_id")
        if name in {"show", "tail"}:
            parser.add_argument("--component")
            parser.add_argument("--delegation", help="Filter by delegation_id.")
            parser.add_argument("--errors", action="store_true")
            parser.add_argument("--event", help="Filter by event_id or event type.")
            parser.add_argument("--details", action="store_true", help="Show full locally recorded payloads.")
        if name in {"show", "list", "tail"}:
            parser.add_argument("--json", action="store_true", help="Print JSON (JSONL for tail).")
        if name == "tail":
            parser.add_argument("--follow", action="store_true")
            parser.add_argument("--cursor", help="Resume from a JSON object mapping session IDs to byte offsets.")
        if name == "export":
            parser.add_argument("--output", required=True)


def _matches(event, args):
    return (not args.component or event.get("component") == args.component) and (
        not args.delegation or event.get("delegation_id") == args.delegation
    ) and (not args.errors or _is_error(event)) and (
        not args.event or args.event in {event.get("event"), event.get("event_id")}
    )


def _event_line(event):
    names = {"jev": "Jev", "supervisor": "Supervisor", "text": "Auxiliar de texto",
             "verification": "Verificación", "browser": "Navegador", "worker": "Ejecución"}
    component = names.get(event.get("component"), event.get("component"))
    fields = [component]
    semantic = event.get("semantic_summary") or event.get("semantic_intent")
    if isinstance(semantic, str) and semantic:
        fields.append(_display(semantic))
    else:
        readable = {
            "call.started": "inicia una consulta al modelo",
            "call.completed": "termina la consulta al modelo",
            "call.failed": "no pudo completar la consulta al modelo",
            "browser.started": "va a ejecutar una acción en el navegador",
            "browser.completed": "terminó una acción en el navegador",
            "action_executed": "ejecutó la acción elegida",
            "action_discarded": "retiró una acción que ya no era válida",
            "model_choice": "eligió una operación",
            "run_stopped": "detuvo la ejecución",
        }
        fields.append(readable.get(event.get("event"), _display(event.get("event", "evento"))))
    for key in ("phase", "duration_ms", "status"):
        if event.get(key) is not None:
            fields.append(f"{key}={event[key]}")
    return "  ".join(_display(value) for value in fields if value is not None)


def _warn(warnings):
    for warning in warnings:
        print("Trace warning: " + _display(warning), file=sys.stderr)


def handle_trace_command(args):
    reader = TraceReader(args.trace_dir, args.state_dir)
    cursor = {}
    try:
        if args.trace_command == "list":
            runs = reader.list_runs()
            if args.json:
                print(_json(runs))
            else:
                for run in runs:
                    print(f"{run['run_id']}  {_display(run['status'])}"
                          f"  verification={_display(run['verification_status'])}  {run['coverage']}"
                          f"  open_calls={run['open_calls']}  {_display(run['goal'] or '')[:100]}")
            return 0
        if args.trace_command == "export":
            manifest = reader.export(args.run_id, args.output)
            _warn(manifest["warnings"])
            print(f"Exported {_display(args.run_id)} to {_display(args.output)} ({manifest['coverage']}).")
            return 0
        if args.trace_command == "show":
            snapshot = reader.read(args.run_id, details=args.details)
            snapshot["events"] = [event for event in snapshot["events"] if _matches(event, args)]
            _warn(snapshot["warnings"])
            if args.json:
                print(_json(snapshot))
            else:
                summary = snapshot["summary"]
                print(f"Ejecución: {_display(summary['status'])} · cobertura {snapshot['coverage']}")
                print(f"Phase: {_display(summary['phase'])} · stop: {_display(summary['stop_reason'])}")
                print(f"Recorded verification: {_display(summary['verification_status'])}"
                      " · independent outcome verification is not inferred.")
                print(f"Known counters: {_json(summary['metrics'])}")
                print(f"Calls without a recorded result: {summary['open_calls']}")
                for event in snapshot["events"]:
                    print(_event_line(event))
                    if args.details:
                        print(_json(event.get("payload", event)))
                if args.details and snapshot["journal"]:
                    print(_json(snapshot["journal"]))
            return 0
        cursor = json.loads(args.cursor) if args.cursor else {}
        if not isinstance(cursor, dict):
            raise TraceReadError("Invalid trace cursor.")
        initial = reader.read(args.run_id)  # Also recognizes historical-only runs.
        _warn(initial["warnings"])
        warned = set(initial["warnings"])
        while True:
            events, cursor, warnings = reader.events(args.run_id, cursor=cursor)
            for event in events:
                if _matches(event, args):
                    if args.details and "payload_ref" in event:
                        try:
                            event["payload"] = reader.blob(args.run_id, event["payload_ref"])
                        except TraceReadError as error:
                            warnings.append(str(error))
                    print(json.dumps(event, ensure_ascii=True) if args.json else _event_line(event), flush=True)
                    if args.details and not args.json:
                        print(_json(event.get("payload", event)), flush=True)
            _warn([warning for warning in warnings if warning not in warned])
            warned.update(warnings)
            if not args.follow and not events:
                print("Cursor: " + json.dumps(cursor, sort_keys=True), file=sys.stderr)
                return 0
            if not events:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("Cursor: " + json.dumps(cursor, sort_keys=True), file=sys.stderr)
        return 130
    except (TraceReadError, OSError, ValueError):
        print("Trace read failed: check the run ID, configured directories, cursor and output path.", file=sys.stderr)
        return 2
