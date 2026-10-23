"""Coordinator acceptance: isolated local submissions or guarded read-only coches.net visits.

Models are simulated by default through the production supervisor tool schemas.
--live-models explicitly enables configured paid providers. Never imported by pytest to run live work.
"""

import argparse
import json
import re
import sqlite3
import sys
import threading
from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import Field  # noqa: E402

from agentrun import SupervisedAgent, SupervisorLimits  # noqa: E402
from agentrun.config import load_environment  # noqa: E402
from agentrun.supervisor_policy import SupervisorPolicy  # noqa: E402
from AgentBrowser import agent as worker_module  # noqa: E402
from AgentBrowser import model  # noqa: E402
from AgentBrowser.browser import Browser  # noqa: E402
from scripts.evaluate_coches import READ_DOM, detail_id, filter_outcomes, normalized  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "coordinator"
MESSAGE = "Coordinator local acceptance"
ITEM_COUNT = 14
COCHES_URL = (
    "https://www.coches.net/search/?hasPhoto=false&MaxPrice=20000&arrProvince=28"
    "&MakeIds%5B0%5D=7&ModelIds%5B0%5D=291"
)
LOCAL_GOAL = (
    "Filter the library to category eligible. For every result across every page, open its details, "
    f"enter '{MESSAGE}' in the message field, submit to this local test server, "
    "verify the submission confirmation, and return to the filtered results. Process each item exactly once."
)
COCHES_GOAL = (
    "Verify that the existing results are BMW Z4 in Madrid with a maximum price of 20,000 euros. "
    "Open exactly three distinct advertisements from the current results, inspect the visible title and price "
    "of each, and return to the results after each advertisement. Do not fill or submit any contact form."
)
READ_LOCAL = """(() => ({url:location.href,title:document.title,
  fixture:document.querySelector('main')?.dataset.fixture,
  item_id:document.querySelector('[data-item-id]')?.dataset.itemId || null,
  message:document.querySelector('[name=message]')?.value || '',
  confirmation:document.querySelector('#confirmation')?.textContent || '',
  accepted:document.querySelector('#confirmation')?.dataset.accepted === 'true',
  category:document.querySelector('[data-category]')?.dataset.category || '',
  page:document.querySelector('[data-page]')?.dataset.page || null,
  links:[...document.querySelectorAll('a[data-result]')].map(a=>a.href),
  end:!!document.querySelector('[data-end]')
}))()"""


def limits():
    return SupervisorLimits(max_actions=180, max_decisions=360, max_windows=360,
                            max_supervisor_calls=120, max_seconds=900)


