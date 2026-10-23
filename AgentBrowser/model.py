"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from .candidates import CandidateTraversal, SerializedBody, serialize_json
from .evidence import bounded_context, execution_history
from .observability import current_trace
from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)
OPENROUTER_HOSTS = {"openrouter.ai", "eu.openrouter.ai", "us.openrouter.ai"}
_LIMIT_SOURCES = {
    402: {"openrouter_credits", "openrouter_key_limit", "openrouter_in_flight_budget"},
    403: {"openrouter_guardrail_budget", "openrouter_workspace_budget"},
}
_LIMIT_REASONS = {"in_flight_budget_exhausted", "weight_exceeds_budget"}
MAX_POLICY_EFFECTS = 6
MAX_POLICY_EFFECT_TEXT = 512
_DIAGNOSTIC_COMPONENTS = {"jev", "text", "verification", "supervisor"}
_DIAGNOSTIC_CODES = {
    "invalid_request_error", "invalid_request", "invalid_parameter", "invalid_value",
    "missing_required_parameter", "unsupported_parameter", "context_length_exceeded",
    "model_not_found", "insufficient_quota", "rate_limit_exceeded", "invalid_api_key", "invalid_json",
    "output_token_limit",
}
_DIAGNOSTIC_FIELDS = {
    "model", "messages", "questions", "state", "max_tokens", "temperature", "response_format",
    "tools", "tool_choice", "prompt",
}


def provider_diagnostic(diagnostic):
    """Allow only bounded request identifiers, sizes and enumerated error labels."""
    if not isinstance(diagnostic, dict):
        return {}
    safe = {}
    for name, allowed in (("component", _DIAGNOSTIC_COMPONENTS), ("error_code", _DIAGNOSTIC_CODES),
                          ("field", _DIAGNOSTIC_FIELDS)):
        value = diagnostic.get(name)
        if isinstance(value, str) and value in allowed:
            safe[name] = value
    for name in ("request_bytes", "candidate_count", "head_count"):
        value = diagnostic.get(name)
        if type(value) is int and 0 <= value <= 2**31 - 1:
            safe[name] = value
    model = diagnostic.get("model")
    if (isinstance(model, str) and len(model) <= 128
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._:-]*)?", model)):
        safe["model"] = model
    digest = diagnostic.get("request_sha256")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        safe["request_sha256"] = digest
    request_id = diagnostic.get("provider_request_id")
    if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", request_id):
        safe["provider_request_id"] = request_id
    return safe


def _request_diagnostic(content, diagnostic=None):
    safe = provider_diagnostic(diagnostic)
    try:
        body = json.loads(content)
    except (ValueError, TypeError, UnicodeError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("model"), str):
        questions = body.get("questions")
        if isinstance(questions, dict):
            safe.update(component="jev", head_count=len(questions))
            safe["candidate_count"] = sum(
                len(question["criteria"]) for name, question in questions.items()
                if name != "operation" and isinstance(question, dict) and isinstance(question.get("criteria"), dict)
            )
        elif isinstance(body.get("messages"), list):
            response_format = body.get("response_format")
            schema = response_format.get("json_schema") if isinstance(response_format, dict) else None
            safe.setdefault("component", "verification" if isinstance(schema, dict)
                            and schema.get("name") == "browser_outcome_review" else "text")
        safe.update(provider_diagnostic({"model": body["model"]}))
    if safe:
        safe.update(request_bytes=len(content), request_sha256=hashlib.sha256(content).hexdigest())
    return provider_diagnostic(safe)


def _response_diagnostic(response):
    diagnostic = {"provider_request_id": response.headers.get("x-request-id") or response.headers.get("request-id")}
    try:
        body = response.json()
    except (ValueError, TypeError, UnicodeError):
        return provider_diagnostic(diagnostic)
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        diagnostic.update(error_code=error.get("code"), field=error.get("param", error.get("field")))
    return provider_diagnostic(diagnostic)


def provider_limit_metadata(status_code, limit_source=None, limit_reason=None):
    """Copy only documented limit categories, never provider-authored text."""
    safe = {}
    if (type(status_code) is int and isinstance(limit_source, str)
            and limit_source in _LIMIT_SOURCES.get(status_code, set())):
        safe["provider_limit_source"] = limit_source
        if (isinstance(limit_reason, str) and (
            limit_source == "openrouter_in_flight_budget" and limit_reason in _LIMIT_REASONS
            or limit_source == "openrouter_credits" and limit_reason == "weight_exceeds_budget"
        )):
            safe["provider_limit_reason"] = limit_reason
    return safe


