"""Explicit console feedback check on an existing tab, with no model calls or browser inputs."""

import argparse
import builtins
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentrun.console import run_console  # noqa: E402
from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent  # noqa: E402
from agentrun.supervisor_policy import SupervisorPolicy  # noqa: E402
from AgentBrowser import agent as worker_module  # noqa: E402
from AgentBrowser import model  # noqa: E402
from AgentBrowser.browser import Browser  # noqa: E402
from scripts.evaluate_coches import READ_DOM, filter_outcomes  # noqa: E402

GOAL = "Ask for the expected page title and verify it against the current page without browser input."


def checkpoint_digest(path):
    """Fingerprint persisted data; SQLite's shared-memory lock file is transient."""
    return {
        suffix: hashlib.sha256(candidate.read_bytes()).hexdigest()
        for suffix in ("", "-wal") if (candidate := Path(str(path) + suffix)).exists()
    }


def source_target(run_id, state_dir):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid source run ID")
    path = Path(state_dir).resolve() / run_id / "state.sqlite"
    before = checkpoint_digest(path)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT state_json FROM supervisor_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown source run")
    target = json.loads(row[0]).get("target")
    if not isinstance(target, str) or not target:
        raise ValueError("Source run has no browser target")
    return target, path, before


class ReadOnlyBrowser(Browser):
    denied_inputs = 0

    def act(self, *_args, **_kwargs):
        self.denied_inputs += 1
        raise AssertionError("This diagnostic forbids all browser inputs")