class LocalFixture:
    """An independent HTTP service; its ledger never reads the agent's claims."""

    def __init__(self, *, ledger=None):
        self.lock = threading.Lock()
        self.ledger = {"submissions": [], "returns": [], "detail_reads": [], "result_reads": [], "rejected": []}
        if ledger is not None:
            if set(ledger) != set(self.ledger) or any(not isinstance(value, list) for value in ledger.values()):
                raise ValueError("Invalid saved fixture ledger")
            self.ledger = deepcopy(ledger)
        self.pending_return = None
        if self.ledger["submissions"]:
            latest = self.ledger["submissions"][-1]["item_id"]
            if latest not in self.ledger["returns"]:
                self.pending_return = latest
        self.server = None
        self.thread = None

    def snapshot(self):
        with self.lock:
            return deepcopy(self.ledger)

    def listing(self, query):
        category = query.get("category", [""])[0]
        number = 2 if query.get("page", ["1"])[0] == "2" else 1
        with self.lock:
            self.ledger["result_reads"].append({"page": number, "category": category})
            reads = len(self.ledger["result_reads"])
            if category == "eligible" and self.pending_return is not None:
                self.ledger["returns"].append(self.pending_return)
                self.pending_return = None
        form = ('<form action="/results"><label>Category<input name="category" value="'
                + escape(category, quote=True) + '"></label><button>Apply filter</button></form>')
        if category != "eligible":
            return '<h1>Library</h1><p>Choose category eligible to display the results.</p>' + form
        identifiers = list(range(1 if number == 1 else 8, 8 if number == 1 else 15))
        offset = reads % len(identifiers)
        identifiers = identifiers[offset:] + identifiers[:offset]
        identifiers.append(identifiers[0])
        cards = "".join(f'<article><a data-result href="/items/{item}">Item {item:02d}</a></article>'
                        for item in identifiers)
        navigation = ('<a href="/results?category=eligible&amp;page=2">Next page</a>' if number == 1
                      else '<p data-end="true">End of results. All 14 items are covered by the two pages.</p>')
        return (f'<h1 data-category="eligible" data-page="{number}">Eligible results — page {number} of 2</h1>'
                '<p>14 distinct results, seven per page. Repeated links refer to the same item.</p>'
                + form + cards + '<nav>' + navigation + '</nav>')

    def detail(self, item):
        with self.lock:
            self.ledger["detail_reads"].append(item)
            accepted = any(row["item_id"] == item for row in self.ledger["submissions"])
        confirmation = f"Submission confirmed for item {item}" if accepted else "No submission yet."
        page = 1 if item <= 7 else 2
        return (f'<h1 data-item-id="{item}">Item {item:02d}</h1><p>Category eligible. Local acceptance record.</p>'
                f'<form data-local-submit action="/submit/{item}" method="post">'
                '<label>Message<input name="message" autocomplete="off"></label>'
                f'<button{" disabled" if accepted else ""}>Submit locally</button></form>'
                f'<p id="confirmation" role="status" data-accepted="{str(accepted).lower()}">{confirmation}</p>'
                f'<a href="/results?category=eligible&amp;page={page}">Return to results</a>'
                '<script src="/submit.js"></script>')

    def submit(self, item, message):
        with self.lock:
            accepted = 1 <= item <= ITEM_COUNT and message == MESSAGE
            row = {"item_id": item, "message": message}
            self.ledger["submissions" if accepted else "rejected"].append(row)
            if accepted:
                self.pending_return = item
        return {"accepted": accepted, "item_id": item}

    def start(self, port=0):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid fixture port")
        if self.server is not None:
            raise ValueError("Fixture server is already started")
        fixture = self
        template = Template((FIXTURES / "page.html").read_text())

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, body, content_type="text/html; charset=utf-8", status=200):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/submit.js":
                    return self.respond((FIXTURES / "submit.js").read_text(), "application/javascript")
                if parsed.path in {"/", "/results"}:
                    content = fixture.listing(parse_qs(parsed.query))
                elif (match := re.fullmatch(r"/items/(\d+)", parsed.path)) and 1 <= int(match[1]) <= ITEM_COUNT:
                    content = fixture.detail(int(match[1]))
                else:
                    return self.respond("Not found", status=404)
                self.respond(template.substitute(content=content))

            def do_POST(self):
                match = re.fullmatch(r"/submit/(\d+)", urlparse(self.path).path)
                length = int(self.headers.get("Content-Length", "0"))
                if not match or length > 4096:
                    return self.respond("Rejected", status=400)
                data = parse_qs(self.rfile.read(length).decode())
                result = fixture.submit(int(match[1]), data.get("message", [""])[0])
                self.respond(json.dumps(result), "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/results"

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()


def local_outcome(ledger, observations):
    counts = Counter(row["item_id"] for row in ledger["submissions"])
    expected = set(range(1, ITEM_COUNT + 1))
    confirmed = {int(row["item_id"]) for row in observations if row.get("item_id") and row.get("accepted")}
    result = {
        "submitted_items": sorted(counts), "submission_counts": dict(counts),
        "returned_items": sorted(set(ledger["returns"])), "confirmed_items": sorted(confirmed),
        "pages_observed": sorted({int(row["page"]) for row in observations if row.get("page")}),
        "end_observed": any(row.get("end") for row in observations), "rejected_submissions": ledger["rejected"],
    }
    result["verified"] = bool(
        set(counts) == expected and all(count == 1 for count in counts.values())
        and set(ledger["returns"]) == expected and confirmed == expected
        and result["pages_observed"] == [1, 2] and result["end_observed"] and not ledger["rejected"]
        and all(row["message"] == MESSAGE for row in ledger["submissions"])
    )
    return result


def same_origin(left, right):
    first, second = urlparse(left), urlparse(right)
    return (first.scheme, first.hostname, first.port) == (second.scheme, second.hostname, second.port)


def permitted_action(scenario, origin, action, page, text=None):
    kind, label = action.get("kind"), normalized(action.get("label", ""))
    if not same_origin(page.get("url", ""), origin):
        return False
    if kind in {"scroll", "wait"}:
        return True
    href = action.get("href", "")
    if scenario == "local":
        if kind == "fill":
            return (label == "category" and text == "eligible") or (label == "message" and text == MESSAGE)
        if kind in {"click", "back"} and href:
            return same_origin(href, origin) and bool(re.fullmatch(r"/(?:results|items/\d+)", urlparse(href).path))
        return kind == "click" and label in {"apply filter", "submit locally", "category", "message"}
    if kind == "back":
        return same_origin(href, origin)
    if kind != "click":
        return False
    if href:
        return same_origin(href, origin) and bool(detail_id(href) or label in {"volver", "volver al listado"})
    return not detail_id(page.get("url", "")) and label in {
        "aceptar", "aceptar todas", "aceptar y cerrar", "aceptar y continuar", "aceptar cookies",
    }


class GuardedBrowser(Browser):
    def __init__(self, url, *, scenario, observations):
        self.scenario, self.origin, self.observations = scenario, url, observations
        self.allowed_actions, self.denied_actions = [], []
        self.fixture = None
        super().__init__(url, enable_history=True)

    @classmethod
    def attach_evaluation(cls, target, *, origin, observations, previous_report, fixture):
        browser = super().attach(target)
        browser.scenario, browser.origin, browser.observations = "local", origin, observations
        browser.allowed_actions = deepcopy(previous_report.get("allowed_actions", []))
        browser.denied_actions = deepcopy(previous_report.get("denied_actions", []))
        browser.fixture = fixture
        return browser

    def observe(self, screenshot=False):
        observed = super().observe(screenshot=screenshot)
        oracle = self.evaluate(READ_LOCAL if self.scenario == "local" else READ_DOM)
        if not isinstance(oracle, dict):
            raise ValueError("Independent reader returned no page")
        self.observations.append(oracle)
        return observed

    def act(self, action, page, text=None, **kwargs):
        record = {"kind": action.get("kind"), "label": action.get("label"), "href": action.get("href")}
        duplicate_submit = False
        if self.fixture and action.get("kind") == "click" and normalized(action.get("label")) == "submit locally":
            match = re.fullmatch(r"/items/(\d+)", urlparse(page.get("url", "")).path)
            duplicate_submit = bool(match and any(row["item_id"] == int(match[1])
                                                 for row in self.fixture.snapshot()["submissions"]))
        if duplicate_submit or not permitted_action(self.scenario, self.origin, action, page, text):
            self.denied_actions.append(record)
            raise AssertionError("Evaluation guard rejected a browser input")
        self.allowed_actions.append(record)
        return super().act(action, page, text, **kwargs)


def contract_for(goal, scenario):
    def task(identifier, instruction, criterion, requirement, quote, *, scope="current"):
        return {"id": identifier, "instruction": instruction, "success_criteria": [criterion],
                "requirement_ids": [requirement], "goal_quote": quote, "verification_scope": scope}

    local = scenario == "local"
    requirements = [{"id": "items", "goal_quote": goal, "description": goal,
                     "status": "unknown", "evidence_refs": []}]
    tasks = []
    if local:
        requirements.insert(0, {"id": "filter", "goal_quote": "Filter the library to category eligible.",
                               "description": "Eligible filtered results are visible", "status": "unknown",
                               "evidence_refs": []})
        tasks.append({**task("filter", "Set Category to eligible and apply the filter.",
                             "Eligible results are visible.", "filter", "Filter the library to category eligible."),
                      "depends_on": []})
    else:
        quote = "Verify that the existing results are BMW Z4 in Madrid with a maximum price of 20,000 euros."
        requirements.insert(0, {"id": "filter", "goal_quote": quote, "description": quote,
                               "status": "unknown", "evidence_refs": []})
        tasks.append({**task("filter", "Verify the existing BMW Z4, Madrid and maximum price 20,000 euros filters. "
                             "Inspect their current evidence without changing the existing search.",
                             "The results heading confirms BMW Z4 and Madrid and the URL or applied filter "
                             "confirms a maximum price of 20,000 euros.", "filter", quote), "depends_on": []})
    effect = (f"Open the assigned observed item. Fill Message with '{MESSAGE}', submit locally, "
              "and verify its submission confirmation. Stay on the item details."
              if local else "Open the assigned observed advertisement. Inspect its visible title and price. "
              "Stay on the advertisement details. Do not fill or submit any form.")
    steps = [
        task("inspect", effect, "Submission confirmation for this item is visible." if local else
             "The assigned advertisement title and price are visible.", "items", goal, scope="destination"),
        task("return", "Return to the results after this item, using its observed return link or browser history.",
             "The results list is visible after visiting this item.", "items", goal),
    ]
    return {
        "version": 2, "requirements": requirements, "tasks": tasks,
        "loops": [{"id": "results", "goal_quote": goal, "description": "Every requested result",
                   "requirement_ids": ["items"], "depends_on": ["filter"],
                   "discovery": task("discover", "Expose the next page of results using observed pagination. "
                                     "Only discover links; do not process an item.",
                                     "New result links or the end of results are observed.", "items", goal),
                   "steps": steps, "limit": None if local else 3, "order": "observed"}],
        "execution_constraints": [
            "Process each identity once", "Never repeat a submission",
            "Submit only to this local fixture" if local else "Never fill or submit contact forms",
        ],
    }


def fixture_reply(payload, scenario):
    """A deterministic fake model; responses still traverse production tools and validators."""
    phase = payload["phase"]
    if phase == "initialize":
        return "initialize_plan", {"contract": contract_for(payload["goal"], scenario)}
    if phase == "audit":
        return "audit_plan", {"approved": True, "problems": [], "replacement_contract": None}
    facts, current = payload.get("evidence", []), set(payload.get("current_evidence_refs", []))
    current_facts = [fact for fact in facts if fact["ref"] in current]
    links = [fact for fact in payload.get("observed_links", []) if (
        bool(re.fullmatch(r"/items/\d+", urlparse(fact.get("href", "")).path)) if scenario == "local"
        else bool(detail_id(fact.get("href", "")))
    )]
    result = {"requirement_updates": [], "subtask_review": None,
              "discovered": [{"loop_id": "results", "item_refs": [fact["ref"] for fact in links]}] if links else [],
              "loop_reviews": [], "reconcile_attempts": [], "next_action": "continue",
              "repair": None, "feedback": None, "stop_reason": None}
    last = payload.get("last_result") or {}
    task = last.get("task") or {}
    instruction = task.get("instruction", "").lower()
    refs = []
    if last:
        if "set category" in instruction:
            refs = [fact["ref"] for fact in current_facts if "Eligible results" in fact.get("text", "")]
            if refs:
                result["requirement_updates"].append({"id": "filter", "status": "satisfied", "evidence_refs": refs})
        elif "verify the existing" in instruction:
            text_refs = [fact["ref"] for fact in current_facts if "bmw z4" in normalized(fact.get("text", ""))
                         and "madrid" in normalized(fact.get("text", ""))]
            url_refs = [fact["ref"] for fact in current_facts
                        if parse_qs(urlparse(fact.get("url", "")).query).get("MaxPrice") == ["20000"]]
            if text_refs and url_refs:
                refs = text_refs + url_refs
                result["requirement_updates"].append({"id": "filter", "status": "satisfied", "evidence_refs": refs})
        elif "return to the results" in instruction:
            refs = [fact["ref"] for fact in current_facts if (
                urlparse(fact.get("url", "")).path == "/results" if scenario == "local"
                else fact.get("kind") == "page" and fact.get("url") and not detail_id(fact["url"]))]
        elif "only discover links" in instruction:
            refs = [fact["ref"] for fact in current_facts if fact.get("kind") == "page"]
        elif scenario == "local":
            active = payload.get("active_work") or {}
            item = payload.get("items", {}).get(active.get("item_id"), {})
            number = urlparse(item.get("url", payload.get("page", {}).get("url", ""))).path.rsplit("/", 1)[-1]
            refs = [fact["ref"] for fact in facts
                    if f"Submission confirmed for item {number}" in fact.get("text", "")]
        else:
            refs = [fact["ref"] for fact in current_facts if fact.get("kind") == "page"
                    and detail_id(fact.get("observed_url", fact.get("url", "")))]
        result["subtask_review"] = {"delegation_id": last["delegation_id"],
                                    "status": "satisfied" if refs else "unknown", "evidence_refs": refs}
    end_refs = [fact["ref"] for fact in facts if "End of results." in fact.get("text", "")]
    items = list(payload.get("items", {}).values())
    completed = [item for item in items if item.get("status") == "completed"]
    finishing_last = bool(last and "return to the results" in instruction and refs
                          and len(completed) == (13 if scenario == "local" else 2))
    all_completed = len(completed) == (ITEM_COUNT if scenario == "local" else 3)
    if scenario == "local" and end_refs and (all_completed or finishing_last):
        result["loop_reviews"] = [{"loop_id": "results", "exhausted": True, "evidence_refs": end_refs}]
    if scenario == "coches" and (all_completed or finishing_last):
        result["loop_reviews"] = [{
            "loop_id": "results", "exhausted": True,
            "evidence_refs": [fact["ref"] for fact in current_facts if fact.get("kind") == "page"],
        }]
    if items and (all_completed or finishing_last):
        refs = list(dict.fromkeys(ref for item in items for ref in item.get("evidence_refs", [])))
        refs = refs or [fact["ref"] for fact in current_facts if fact.get("kind") == "page"]
        result["requirement_updates"].append({"id": "items", "status": "satisfied", "evidence_refs": refs[:64]})
    return "review_progress", result


class FixtureToolModel(BaseChatModel):
    scenario: str = "local"
    calls: int = 0
    tools_seen: list[Any] = Field(default_factory=list)
    payloads: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-coordinator-acceptance"

    def bind_tools(self, tools, **_kwargs):
        self.tools_seen = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **_kwargs):
        payload = json.loads(messages[-1].content)
        self.payloads.append(payload)
        name, args = fixture_reply(payload, self.scenario)
        offered = {tool.name: tool for tool in self.tools_seen}
        if name not in offered:
            raise AssertionError("Fake model selected an unavailable production tool")
        offered[name].args_schema.model_validate(args)
        self.calls += 1
        message = AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": f"fixture-{self.calls}", "type": "tool_call",
        }])
        return ChatResult(generations=[ChatGeneration(message=message)])