def provider_retry_after(value, *, now=None):
    """Parse only Retry-After seconds/dates; never retain provider-authored text."""
    ceiling = 2**31 - 1
    if type(value) is int:
        return min(ceiling, value) if value >= 0 else None
    if type(value) is float:
        if math.isfinite(value) and value >= 0:
            return min(ceiling, math.ceil(value))
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return ceiling if len(value) > 10 else min(ceiling, int(value))
    if len(value) > 128:
        return None
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return min(ceiling, max(0, math.ceil(date.timestamp() - (time.time() if now is None else now))))
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def provider_retry_after_headers(*headers, now=None):
    """Honor the longest valid server delay if wrappers supply both sources."""
    values = [provider_retry_after(value, now=now) for source in headers if isinstance(source, Mapping)
              for name, value in source.items() if isinstance(name, str) and name.lower() == "retry-after"]
    return max((value for value in values if value is not None), default=None)


class ProviderError(RuntimeError):
    """Safe transport diagnostics without response bodies, URLs or credentials."""

    def __init__(self, kind, *, status_code=None, limit_source=None, limit_reason=None, diagnostic=None,
                 retry_after_seconds=None):
        self.kind = kind if isinstance(kind, str) and kind in {
            "authentication", "billing", "rate_limit", "unavailable", "timeout",
            "connection", "request", "invalid_response",
        } else "unknown"
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        self.metadata = {"provider_error_kind": self.kind}
        if self.status_code is not None:
            self.metadata["provider_http_status"] = self.status_code
        self.metadata.update(provider_limit_metadata(self.status_code, limit_source, limit_reason))
        self.retry_after_seconds = (provider_retry_after(retry_after_seconds)
                                    if type(retry_after_seconds) in (int, float) else None)
        if self.retry_after_seconds is not None:
            self.metadata["provider_retry_after_seconds"] = self.retry_after_seconds
        safe_diagnostic = provider_diagnostic(diagnostic)
        if safe_diagnostic:
            self.metadata["provider_diagnostic"] = safe_diagnostic
        message = (f"Model provider returned HTTP {self.status_code}" if self.status_code is not None
                   else f"Model provider failure ({self.kind})")
        super().__init__(message + "; no action executed.")


def _response_limits(url, response):
    if response.status_code not in _LIMIT_SOURCES:
        return {}
    try:
        hostname = urlparse(url).hostname
    except (ValueError, TypeError):
        return {}
    if hostname not in OPENROUTER_HOSTS:
        return {}
    try:
        body = response.json()
    except (ValueError, TypeError, UnicodeError):
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    metadata = error.get("metadata") if isinstance(error, dict) else None
    if not isinstance(metadata, dict):
        return {}
    return provider_limit_metadata(response.status_code, metadata.get("limit_source"), metadata.get("reason"))


def _wire_payload(content):
    """Trace the actual wire data without hiding credentials inside binary encodings."""
    try:
        return json.loads(content)
    except (ValueError, TypeError, UnicodeError):
        try:
            return {"raw_utf8": content.decode("utf-8"), "json_valid": False}
        except (AttributeError, UnicodeError):
            return {"decoding_failure": "invalid_utf8", "raw_omitted": True}


