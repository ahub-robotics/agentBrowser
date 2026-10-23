"""Local, best-effort traces that never control agent execution or retries."""

import gzip
import hashlib
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter as _trace_clock
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

SCHEMA_VERSION = 1
_REDACTED = "[REDACTED]"
_CURRENT = ContextVar("agentrun_trace", default=None)
_CORRELATIONS = ContextVar("agentrun_trace_correlations", default=None)
_LOGGER = logging.getLogger("agentrun")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
_SENSITIVE = re.compile(r"(?:api.?key|secret|password|passwd|private.?key|credential)", re.I)
_OMITTED_KEYS = {"env", "environment", "environ", "client", "httpclient", "browser", "callbacks", "connection"}
_ROOT_FIELDS = {"schema_version", "run_id", "session_id", "seq", "event_id", "timestamp", "component", "event",
                "payload_ref", "redacted"}


@dataclass(frozen=True)
class TraceConfig:
    enabled: bool = True
    directory: str | Path = "artifacts/traces"


def _sensitive_key(key):
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return (bool(_SENSITIVE.search(key)) or compact in {
        "key", "token", "accesstoken", "refreshtoken", "idtoken", "authorization", "proxyauthorization",
        "cookie", "cookies", "setcookie", "accesskey", "accesskeyid",
    } or key.lower().endswith("_token"))


def _environment_secrets():
    return {value for key, value in os.environ.items()
            if value and (_sensitive_key(key) or key.lower().endswith("_key"))}


def redact(value, *, secrets=()):
    """Return (JSON-compatible data, changed), without truncating nonsensitive data.

    Credentials are removed before hashing or writing. Unsupported objects are
    represented by their type, never repr/str, which can expose client secrets.
    """
    values = set(_environment_secrets())
    values.update(secret for secret in secrets if isinstance(secret, str) and secret)
    variants = {variant for secret in values for variant in (secret, quote(secret, safe=""), quote_plus(secret))}
    variants = sorted(variants, key=len, reverse=True)
    changed = False
    seen = set()

    def scrub_url(match):
        nonlocal changed
        original = match.group()
        try:
            parts = urlsplit(original)
            netloc = parts.netloc.rsplit("@", 1)[-1]
            local_change = netloc != parts.netloc
            query = []
            query_changed = False
            for key, val in parse_qsl(parts.query, keep_blank_values=True):
                if _sensitive_key(key):
                    val, query_changed = _REDACTED, True
                query.append((key, val))
            fragment = parts.fragment
            if "=" in fragment:
                pairs = parse_qsl(fragment, keep_blank_values=True)
                if any(_sensitive_key(key) for key, _ in pairs):
                    fragment = urlencode([(key, _REDACTED if _sensitive_key(key) else val) for key, val in pairs])
                    local_change = True
            if local_change or query_changed:
                changed = True
                return urlunsplit((parts.scheme, netloc, parts.path,
                                   urlencode(query) if query_changed else parts.query, fragment))
        except (ValueError, UnicodeError):
            # Userinfo is still removed when a malformed host cannot be parsed.
            cleaned = re.sub(r"(?<=://)[^/\s]*@", "", original)
            cleaned = re.sub(r"([?&#])([^=&?#]+)=([^&#]*)", lambda pair: (
                pair[1] + pair[2] + "=" + quote(_REDACTED) if _sensitive_key(unquote(pair[2])) else pair.group()
            ), cleaned)
            changed |= cleaned != original
            return cleaned
        return original

    def text(value):
        nonlocal changed
        # Parse credential-bearing URLs before replacement markers can make
        # their authority syntactically invalid (for example userinfo brackets).
        value = _URL.sub(scrub_url, value)
        for secret in variants:
            if secret in value:
                value = value.replace(secret, _REDACTED)
                changed = True
        return value

    def walk(item):
        nonlocal changed
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if math.isfinite(item):
                return item
            changed = True
            return {"_trace_omitted": "nonfinite_number"}
        if isinstance(item, str):
            return text(item)
        if isinstance(item, Path):
            return text(str(item))
        if isinstance(item, bytes):
            return text(item.decode("utf-8", errors="replace"))
        if isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity in seen:
                changed = True
                return {"_trace_omitted": "cycle"}
            seen.add(identity)
            try:
                if isinstance(item, dict):
                    result = {}
                    for key, val in item.items():
                        if not isinstance(key, str):
                            changed = True
                            continue
                        safe_key = text(key)
                        compact = re.sub(r"[^a-z0-9]", "", key.lower())
                        if compact in _OMITTED_KEYS or _sensitive_key(key):
                            result[safe_key] = _REDACTED
                            changed = True
                        else:
                            result[safe_key] = walk(val)
                    return result
                return [walk(element) for element in item]
            finally:
                seen.remove(identity)
        changed = True
        return {"_trace_omitted": "unsupported_type", "type": text(type(item).__name__)}

    result = walk(value)
    return result, changed