def action_decision(page, action=None, terminal="DONE"):
    if action is None:
        return {"choice": terminal, "operation": terminal, "target": None, "confidence": 1,
                "probabilities": {terminal: 1}, "model": "offline-coordinator", "http_attempts": 0,
                "latency_ms": 0, "usage": {}}
    operation = {"click": "CLICK", "fill": "TYPE_TEXT", "back": "BACK", "wait": "WAIT",
                 "scroll": "SCROLL_DOWN", "select": "SELECT"}[action["kind"]]
    _, targets, _ = model.action_space(page["actions"])
    target = next((key for key, candidate in targets.get(operation, {}).items()
                   if candidate["id"] == action["id"]), None)
    return {"choice": action["id"], "operation": operation, "target": target, "confidence": 1,
            "probabilities": {action["id"]: 1}, "model": "offline-coordinator", "http_attempts": 0,
            "latency_ms": 0, "usage": {}}


class FixturePolicy:
    def __init__(self, scenario="local"):
        self.scenario = scenario

    def __call__(self, page, instruction, history):
        actions = page["actions"]
        current = page.get("url", "")
        lower = instruction.lower()
        action = None
        if self.scenario == "coches":
            consent = {"aceptar", "aceptar todas", "aceptar y cerrar", "aceptar y continuar", "aceptar cookies"}
            action = next((item for item in actions if item["kind"] == "click" and normalized(item.get("label", ""))
                           in consent), None)
        if action:
            return action_decision(page, action)
        if "set category" in lower:
            if "category=eligible" in current:
                return action_decision(page)
            field = next((item for item in actions if item["kind"] == "fill" and item.get("label") == "Category"), None)
            if field and field.get("value", field.get("current_value")) != "eligible":
                action = field
            else:
                action = next((item for item in actions if normalized(item.get("label", "")) == "apply filter"), None)
        elif "verify the existing" in lower:
            content = normalized(page.get("text", ""))
            if ("bmw z4" in content and "madrid" in content
                    and parse_qs(urlparse(current).query).get("MaxPrice") == ["20000"]):
                return action_decision(page)
            action = next((item for item in actions if item["kind"] == "wait"), None)
        elif "return to the results" in lower:
            is_detail = (bool(re.fullmatch(r"/items/\d+", urlparse(current).path)) if self.scenario == "local"
                         else bool(detail_id(current)))
            if not is_detail:
                return action_decision(page)
            action = next((item for item in actions if normalized(item.get("label", "")) == "return to results"), None)
            action = action or next((item for item in actions if item["kind"] == "back"), None)
        elif "only discover links" in lower:
            action = next((item for item in actions if normalized(item.get("label", "")) == "next page"), None)
            if action is None and "End of results." in page.get("text", ""):
                return action_decision(page)
        else:
            matched = re.search(r"OBSERVED ITEM URL:\n([^\n]+)", instruction)
            expected = matched[1].strip() if matched else None
            if expected and current != expected:
                action = next((item for item in actions if item.get("href") == expected), None)
            elif self.scenario == "coches":
                if re.search(r"\d[\d.\s,]*\s*€", page.get("text", "")):
                    return action_decision(page)
                action = next((item for item in actions if item["kind"] == "wait"), None)
            elif "Submission confirmed for item " in page.get("text", ""):
                return action_decision(page)
            else:
                field = next((item for item in actions
                              if item["kind"] == "fill" and item.get("label") == "Message"), None)
                if field and field.get("value", field.get("current_value")) != MESSAGE:
                    action = field
                else:
                    action = next((item for item in actions
                                   if normalized(item.get("label", "")) == "submit locally"), None)
        if action is None and page.get("candidate_coverage", {}).get("more_available"):
            return action_decision(page, terminal="MORE_CANDIDATES")
        if action is None:
            action = next((item for item in actions if item["kind"] == "scroll"
                           and item.get("direction", "down") == "down"), None)
        return action_decision(page, action, terminal="BLOCKED")