class TitleSupervisor:
    def __init__(self, browser):
        self.browser = browser
        self.calls = 0
        self.last_dom = None

    def __call__(self, payload):
        self.calls += 1
        expected = payload["feedback"][-1] if payload["feedback"] else None
        self.last_dom = self.browser.evaluate(READ_DOM)
        if not isinstance(self.last_dom, dict) or not isinstance(self.last_dom.get("title"), str):
            raise ValueError("The independent DOM reader did not return a title")
        observed = self.last_dom["title"]
        refs = [fact["ref"] for fact in payload["evidence"]
                if fact.get("ref") in payload["current_evidence_refs"] and fact.get("title") == observed]
        matched = expected is not None and expected == observed and bool(refs)
        requirement = {
            "id": "title", "goal_quote": GOAL, "description": "The expected title matches the current page",
            "status": "satisfied" if matched else "unknown", "evidence_refs": refs if matched else [],
        }
        if matched:
            return {"action": "finish", "requirements": [requirement], "evidence_refs": refs}
        return {
            "action": "feedback", "requirements": [requirement],
            "feedback_reason": "missing_user_information", "missing_information": "The expected page title",
            "question": ("Escribe el título exacto que debe tener la pestaña para verificarlo."
                         if expected is None else "El título no coincide. Escribe el título que debo comprobar."),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--target", help="An existing Chrome page target ID; no tab will be created.")
    source.add_argument("--run-id", help="Read only the target from this saved run; never resume its actions.")
    parser.add_argument("--source-state-dir", type=Path, default=ROOT / "artifacts" / "supervised")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-input", action="store_true", help="Stop at the diagnostic feedback pause.")
    args = parser.parse_args(argv)
    output = args.output or ROOT / "artifacts" / ("supervised-feedback-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    output.mkdir(parents=True, exist_ok=True)
    # Keep an interrupted diagnostic resumable, but never share the original store.
    diagnostic_state_dir = Path(tempfile.mkdtemp(prefix="state-", dir=output))
    target, source_path, original_digest = args.target, None, None
    browser = agent = supervisor = None
    waits, forbidden, error_type = [], [], None
    result = None
    final = {}
    started = time.perf_counter()
    try:
        if args.run_id:
            target, source_path, original_digest = source_target(args.run_id, args.source_state_dir)
        with ExitStack() as stack:
            for owner, name in (
                (worker_module, "choose"), (worker_module, "field_text"), (model, "post_json"),
                (model.CLIENT, "post"), (SupervisorPolicy, "invoke"),
            ):
                forbidden.append(stack.enter_context(patch.object(
                    owner, name, side_effect=AssertionError("Paid models and worker decisions are forbidden"),
                )))
            browser = ReadOnlyBrowser.attach(target)
            initial_dom = browser.evaluate(READ_DOM)
            if not isinstance(initial_dom, dict) or not initial_dom.get("url"):
                raise ValueError("The independent DOM reader did not return a URL")
            supervisor = TitleSupervisor(browser)
            agent = SupervisedAgent(initial_dom["url"], GOAL, browser=browser, supervisor=supervisor,
                                    state_dir=diagnostic_state_dir)
            original_input = builtins.input

            def measured_input(prompt):
                before = agent.snapshot()
                waiting_started = time.perf_counter()
                try:
                    return original_input(prompt)
                finally:
                    after = agent.snapshot()
                    waits.append({
                        "wall_ms": round((time.perf_counter() - waiting_started) * 1000),
                        "active_elapsed_change_ms": after["elapsed_ms"] - before["elapsed_ms"],
                        "same_run": before["run_id"] == after["run_id"] == agent.run_id,
                        "same_tab": agent.browser is browser and browser.target == target,
                    })

            stack.enter_context(patch.object(builtins, "input", measured_input))
            with agent:
                result = run_console(agent, no_input=args.no_input, state_dir=diagnostic_state_dir)
                final = result.state or {}
                supervisor.last_dom = browser.evaluate(READ_DOM)
    except Exception as error:
        error_type = type(error).__name__
    finally:
        if agent is not None:
            if not agent._closed:
                final = agent.snapshot()
                agent.disconnect()
        elif browser is not None:
            browser.disconnect()
    dom = supervisor.last_dom if supervisor is not None else {}
    filters = filter_outcomes(dom or {}, [])
    report = {
        "status": final.get("status", "error"), "source_run_id": args.run_id,
        "diagnostic_run_id": final.get("run_id"), "diagnostic_state_dir": str(diagnostic_state_dir),
        "same_existing_tab": browser is not None and browser.target == target,
        "same_run_through_feedback": bool(waits) and all(wait["same_run"] for wait in waits),
        "same_tab_through_feedback": bool(waits) and all(wait["same_tab"] for wait in waits),
        "feedback_received": len(agent.state["feedback"]) if agent is not None else 0,
        "input_waits": waits, "waiting_excluded_from_active_time": bool(waits) and all(
            wait["active_elapsed_change_ms"] == 0 for wait in waits
        ),
        "wall_ms": round((time.perf_counter() - started) * 1000), "active_ms": final.get("elapsed_ms"),
        "model_calls": 0, "blocked_model_attempts": sum(mock.call_count for mock in forbidden),
        "browser_inputs": 0, "blocked_browser_inputs": browser.denied_inputs if browser is not None else 0,
        "simulated_supervisor_calls": supervisor.calls if supervisor is not None else 0,
        "source_checkpoint_unchanged": source_path is None or checkpoint_digest(source_path) == original_digest,
        "independent_title": (dom or {}).get("title"),
        "filter_outcomes": {key: {"status": value["status"], "evidence": {
            field: evidence for field, evidence in value["evidence"].items() if field != "url"
        }} for key, value in filters.items()},
        "tab_left_open": browser is not None and browser.target == target,
        "error_type": error_type,
    }
    report["verified"] = bool(
        result is not None and result.exit_code == 0 and report["same_run_through_feedback"]
        and report["same_tab_through_feedback"] and report["waiting_excluded_from_active_time"]
        and report["source_checkpoint_unchanged"] and not report["blocked_model_attempts"]
        and not report["blocked_browser_inputs"] and error_type is None
    )
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verified"] else (result.exit_code if result is not None and result.exit_code else 2)


if __name__ == "__main__":
    raise SystemExit(main())