def current_trace():
    """Return the context bound in this execution scope, never a global run."""
    return _CURRENT.get()


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def _atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".trace-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class TraceContext:
    def __init__(self, config=True, *, run_id=None, mode="direct", resumed=False, state_path=None,
                 metadata=None, secrets=()):
        self.config = config if isinstance(config, TraceConfig) else TraceConfig(enabled=bool(config))
        self.run_id = run_id or uuid4().hex
        self.session_id = uuid4().hex
        self.mode = mode
        self.directory = Path("artifacts/traces")
        self.enabled = bool(self.config.enabled)
        self.degraded = False
        self._warned = False
        self._closed = False
        self._seq = 0
        self._lock = threading.RLock()
        self._secrets = set()
        self._manifest = None
        try:
            for secret in (secrets,) if isinstance(secrets, str) else (secrets or ()):
                self.register_secret(secret)
        except Exception as error:
            self._degrade(error)
        try:
            self.directory = Path(self.config.directory) / self.run_id
        except Exception as error:
            self._degrade(error)
            return
        if not self.enabled or self.degraded:
            return
        try:
            if not isinstance(self.run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.run_id):
                raise ValueError("Invalid trace run identifier")
            for directory in (Path(self.config.directory), self.directory,
                              self.directory / "events", self.directory / "blobs"):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
            self._event_path = self.directory / "events" / f"{self.session_id}.jsonl"
            self._manifest_path = self.directory / "manifest.json"
            now = _timestamp()
            previous = json.loads(self._manifest_path.read_text()) if self._manifest_path.exists() else {}
            self._manifest = {
                **previous, "schema_version": SCHEMA_VERSION, "run_id": self.run_id, "mode": self.mode,
                "created_at": previous.get("created_at", now), "updated_at": now,
                "state_path": str(state_path) if state_path is not None else previous.get("state_path"),
                "metadata": {**previous.get("metadata", {}), **(metadata or {})},
                "sessions": [*previous.get("sessions", []), {
                    "session_id": self.session_id, "started_at": now, "resumed": bool(resumed),
                }],
            }
            self._write_manifest()
            self.emit("run.resumed" if resumed else "run.started", mode=mode, resumed=bool(resumed))
        except Exception as error:
            self._degrade(error)

    def _degrade(self, error):
        self.degraded = True
        if not self._warned:
            self._warned = True
            try:
                sys.stderr.write(f"AgentRun trace degraded ({type(error).__name__[:80]}); execution continues.\n")
            except Exception:
                pass

    def info(self):
        return {"enabled": self.enabled, "run_id": self.run_id, "session_id": self.session_id,
                "directory": str(self.directory), "degraded": self.degraded}

    def register_secret(self, value):
        try:
            if isinstance(value, str) and value:
                with self._lock:
                    self._secrets.add(value)
        except Exception as error:
            self._degrade(error)

    def redact(self, value):
        """Sanitize console/export data with this run's credentials, without throwing."""
        try:
            with self._lock:
                return redact(value, secrets=self._secrets)
        except Exception as error:
            self._degrade(error)
            return {"_trace_omitted": "redaction_failed"}, True

    def _write_manifest(self):
        safe, changed = redact(self._manifest, secrets=self._secrets)
        safe["redacted"] = changed or safe.get("redacted", False)
        _atomic_write(self._manifest_path, _json_bytes(safe))

    def emit(self, event, *, component="agent", payload=None, **fields):
        if not self.enabled or self._closed or self.degraded:
            return None
        try:
            with self._lock:
                inherited = (_CORRELATIONS.get() or {}) if current_trace() is self else {}
                details, changed = redact({**inherited, **fields}, secrets=self._secrets)
                details = {key: val for key, val in details.items() if key not in _ROOT_FIELDS}
                payload_ref = None
                if payload is not None:
                    safe, payload_changed = redact(payload, secrets=self._secrets)
                    content = _json_bytes(safe)
                    digest = hashlib.sha256(content).hexdigest()
                    path = self.directory / "blobs" / f"{digest}.json.gz"
                    if not path.exists():
                        _atomic_write(path, gzip.compress(content, mtime=0))
                    payload_ref = {"sha256": digest, "path": f"blobs/{digest}.json.gz", "encoding": "gzip",
                                   "media_type": "application/json", "redacted": payload_changed}
                    changed |= payload_changed
                self._seq += 1
                event_id = f"{self.session_id}:{self._seq}"
                names, names_changed = redact({"name": event, "part": component}, secrets=self._secrets)
                record = {
                    **details, "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
                    "session_id": self.session_id, "seq": self._seq, "event_id": event_id,
                    "timestamp": _timestamp(), "component": names["part"], "event": names["name"],
                    "redacted": changed or names_changed,
                }
                if payload_ref is not None:
                    record["payload_ref"] = payload_ref
                descriptor = os.open(self._event_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "ab") as output:
                    output.write(_json_bytes(record) + b"\n")
                    output.flush()
                    os.fsync(output.fileno())
                return event_id
        except Exception as error:
            self._degrade(error)
            return None

    @contextmanager
    def bind(self, **correlations):
        inherited = (_CORRELATIONS.get() or {}) if current_trace() is self else {}
        owner_token = _CURRENT.set(self)
        fields_token = _CORRELATIONS.set({**inherited, **correlations})
        try:
            yield self
        finally:
            _CORRELATIONS.reset(fields_token)
            _CURRENT.reset(owner_token)

    @contextmanager
    def call(self, component, operation, *, payload=None, **fields):
        fields = {key: val for key, val in fields.items() if key not in {"event", "component", "payload"}}
        call = _TraceCall(self, component, operation, fields=fields)
        started = _trace_clock()
        parent = (_CORRELATIONS.get() or {}).get("call_id") if current_trace() is self else None
        with self.bind(**{**fields, "call_id": call.call_id}):
            self.emit("call.started", component=component, payload=payload,
                      **{**fields, "operation": operation, "parent_call_id": parent})
            self._call_summary(call, "started")
            try:
                yield call
            except BaseException as error:
                duration = round((_trace_clock() - started) * 1000, 3)
                outcome = "failed" if isinstance(error, Exception) else "interrupted"
                stack = [{"file": frame.f_code.co_filename, "line": line, "function": frame.f_code.co_name}
                         for frame, line in traceback.walk_tb(error.__traceback__)]
                self.emit(f"call.{outcome}", component=component, **{
                    **fields, "operation": operation, "duration_ms": duration,
                    "error_type": type(error).__name__, "stack": stack,
                })
                self._call_summary(call, outcome, duration, type(error).__name__)
                raise
            else:
                duration = round((_trace_clock() - started) * 1000, 3)
                self.emit("call.completed", component=component, payload=call.value,
                          **{**fields, **call.fields, "operation": operation, "duration_ms": duration})
                self._call_summary(call, "completed", duration)

    def _call_summary(self, call, outcome, duration=None, error_type=None):
        if not self.enabled:
            return
        try:
            agents = {"jev": "jev", "supervisor": "supervisor", "text": "text",
                      "verification": "verification", "browser": "browser"}
            agent = agents.get(call.component, "agent")
            intent = call.fields.get("semantic_intent")
            semantic = call.fields.get("semantic_summary")
            if outcome == "started" and isinstance(intent, str) and intent:
                safe, _ = redact(intent, secrets=self._secrets)
                _LOGGER.info("%s", safe, extra={"agentrun_agent": agent})
                return
            if outcome == "completed" and isinstance(semantic, str) and semantic:
                if call.component == "jev":
                    # The validated decision is rendered by log_event with its observed target.
                    return
                safe, _ = redact(semantic, secrets=self._secrets)
                _LOGGER.info("%s", safe, extra={"agentrun_agent": agent})
                return
            if outcome in {"failed", "interrupted"}:
                names = {"jev": "Jev", "supervisor": "Supervisor", "text": "Auxiliar de texto",
                         "verification": "Verificación", "browser": "Navegador"}
                safe, _ = redact(error_type or "fallo inesperado", secrets=self._secrets)
                _LOGGER.error("%s no pudo completar %s (%s).", names.get(call.component, "AgentRun"),
                              call.fields.get("semantic_intent") or call.operation, safe,
                              extra={"agentrun_agent": agent})
        except Exception as error:
            self._degrade(error)

    def close(self, status=None, **fields):
        with self._lock:
            if self._closed:
                return
            self.emit("session.closed", status=status,
                      **{key: val for key, val in fields.items() if key not in {"event", "component", "payload"}})
            self._closed = True
            if not self.enabled or self.degraded or self._manifest is None:
                return
            try:
                now = _timestamp()
                for session in self._manifest["sessions"]:
                    if session["session_id"] == self.session_id:
                        session.update(ended_at=now, status=status)
                self._manifest["updated_at"] = now
                self._write_manifest()
            except Exception as error:
                self._degrade(error)


class _TraceCall:
    def __init__(self, trace, component, operation, *, fields=None):
        self.trace = trace
        self.component = component
        self.operation = operation
        self.call_id = uuid4().hex
        self.fields = dict(fields or {})
        self.value = None

    def result(self, value, **fields):
        self.value = value
        self.fields.update({key: val for key, val in fields.items() if key not in {"event", "component", "payload"}})


NULL_TRACE = TraceContext(False)