@contextmanager
def simulated_providers(scenario):
    with ExitStack() as stack:
        stack.enter_context(patch.object(worker_module, "choose", FixturePolicy(scenario)))
        stack.enter_context(patch.object(worker_module, "field_text", lambda context: (
            "eligible" if context.get("field", {}).get("label") == "Category" else MESSAGE,
            {"model": "offline-text", "http_attempts": 0, "latency_ms": 0, "usage": {}},
        )))
        blocked = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError("Paid models blocked")))
                   for owner, name in ((model, "post_json"), (model.CLIENT, "post"))]
        yield SupervisorPolicy(model=FixtureToolModel(scenario=scenario)), blocked


def coches_outcome(observations):
    observed_sources, visits, returns = set(), {}, set()
    pending, applied_filters = None, None
    for row in observations:
        identity = detail_id(row.get("url", ""))
        if not identity:
            filters = filter_outcomes(row, [])
            if not all(value["status"] == "verified" for value in filters.values()):
                continue
            if applied_filters is None:
                applied_filters = filters
            observed_sources.update(detail_id(link.get("href", "")) for link in row.get("anchors", [])
                                    if link.get("result_card") is True and not link.get("in_panel"))
            if pending:
                returns.add(pending)
                pending = None
        elif identity in observed_sources:
            text = " ".join(str(row.get(key, "")) for key in ("title", "headings", "text"))
            if row.get("title") and re.search(r"\d[\d.\s,]*\s*€", text):
                visits[identity] = {"url": row["url"], "title": row["title"]}
                pending = identity
    return {"verified": applied_filters is not None and len(visits) == len(returns) == 3,
            "visits": visits, "returned_items": sorted(returns), "filter_outcomes": applied_filters,
            "filters_provided_by_initial_url": True}