def post_json(url, key, body, *, diagnostic=None):
    # OpenRouter returns the provider's exact per-generation cost only when
    # usage reporting is requested. Other providers receive the original body.
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        hostname = None
    if hostname in OPENROUTER_HOSTS:
        if isinstance(body, SerializedBody):
            body.setdefault("usage", {"include": True})
            body.content = serialize_json(body)
        elif isinstance(body, dict):
            body = {**body, "usage": body.get("usage", {"include": True})}
    content = body.content if isinstance(body, SerializedBody) else serialize_json(body)
    request_diagnostic = _request_diagnostic(content, diagnostic)
    trace = current_trace()
    if trace is not None:
        trace.register_secret(key)
    component = request_diagnostic.get("component", "provider")
    for attempt in range(3):
        if isinstance(body, SerializedBody):
            body.http_attempts += 1
        started = time.perf_counter()
        if trace is not None:
            trace.emit(
                "http.request", component=component, payload=_wire_payload(content), attempt=attempt + 1,
                method="POST", request_bytes=len(content), request_sha256=hashlib.sha256(content).hexdigest(),
            )
        try:
            response = CLIENT.post(url, content=content, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            })
        except httpx.TimeoutException:
            if trace is not None:
                trace.emit("http.error", component=component, attempt=attempt + 1, error_kind="timeout",
                           latency_ms=round((time.perf_counter() - started) * 1000))
            raise ProviderError("timeout", diagnostic=request_diagnostic) from None
        except httpx.HTTPError:
            if trace is not None:
                trace.emit("http.error", component=component, attempt=attempt + 1, error_kind="connection",
                           latency_ms=round((time.perf_counter() - started) * 1000))
            raise ProviderError("connection", diagnostic=request_diagnostic) from None
        except BaseException as error:
            if trace is not None:
                trace.emit("http.error", component=component, attempt=attempt + 1,
                           error_kind="unexpected" if isinstance(error, Exception) else "interrupted",
                           error_type=type(error).__name__[:80],
                           latency_ms=round((time.perf_counter() - started) * 1000))
            raise
        retrying = response.status_code in {429, 529, 503} and attempt < 2
        if trace is not None:
            response_content = response.content
            trace.emit(
                "http.response", component=component, payload=_wire_payload(response_content), attempt=attempt + 1,
                status_code=response.status_code, response_bytes=len(response_content),
                response_sha256=hashlib.sha256(response_content).hexdigest(), retrying=retrying,
                latency_ms=round((time.perf_counter() - started) * 1000),
            )
        if retrying:
            if trace is not None:
                trace.emit("http.retry", component=component, attempt=attempt + 1,
                           status_code=response.status_code, delay_seconds=0.5 * 2**attempt)
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            kind = {401: "authentication", 403: "authentication", 402: "billing", 429: "rate_limit"}.get(
                response.status_code, "unavailable" if response.status_code >= 500 else "request",
            )
            limits = _response_limits(url, response)
            if limits:
                kind = "billing"
            if trace is not None:
                trace.emit("http.error", component=component, attempt=attempt + 1, error_kind=kind,
                           status_code=response.status_code, latency_ms=round((time.perf_counter() - started) * 1000))
            raise ProviderError(
                kind, status_code=response.status_code,
                limit_source=limits.get("provider_limit_source"), limit_reason=limits.get("provider_limit_reason"),
                diagnostic={**request_diagnostic, **_response_diagnostic(response)},
                retry_after_seconds=provider_retry_after_headers(response.headers),
            )
        try:
            return response.json()
        except ValueError:
            if trace is not None:
                trace.emit("http.error", component=component, attempt=attempt + 1, error_kind="invalid_response",
                           status_code=response.status_code, latency_ms=round((time.perf_counter() - started) * 1000))
            raise ProviderError("invalid_response", status_code=response.status_code,
                                diagnostic={**request_diagnostic, **_response_diagnostic(response)}) from None
    raise ProviderError("unavailable", diagnostic=request_diagnostic)


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        scoped_scroll = kind == "scroll" and action.get("node") is not None
        if kind not in operations and not scoped_scroll:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {
                k: action[k] for k in ("role", "value", "checked", "selected", "expanded", "pressed",
                                       "required", "invalid", "validation_message", "href", "href_truncated")
                if k in action
            }
            if action.get("context"):
                element["context"] = bounded_context(action["context"])
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        if scoped_scroll:
            direction = action.get("direction", "down" if action.get("delta", 0) > 0 else "up")
            if direction not in {"up", "down"}:
                raise ValueError("Unsupported observed scroll direction")
            operation = "SCROLL_" + direction.upper()
        else:
            operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def _recent_actions(history):
    """Bound policy context without changing the full execution/evidence journal."""
    recent = history[-10:]
    compact = execution_history(recent)
    for record, entry in zip(compact, recent, strict=True):
        if "page_changed" in entry:
            record["page_changed"] = entry["page_changed"]
        if "effects" not in entry:
            continue
        effects = [effect for effect in entry["effects"] if effect.get("kind") != "effects_truncated"]
        omitted = entry.get("effects_omitted", 0) + sum(
            effect.get("omitted", 0) for effect in entry["effects"] if effect.get("kind") == "effects_truncated"
        )

        def priority(index):
            effect = effects[index]
            kind = effect.get("kind")
            acted = (effect.get("node") == entry["node"] if entry.get("node") is not None
                     else bool(entry.get("action")) and effect.get("label") == entry["action"])
            if kind == "navigated":
                return 0
            if acted:
                return 1
            if kind in {"value", "current_value", "selected", "checked", "pressed", "invalid", "validation_message"}:
                return 2
            return 3 if kind in {"visible_text_changed", "label"} else 4

        selected = sorted(sorted(range(len(effects)), key=priority)[:MAX_POLICY_EFFECTS])
        record["effects"] = []
        for index in selected:
            truncated = []

            def bound(value, path):
                if isinstance(value, str) and path.rsplit(".", 1)[-1] != "ref":
                    if len(value) > MAX_POLICY_EFFECT_TEXT:
                        truncated.append(path)
                    return value[:MAX_POLICY_EFFECT_TEXT]
                if isinstance(value, dict):
                    return {key: bound(item, f"{path}.{key}" if path else key) for key, item in value.items()}
                if isinstance(value, list):
                    return [bound(item, f"{path}.{number}") for number, item in enumerate(value)]
                return value

            brief = bound(effects[index], "")
            if truncated:
                brief["truncated_fields"] = list(dict.fromkeys([*brief.get("truncated_fields", []), *truncated]))
            record["effects"].append(brief)
        omitted += len(effects) - len(selected)
        if omitted:
            record["effects_omitted"] = omitted
    if len(history) > len(recent) and compact:
        compact[0]["earlier_executions_omitted"] = len(history) - len(recent)
    return compact


