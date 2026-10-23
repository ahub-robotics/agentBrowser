"""Explicit real coches.net acceptance with simulated models and no contact submissions.

The site-specific policy and DOM oracle belong only to this evaluator. No paid
model endpoint is reachable. The working tab is preserved unless --close-tab.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentrun import SupervisorLimits  # noqa: E402
from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent  # noqa: E402
from agentrun.supervisor_policy import SupervisorPolicy  # noqa: E402
from AgentBrowser import agent as worker_module  # noqa: E402
from AgentBrowser import model  # noqa: E402
from AgentBrowser.browser import Browser  # noqa: E402
from scripts.evaluate_coches import READ_DOM, detail_id, normalized  # noqa: E402

URL = "https://www.coches.net/segunda-mano/"
MESSAGE = "Me interesa el Vehiculo"
GOAL = (
    "Inspect exactly three distinct advertisements from the current results. "
    f"In each advertisement fill the contact message with '{MESSAGE}' without submitting it. "
    "Return to the results after each advertisement. Stop after three verified fills and returns."
)
COOKIE_BUTTONS = {"aceptar", "aceptar todas", "aceptar y cerrar", "aceptar y continuar", "aceptar cookies"}
READ_MESSAGES_DOM = READ_DOM.replace(
    "querySelectorAll('input,select,button,", "querySelectorAll('input,textarea,select,button,",
).replace(
    "text(e.labels?.[0])||text(row)||text(e)",
    "text(e.labels?.[0])||e.getAttribute('placeholder')||e.name||text(row)||text(e)",
)


def is_message(control):
    label = normalized(control.get("label", ""))
    return label == "contacta con el anunciante" or any(
        word in label for word in ("mensaje", "message", "comentario")
    )


def choose_action(page, goal, history, *, starting_action=0):
    """A deterministic stand-in for Jev, selecting only offered action IDs."""
    actions = page["actions"]
    url = page.get("url", "")
    action = next((item for item in actions if item["kind"] == "click"
                   and normalized(item.get("label", "")) in COOKIE_BUTTONS), None)
    if action is None and goal.startswith("Return to the results"):
        if not detail_id(url):
            return decision("DONE")
        action = next((item for item in actions if item["kind"] == "back"), None)
    elif action is None and goal.startswith("Open the observed advertisement"):
        expected = goal.split("\n", 1)[1]
        if detail_id(url) == detail_id(expected):
            fields = [item for item in actions if item["kind"] == "fill" and is_message(item)]
            if any(item.get("value") == MESSAGE or item.get("current_value") == MESSAGE for item in fields):
                return decision("DONE")
            action = next(iter(fields), None)
        elif any(entry.get("kind") == "click" and entry.get("href") == expected and entry.get("url") == url
                 for entry in history):
            # An acknowledged link click can precede an asynchronous navigation.
            # Its still-visible source is not permission to dispatch it again.
            action = next((item for item in actions if item["kind"] == "wait"), None)
            if action is None:
                return decision("BLOCKED")
        else:
            action = next((item for item in actions if item["kind"] == "click"
                           and detail_id(item.get("href", "")) == detail_id(expected)), None)
    elif action is None and goal.startswith("Discover"):
        # One scroll per delegation; the supervisor inventories the next observation.
        if len(history) > starting_action and history[-1].get("kind") == "scroll":
            return decision("DONE")
    if action is None and page.get("candidate_coverage", {}).get("more_available"):
        return decision("MORE_CANDIDATES")
    if action is None:
        action = next((item for item in actions if item["kind"] == "scroll"
                       and item.get("direction", "down" if item.get("delta", 0) > 0 else "up") == "down"), None)
    if action is None:
        return decision("BLOCKED")
    result = decision(action["id"], action)
    _, targets, _ = model.action_space(actions)
    result["target"] = next((index for index, item in targets.get(result["operation"], {}).items()
                             if item["id"] == action["id"]), None)
    return result


class ScriptedPolicy:
    def __init__(self):
        self.goal = None
        self.starting_action = 0

    def __call__(self, page, goal, history):
        # The handoff may start directly with its subtask. Its former global-goal
        # preamble is not part of the worker contract.
        if "CURRENT SUBTASK:\n" in goal:
            goal = goal.split("CURRENT SUBTASK:\n", 1)[1].split("\nSUCCESS CRITERIA:\n", 1)[0]
        if goal != self.goal:
            self.goal = goal
            self.starting_action = len(history)
        return choose_action(page, goal, history, starting_action=self.starting_action)


def decision(choice, action=None):
    operation = {
        "click": "CLICK", "fill": "TYPE_TEXT", "back": "BACK", "scroll": "SCROLL_DOWN", "wait": "WAIT",
    }.get((action or {}).get("kind"), choice)
    return {"choice": choice, "operation": operation, "confidence": 1,
            "probabilities": {choice: 1}, "model": "simulated-coches-policy", "http_attempts": 0,
            "latency_ms": 0, "target": None, "usage": {}}


class ScriptedSupervisor:
    """Derive every decision from persistent payload data, including after resume."""

    def __call__(self, payload):
        facts = payload["evidence"]
        current = set(payload.get("current_evidence_refs", []))
        page = payload["page"]
        items = payload.get("items", {})
        items = list(items.values()) if isinstance(items, dict) else items
        links = {}
        identities = set()
        for fact in facts:
            href = fact.get("href", "")
            identity = detail_id(href)
            if (fact.get("ref") in current and identity and identity not in identities
                    and not fact.get("href_truncated") and not detail_id(fact.get("observed_url", ""))):
                links[href] = fact["ref"]
                identities.add(identity)
        selected = list(dict.fromkeys(item["url"] for item in items))
        selected_ids = {detail_id(url) for url in selected}
        if not any(item.get("status") != "completed" for item in items) and len(selected) < 3:
            # Results can reshuffle on BACK. Commit only the next currently
            # observed item, instead of reserving stale links before a visit.
            selected.extend([url for url in links if detail_id(url) not in selected_ids][:1])
        refs = [item["source_ref"] for item in items]
        refs.extend(links[url] for url in selected if url in links and links[url] not in refs)
        complete = [item for item in items if item.get("status") == "completed"]
        on_detail = bool(detail_id(page.get("url", "")))
        message_facts = [fact for fact in facts if fact.get("ref") in current and is_message(fact)
                         and (fact.get("value") == MESSAGE or fact.get("current_value") == MESSAGE)]
        page_refs = [fact["ref"] for fact in facts if fact.get("ref") in current and "url" in fact]
        item_reviews = []
        for item in items:
            if detail_id(item["url"]) == detail_id(page.get("url", "")) and message_facts:
                item_reviews.append({"item_id": item["item_id"], "status": "completed",
                                     "evidence_refs": [fact["ref"] for fact in message_facts]})
        completed_ids = {item["item_id"] for item in complete} | {item["item_id"] for item in item_reviews}
        done = len(completed_ids) == 3 and not on_detail
        final_refs = list(dict.fromkeys([
            *page_refs, *(ref for item in complete for ref in item.get("evidence_refs", [])),
        ]))
        result = {
            "action": "finish" if done else "delegate",
            "plan": ["Discover three advertisement URLs", "Fill and verify each message", "Return to results"],
            "requirements": [{
                "id": "three-filled-and-returned", "goal_quote": GOAL,
                "description": "Three distinct observed ads filled without submission, returned to results",
                "status": "satisfied" if done else "unsatisfied",
                "evidence_refs": final_refs if done else [],
            }],
            "loops": [{
                "id": "three-advertisements", "goal_quote": "exactly three distinct advertisements",
                "description": "Explicitly bounded three-advertisement acceptance sample",
                "item_refs": refs, "exhausted": len(selected) == 3, "evidence_refs": refs,
            }],
            "item_reviews": item_reviews, "evidence_refs": final_refs if done else [], "task": None,
        }
        if done:
            return result
        counter = payload.get("metrics", {}).get("supervisor_calls", 0)
        if on_detail and message_facts:
            instruction = "Return to the results using the observed browser history."
            item_ref = None
        else:
            pending = next((item for item in items if item["item_id"] not in completed_ids), None)
            if pending:
                url, item_ref = pending["url"], pending["item_id"]
            else:
                url = next((url for url in links if url in selected
                            and not any(item["url"] == url for item in items)), None)
                item_ref = links.get(url)
            if url:
                instruction = (
                    "Open the observed advertisement and fill its contact message with "
                    f"'{MESSAGE}' without submitting.\n" + url
                )
            else:
                instruction = (
                    "Discover another advertisement by scrolling the results once, then return control. "
                    f"Round {counter}."
                )
                item_ref = None
        result["task"] = {
            "id": f"evaluation-{counter}-{len(items)}-{len(completed_ids)}-{'detail' if on_detail else 'list'}",
            "instruction": instruction,
            "success_criteria": ["Read the resulting page and field values without submitting any contact form"],
            "item_ref": item_ref,
        }
        return result


class GuardedBrowser(Browser):
    """Permit only observed ad links, consent, scrolling, BACK and message fills."""

    def __init__(self, url, *, observations, candidate_limits=None):
        self.observations = observations
        self.allowed_actions = []
        self.denied_actions = []
        super().__init__(url, candidate_limits=candidate_limits, enable_history=True)

    def observe(self, screenshot=False):
        page = super().observe(screenshot=screenshot)
        observed = self.evaluate(READ_MESSAGES_DOM)
        if not isinstance(observed, dict):
            raise ValueError("Independent DOM reader returned no document")
        self.observations.append(observed)
        return page

    def act(self, action, page, text=None, **kwargs):
        kind = action.get("kind")
        permitted = kind in {"back", "scroll", "wait"}
        if kind == "click":
            permitted = bool(detail_id(action.get("href", ""))) or (
                normalized(action.get("label", "")) in COOKIE_BUTTONS and not detail_id(page.get("url", ""))
            )
        if kind == "fill":
            permitted = bool(detail_id(page.get("url", ""))) and is_message(action) and text == MESSAGE
        record = {"kind": kind, "label": action.get("label"), "url": page.get("url"), "href": action.get("href")}
        if not permitted:
            self.denied_actions.append(record)
            raise AssertionError("Acceptance guard forbids this browser mutation, including contact submission")
        self.allowed_actions.append(record)
        return super().act(action, page, text, **kwargs)


def outcome(observations):
    """Independent oracle: exact field contents on three distinct ads, followed by list returns."""
    filled, returned, sources = {}, set(), {}
    pending = None
    for observation in observations:
        identity = detail_id(observation.get("url", ""))
        if not identity:
            for link in observation.get("anchors", []):
                ad_id = detail_id(link.get("href", ""))
                if ad_id and link.get("result_card") is True and not link.get("in_panel"):
                    sources[ad_id] = observation["url"]
        if identity in sources and any(is_message(control) and control.get("value") == MESSAGE
                                       for control in observation.get("controls", [])):
            filled[identity] = observation["url"]
            pending = identity
        elif not identity and pending and any(detail_id(link.get("href", ""))
                                              for link in observation.get("anchors", [])):
            returned.add(pending)
            pending = None
    return {"status": "verified" if len(filled) == len(returned) == 3 else "unknown",
            "filled_advertisements": filled, "returned_advertisements": sorted(returned),
            "source_list_urls": {identity: sources[identity] for identity in filled}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--url", default=URL, help="A coches.net result page to inspect.")
    parser.add_argument("--close-tab", action="store_true")
    args = parser.parse_args(argv)
    output = args.output or ROOT / "artifacts" / ("supervised-coches-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    output.mkdir(parents=True, exist_ok=True)
    observations, trace = [], []
    browser, agent, failure, resumed = None, None, None, False
    interrupted_after_fill = False
    final = {}
    try:
        with (
            patch.object(worker_module, "choose", ScriptedPolicy()),
            patch.object(worker_module, "field_text", lambda _context: (MESSAGE, {
                "model": "simulated-text-helper", "http_attempts": 0, "latency_ms": 0, "usage": {},
            })),
            patch.object(model, "post_json", side_effect=AssertionError("Paid model calls are forbidden")),
            patch.object(model.CLIENT, "post", side_effect=AssertionError("Paid model calls are forbidden")),
            patch.object(SupervisorPolicy, "invoke", side_effect=AssertionError("Paid supervisor calls are forbidden")),
        ):
            browser = GuardedBrowser(args.url, observations=observations)
            agent = SupervisedAgent(
                args.url, GOAL, supervisor=ScriptedSupervisor(), browser=browser, state_dir=output / "state",
                limits=SupervisorLimits(max_supervisor_calls=40, max_seconds=300),
            )
            stream = agent.run()
            for final in stream:
                trace.append(final)
                if outcome(observations)["filled_advertisements"]:
                    interrupted_after_fill = True
                    stream.close()
                    break
            if interrupted_after_fill:
                saved_run = final.get("run_id", agent.run_id)
                original_target = browser.target
                agent.disconnect()
                # Reattach the original target and inject the guard after a real session release.
                reattached = Browser.attach(original_target)
                browser.session = reattached.session
                browser.after_input = None
                reattached.session = None
                agent = SupervisedAgent.resume(
                    saved_run, supervisor=ScriptedSupervisor(), browser=browser, state_dir=output / "state",
                )
                resumed = agent.browser is browser and browser.target == original_target
                for final in agent.run():
                    trace.append(final)
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
    finally:
        if agent is not None:
            if args.close_tab:
                agent.close()
            else:
                agent.disconnect()
        elif browser is not None:
            if args.close_tab:
                browser.close()
            else:
                browser.disconnect()
    result = outcome(observations)
    report = {
        "goal": GOAL, "url": args.url, "agent_status": final.get("status", "error"),
        "run_id": final.get("run_id"), "outcome": result, "resumed_same_tab": resumed,
        "interrupted_after_verified_fill": interrupted_after_fill,
        "model_http_calls": 0, "models": "simulated", "error": failure,
        "allowed_actions": browser.allowed_actions if browser else [],
        "denied_actions": browser.denied_actions if browser else [],
        "metrics": final.get("metrics", {}), "observations": len(observations),
    }
    for name, data in (("report", report), ("observations", observations), ("trace", trace)):
        (output / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"report": str(output / "report.json"), "outcome": result["status"],
                      "resumed_same_tab": resumed, "error": failure}, ensure_ascii=False))
    return 0 if not failure and resumed and result["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