def run_one(output, *, scenario="local", live_models=False, close_tab=False, url=None):
    output.mkdir(parents=True, exist_ok=True)
    observations, trace = [], []
    browser, agent, final, error = None, None, {}, None
    fixture, blocked = None, []
    with ExitStack() as stack:
        if scenario == "local":
            fixture = stack.enter_context(LocalFixture())
            url = fixture.url
        else:
            url = url or COCHES_URL
        supervisor = None
        if not live_models:
            supervisor, blocked = stack.enter_context(simulated_providers(scenario))
        try:
            browser = GuardedBrowser(url, scenario=scenario, observations=observations)
            browser.fixture = fixture
            kwargs = {"supervisor": supervisor} if supervisor is not None else {}
            agent = SupervisedAgent(url, LOCAL_GOAL if scenario == "local" else COCHES_GOAL,
                                    browser=browser, state_dir=output / "state", limits=limits(), **kwargs)
            for final in agent.run():
                trace.append(final)
        except Exception as caught:
            error = {"type": type(caught).__name__}
        finally:
            if agent:
                final = agent.snapshot()
                agent.close() if close_tab else agent.disconnect()
            elif browser:
                browser.close() if close_tab else browser.disconnect()
        ledger = fixture.snapshot() if fixture else None
        outcome = local_outcome(ledger, observations) if fixture else coches_outcome(observations)
        report = {"scenario": scenario, "models": "live" if live_models else "simulated-strict-tools",
                  "goal": LOCAL_GOAL if fixture else COCHES_GOAL, "outcome": outcome,
                  "status": final.get("status", "error"), "stop_reason": final.get("stop_reason"),
                  "metrics": final.get("metrics", {}), "run_id": final.get("run_id"), "error": error,
                  "blocked_model_attempts": sum(mock.call_count for mock in blocked),
                  "allowed_actions": browser.allowed_actions if browser else [],
                  "denied_actions": browser.denied_actions if browser else [],
                  "feedback_pauses": sum(row.get("status") == "waiting_feedback" for row in trace),
                  "limits": {"actions": 180, "decisions": 360, "windows": 360, "supervisor_calls": 120, "seconds": 900}}
        report["verified"] = bool(outcome["verified"] and final.get("status") == "done" and not error
                                  and not report["denied_actions"] and not report["blocked_model_attempts"]
                                  and not report["feedback_pauses"])
        for name, data in (("report", report), ("trace", trace), ("observations", observations), ("ledger", ledger)):
            (output / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
        return report


def load_resume_evaluation(source, output, *, live_models=False):
    """Read artifacts and the checkpoint without opening Chrome, changing state, or binding a port."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Resume output must be separate from the original evaluation directory")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Resume output must be a new or empty directory")
    saved = {name: json.loads((source / f"{name}.json").read_text())
             for name in ("report", "ledger", "observations", "trace")}
    report = saved["report"]
    if report.get("scenario") != "local":
        raise ValueError("Only local fixture evaluations support restoration")
    if report.get("models") == "live" and not live_models:
        raise ValueError("Resuming a live-model evaluation requires --live-models")
    run_id = report.get("run_id", "")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid saved run ID")
    state_dir = Path(report.get("state_dir", source / "state")).resolve()
    checkpoint = state_dir / run_id / "state.sqlite"
    with sqlite3.connect(checkpoint.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT state_json FROM supervisor_runs WHERE run_id=?", (run_id,)).fetchone()
        action_count = connection.execute(
            "SELECT COUNT(*) FROM supervisor_attempts WHERE run_id=? AND state != 'rejected'", (run_id,),
        ).fetchone()[0]
    if row is None:
        raise ValueError("Saved evaluation checkpoint is missing")
    state = json.loads(row[0])
    if state.get("contract_version") != 2:
        raise ValueError("Resume evaluation requires an existing version-two checkpoint")
    if not state.get("target") or state.get("goal") != report.get("goal"):
        raise ValueError("Saved checkpoint does not match the original evaluation")
    worker = state.get("worker", {})
    checkpoint_metrics = {
        "actions": action_count, "supervisor_calls": state.get("supervisor_calls", 0),
        "supervisor_http_attempts": state.get("supervisor_http_attempts", 0),
        "jev_calls": worker.get("model_calls", 0), "jev_http_attempts": worker.get("model_http_attempts", 0),
        "text_calls": worker.get("text_model_calls", len(worker.get("text_calls", []))),
        "candidate_windows": worker.get("candidate_windows", 0),
    }
    mismatches = [key for key, value in checkpoint_metrics.items()
                  if type(report.get("metrics", {}).get(key)) is not int or report["metrics"][key] != value]
    if mismatches:
        raise ValueError("Stale evaluation artifacts differ from the checkpoint; resume the latest output: "
                         + ", ".join(mismatches))
    if not saved["observations"]:
        raise ValueError("Saved evaluation has no independent origin observation")
    origin = saved["observations"][0].get("url", "")
    parsed = urlparse(origin)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or not same_origin(origin, state.get("initial_url", ""))
            or any(not same_origin(origin, observation.get("url", "")) for observation in saved["observations"])):
        raise ValueError("Saved fixture origin is not one consistent loopback address")
    LocalFixture(ledger=saved["ledger"])
    return {**saved, "source": source, "output": output, "state_dir": state_dir,
            "state": state, "origin": origin, "port": parsed.port}


def resume_evaluation(source, output, *, live_models=False, close_tab=False):
    saved = load_resume_evaluation(source, output, live_models=live_models)
    output = saved["output"]
    output.mkdir(parents=True, exist_ok=True)
    observations, trace = deepcopy(saved["observations"]), deepcopy(saved["trace"])
    original_report, state = saved["report"], saved["state"]
    browser, agent, error, final = None, None, None, {}
    blocked = []
    fixture = LocalFixture(ledger=saved["ledger"])
    with ExitStack() as stack:
        try:
            fixture.start(port=saved["port"])
            stack.callback(fixture.close)
            supervisor = None
            if not live_models:
                supervisor, blocked = stack.enter_context(simulated_providers("local"))
            browser = GuardedBrowser.attach_evaluation(
                state["target"], origin=saved["origin"], observations=observations,
                previous_report=original_report, fixture=fixture,
            )
            kwargs = {"supervisor": supervisor} if supervisor is not None else {}
            agent = SupervisedAgent.resume(state["run_id"], state_dir=saved["state_dir"], browser=browser, **kwargs)
            for final in agent.run():
                trace.append(final)
        except Exception as caught:
            error = {"type": type(caught).__name__}
        finally:
            if agent:
                final = agent.snapshot()
                agent.close() if close_tab else agent.disconnect()
            elif browser:
                browser.close() if close_tab else browser.disconnect()
        ledger = fixture.snapshot()
        outcome = local_outcome(ledger, observations)
        report = {
            "scenario": "local", "models": "live" if live_models else "simulated-strict-tools",
            "goal": state["goal"], "outcome": outcome, "status": final.get("status", "error"),
            "stop_reason": final.get("stop_reason"), "metrics": final.get("metrics", {}),
            "run_id": state["run_id"], "error": error,
            "blocked_model_attempts": original_report.get("blocked_model_attempts", 0)
            + sum(mock.call_count for mock in blocked),
            "allowed_actions": browser.allowed_actions if browser else original_report.get("allowed_actions", []),
            "denied_actions": browser.denied_actions if browser else original_report.get("denied_actions", []),
            "feedback_pauses": sum(row.get("status") == "waiting_feedback" for row in trace),
            "limits": deepcopy(original_report.get("limits", state["limits"])),
            "state_dir": str(saved["state_dir"]), "resumed_from": str(saved["source"]),
            "same_original_tab": browser is not None and browser.target == state["target"],
            "same_fixture_origin": fixture.server is not None and same_origin(fixture.url, saved["origin"]),
            "initial_submission_count": len(saved["ledger"]["submissions"]),
            "resume_start_metrics": deepcopy(original_report.get("metrics", {})),
        }
        report["verified"] = bool(outcome["verified"] and final.get("status") == "done" and not error
                                  and not report["denied_actions"] and not report["blocked_model_attempts"]
                                  and not report["feedback_pauses"] and report["same_original_tab"]
                                  and report["same_fixture_origin"])
        for name, data in (("report", report), ("trace", trace), ("observations", observations), ("ledger", ledger)):
            (output / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["local", "coches", "all"], default="local")
    parser.add_argument("--runs", type=int, help="Override defaults: 3 local runs and 2 coches runs.")
    parser.add_argument("--live-models", action="store_true", help="Explicitly enable configured paid model providers.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--url", help="Optional coches.net results URL")
    parser.add_argument("--close-tab", action="store_true")
    parser.add_argument("--resume-evaluation", type=Path,
                        help="Restore a saved local fixture on its original port and resume its original run and tab.")
    args = parser.parse_args(argv)
    if args.runs is not None and not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    if args.url and (urlparse(args.url).hostname != "www.coches.net" or urlparse(args.url).scheme != "https"):
        parser.error("--url must be an HTTPS www.coches.net results URL")
    if args.resume_evaluation:
        if args.output is None or args.runs is not None or args.url is not None or args.scenario != "local":
            parser.error("--resume-evaluation needs a separate --output; do not combine with --runs, --url or web")
        try:
            load_resume_evaluation(args.resume_evaluation, args.output, live_models=args.live_models)
        except (ValueError, OSError, sqlite3.Error) as error:
            parser.error(str(error))
        if args.live_models:
            load_environment()
        report = resume_evaluation(args.resume_evaluation, args.output, live_models=args.live_models,
                                   close_tab=args.close_tab)
        print(json.dumps({"scenario": "local-resumed", "verified": report["verified"],
                          "status": report["status"], "stop_reason": report["stop_reason"]}))
        return 0 if report["verified"] else 2
    if args.live_models:
        load_environment()
    output = args.output or ROOT / "artifacts" / ("coordinator-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    reports = []
    scenarios = ["local", "coches"] if args.scenario == "all" else [args.scenario]
    for scenario in scenarios:
        for attempt in range(args.runs or (3 if scenario == "local" else 2)):
            report = run_one(output / f"{scenario}-{attempt + 1}", scenario=scenario,
                             live_models=args.live_models, close_tab=args.close_tab, url=args.url)
            reports.append(report)
            print(json.dumps({"scenario": scenario, "attempt": attempt + 1, "verified": report["verified"],
                              "status": report["status"], "stop_reason": report["stop_reason"]}))
    summary = {"verified": all(report["verified"] for report in reports), "runs": reports}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return 0 if summary["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