def _request(state, actions, coverage, goal, history):
    elements, targets, controls = action_space(actions)
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
        "SCROLL_UP": "Scroll an observed page or panel upward to reveal earlier controls.",
        "SCROLL_DOWN": "Scroll an observed page or panel downward to reveal later controls.",
        "RELOAD": "Refresh the current page when it is stuck and no supported action can advance.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    if coverage["more_available"]:
        operations["MORE_CANDIDATES"] = "Inspect the next window of observed candidates; this does not change the page."
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded", "pressed",
                                         "href", "href_truncated") if k in a},
                    **({"context": bounded_context(a["context"])} if a.get("context") else {}),
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    return {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": _page_context(state),
            "elements": elements,
            "candidate_coverage": coverage,
            "recent_actions": _recent_actions(history),
            **({"progress_feedback": state["progress_feedback"]} if "progress_feedback" in state else {}),
        },
        "questions": questions,
    }


def _page_context(state):
    page = {key: state[key] for key in ("url", "title", "text")}
    if "readiness" in state:
        page["readiness"] = state["readiness"]
    # Contexts for offered elements are attached to those elements, rather than
    # duplicating every region (including ones omitted from this window).
    return page


def prepare_candidates(state, goal, history, *, traversal=None):
    """Prepare an explicit window without inference, cursor advancement or browser input."""
    traversal = traversal if traversal is not None else CandidateTraversal(state)
    window = traversal.prepare(state, lambda page, actions, coverage: _request(page, actions, coverage, goal, history))
    return {
        **state, "actions": window.actions, "candidate_coverage": window.coverage,
        "selection_digest": window.digest, "request_bytes": window.request_bytes,
        "_candidate_window": window, "_candidate_request": window.body,
        "_candidate_context": _request_context({**state, "actions": window.actions}, goal, history),
    }


def _request_context(state, goal, history):
    return hashlib.sha256(serialize_json({
        "page": _page_context(state),
        "goal": goal, "history": history[-10:],
        "progress_feedback": state.get("progress_feedback"),
        "actions": [{key: value for key, value in action.items() if key != "rect"}
                    for action in state.get("actions", [])],
    })).hexdigest()


def choose(state, goal, history):
    if "_candidate_request" not in state:
        state = prepare_candidates(state, goal, history)
    elif state.get("_candidate_context") != _request_context(state, goal, history):
        raise ValueError("Prepared candidate request no longer matches goal, page or history")
    body = state["_candidate_request"]
    _, targets, controls = action_space(state["actions"])
    operations = body["questions"]["operation"]["criteria"]
    started = time.perf_counter()
    previous_attempts = body.http_attempts
    result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request_bytes": len(body.content),
        "http_attempts": body.http_attempts - previous_attempts,
        "selection_digest": state["selection_digest"],
        "candidate_coverage": state["candidate_coverage"],
        "request": body,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")}
                 | ({"context": bounded_context(action["context"])} if action.get("context") else {}),
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h[k] for k in ("action", "text", "effects", "context") if k in h}
                           for h in history[-6:]],
        **({"progress_feedback": page["progress_feedback"]} if "progress_feedback" in page else {}),
    }


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    body = SerializedBody({
        "model": model,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
        **reasoning,
        "messages": [
            {"role": "system", "content": TEXT_VALUE},
            {"role": "user", "content": json.dumps(context)},
        ],
    })
    result = post_json(base + "/chat/completions", key, body)
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
        "http_attempts": body.http_attempts,
        "request_bytes": len(body.content),
    }
