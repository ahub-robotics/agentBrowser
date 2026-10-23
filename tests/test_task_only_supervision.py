"""Task review never becomes a per-interaction gate for Jev.

The offline lifecycle scenarios reproduce the e2eb trace: a stale Accept proposal
survived later successful input, then repeated_state discarded delegated context.
They do not replay or depend on that local trace, a real website or paid models.
"""

import json
from copy import deepcopy
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_agent import decision
from test_coordinator_recovery import V2Supervisor

from agentrun.supervised import SupervisedAgent
from AgentBrowser import agent as worker_module
from AgentBrowser import model
from AgentBrowser.browser import StalePage, fingerprint
from AgentBrowser.observability import TraceConfig

GOAL = "Select the available option, accept the filter panel, and verify the results."
CRITERION = "The selected option is applied to the results."
CONTEXT = {
    "success_criteria": [CRITERION],
    "active_item": {"item_id": "item-2", "url": "https://fixture.test/item/2"},
    "completed_items": ["https://fixture.test/item/1"],
}


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("This regression must not call an external provider"))
    monkeypatch.setattr(model.CLIENT, "post", forbidden)
    monkeypatch.setattr(worker_module, "field_text", forbidden)


class PanelBrowser:
    """A local fixture with observed selection and confirmation controls."""

    def __init__(self, *, stale=None, constant=False):
        self.target = "task-only-" + uuid4().hex
        self.selected = False
        self.applied = False
        self.version = 0
        self.stale = stale
        self.stale_raised = False
        self.fail_observation = False
        self.constant = constant
        self.inputs = []
        self.before_input = None

    def observe(self, **_):
        if self.fail_observation:
            self.fail_observation = False
            raise StalePage("The successful input's first observation was interrupted")
        context = {"id": f"panel-{self.version}", "kind": "region", "heading": "Filter options"}
        page = {
            "url": "https://fixture.test/item/2", "title": "Filter fixture",
            "scroll": {"y": 0},
            "text": "Selected option applied to results" if self.applied else "Choose an option and accept",
            "actions": [
                {"id": f"option-{self.version}", "kind": "click", "label": "Option", "node": 10 + self.version,
                 "role": "checkbox", "checked": "true" if self.selected else "false", "context": context},
                {"id": f"accept-{self.version}", "kind": "click", "label": "Accept", "node": 40 + self.version,
                 "role": "button", "submits_form": False, "context": context},
                {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "direction": "down", "dy": 1},
                {"id": "wait", "kind": "wait", "label": "Wait"},
            ],
        }
        page["fingerprint"] = fingerprint(page)
        return deepcopy(page)

    def fresh(self, _page):
        return True

    def act(self, action, _page, text=None):
        if self.before_input:
            self.before_input(action)
        if action["label"] == "Accept" and self.stale == "before" and not self.stale_raised:
            self.stale_raised = True
            self.version += 1
            raise StalePage("The observed Accept node was replaced before input")
        self.inputs.append(action["label"])
        self.version += 1
        if self.constant:
            return
        if action["label"] == "Option":
            self.selected = True
        elif action["label"] == "Accept":
            self.applied = True
            if self.stale == "after" and not self.stale_raised:
                self.stale_raised = True
                self.fail_observation = True

    def disconnect(self):
        pass


def choose_label(page, label):
    if label == "DONE":
        return {**decision("DONE"), "operation": "DONE", "target": None}
    if label == "WAIT":
        return {**decision("wait"), "operation": "WAIT", "target": None}
    if label == "SCROLL_DOWN":
        return {**decision("scroll_down"), "operation": "SCROLL_DOWN", "target": None}
    targets = model.action_space(page["actions"])[1]["CLICK"]
    target, action = next((target, action) for target, action in targets.items() if action["label"] == label)
    return {**decision(action["id"]), "operation": "CLICK", "target": target}


def task_contract():
    return {
        "version": 2,
        "requirements": [{"id": "result", "goal_quote": GOAL, "description": CRITERION,
                          "status": "unknown", "evidence_refs": []}],
        "tasks": [{"id": "configure", "instruction": GOAL, "success_criteria": [CRITERION],
                   "requirement_ids": ["result"], "depends_on": [], "goal_quote": GOAL}],
        "loops": [], "execution_constraints": [],
    }


@pytest.mark.parametrize("accept_task_result", [False, True])
def test_accept_is_jev_interaction_and_done_waits_for_task_review(tmp_path, monkeypatch, accept_task_result):
    browser = PanelBrowser()
    chosen = iter(["Option", "Accept", "DONE"])
    monkeypatch.setattr(worker_module, "choose", lambda page, *_: choose_label(page, next(chosen)))
    reviews = []

    def review(payload):
        if not payload.get("last_result"):
            return {"next_action": "continue"}
        reviews.append(deepcopy(payload))
        assert browser.inputs == ["Option", "Accept"]
        assert payload["last_result"]["status"] == "claimed_done"
        assert payload["requirements"][0]["status"] == "unknown"
        if not accept_task_result:
            return {"next_action": "stop", "stop_reason": "Task evidence was not accepted"}
        refs = [fact["ref"] for fact in payload["evidence"]
                if fact.get("kind") == "page" and fact.get("text") == "Selected option applied to results"]
        assert refs
        return {"next_action": "continue", "requirement_updates": [
            {"id": "result", "status": "satisfied", "evidence_refs": refs},
        ], "subtask_review": {"delegation_id": payload["last_result"]["delegation_id"],
                               "status": "satisfied", "evidence_refs": refs}}

    supervisor = V2Supervisor(review, plan=task_contract())

    def before_input(_action):
        # Initialization, audit and initial scheduling precede the entire task.
        # There is no additional supervisor permission call before Accept.
        assert [payload["phase"] for payload in supervisor.payloads] == ["initialize", "audit", "review"]

    browser.before_input = before_input
    with SupervisedAgent(browser.observe()["url"], GOAL, browser=browser, supervisor=supervisor,
                         state_dir=tmp_path / "state", trace=TraceConfig(directory=tmp_path / "traces")) as agent:
        assert agent.worker.verification_enabled is False
        assert agent.worker.verifier is None
        final = list(agent.run())[-1]
        assert agent.worker.state["verification_calls"] == 0
        assert final["status"] == ("done" if accept_task_result else "unverified")
        assert final["requirements"][0]["status"] == ("satisfied" if accept_task_result else "unknown")
        events = [json.loads(line) for path in (agent.trace.directory / "events").glob("*.jsonl")
                  for line in path.read_text().splitlines()]
    started = next(event["seq"] for event in events if event["event"] == "delegation.started")
    completed = next(event["seq"] for event in events if event["event"] == "delegation.completed")
    assert not any(event["component"] == "supervisor" and event["event"] == "call.started"
                   and started < event["seq"] < completed for event in events)
    assert next(event for event in events if event["event"] == "delegation.completed")["status"] == "claimed_done"
    assert reviews and browser.inputs == ["Option", "Accept"]


@pytest.mark.parametrize("stale", ["before", "after"])
def test_successful_recovery_retires_only_transient_feedback_and_keeps_task_context(monkeypatch, stale):
    browser = PanelBrowser(stale=stale)
    browser.selected = True
    payloads = []
    choices = iter(["Accept", "Accept" if stale == "before" else "WAIT", "DONE"])

    def choose(page, *_):
        payloads.append(deepcopy(page["_candidate_request"]["state"]["progress_feedback"]))
        return choose_label(page, next(choices))

    monkeypatch.setattr(worker_module, "choose", choose)
    with worker_module.Agent(browser.observe()["url"], GOAL, browser=browser,
                             verification=False, trace=False) as worker:
        worker.start_subtask(GOAL, progress_feedback=CONTEXT)
        recovered = worker.command("tick")["progress_feedback"]
        assert recovered["reason"] == "stale_observation"
        assert recovered["recovery"]["action_was_executed"] is (stale == "after")
        assert {key: recovered[key] for key in CONTEXT} == CONTEXT
        worker.command("tick")
        worker.command("tick")
        assert payloads[1]["reason"] == "stale_observation"
        assert "recovery" not in payloads[2] and payloads[2].get("reason") != "stale_observation"
        assert {key: payloads[2][key] for key in CONTEXT} == CONTEXT
        assert sum(entry["action"] == "Accept" for entry in worker.state["history"]) == 1
    assert browser.inputs.count("Accept") == 1


@pytest.mark.parametrize("unrelated", ["WAIT", "SCROLL_DOWN"])
def test_unrelated_input_does_not_erase_a_pending_unexecuted_accept(monkeypatch, unrelated):
    browser = PanelBrowser(stale="before")
    chosen = iter(["Accept", unrelated, "Accept", "DONE"])
    payloads = []

    def choose(page, *_):
        payloads.append(deepcopy(page["_candidate_request"]["state"]["progress_feedback"]))
        return choose_label(page, next(chosen))

    monkeypatch.setattr(worker_module, "choose", choose)
    with worker_module.Agent(browser.observe()["url"], GOAL, browser=browser,
                             verification=False, trace=False) as worker:
        worker.start_subtask(GOAL, progress_feedback=CONTEXT)
        for _ in range(4):
            worker.command("tick")
        assert payloads[2]["reason"] == "stale_observation"
        assert payloads[2]["recovery"]["action_was_executed"] is False
        assert "recovery" not in payloads[3]
        assert {key: payloads[3][key] for key in CONTEXT} == CONTEXT
    assert browser.inputs == ["Wait" if unrelated == "WAIT" else "Scroll down", "Accept"]


def test_repeated_state_preserves_criteria_item_and_completed_items_for_next_jev_request(monkeypatch):
    browser = PanelBrowser(constant=True)
    payloads = []
    observed_accepts = []
    choices = iter(["Accept", "Accept", "Accept", "DONE"])

    def choose(page, *_):
        payloads.append(deepcopy(page["_candidate_request"]["state"]["progress_feedback"]))
        observed_accepts.append(deepcopy(next(action for action in page["actions"] if action["label"] == "Accept")))
        return choose_label(page, next(choices))

    monkeypatch.setattr(worker_module, "choose", choose)
    with worker_module.Agent(browser.observe()["url"], GOAL, browser=browser,
                             verification=False, trace=False) as worker:
        worker.start_subtask(GOAL, progress_feedback=CONTEXT)
        for _ in range(3):
            worker.command("tick")
        assert worker.state["progress_feedback"]["reason"] == "repeated_state"
        worker.command("tick")
        assert payloads[-1]["reason"] == "repeated_state"
        assert {key: payloads[-1][key] for key in CONTEXT} == CONTEXT
        assert worker.state["verification_calls"] == 0
    # Every input remounts the controls and their context; the task has made no
    # semantic progress even though both real DOM fingerprints and IDs change.
    assert len({action["node"] for action in observed_accepts}) == 4
    assert len({action["id"] for action in observed_accepts}) == 4
    assert len({action["context"]["id"] for action in observed_accepts}) == 4


def test_accept_in_a_different_semantic_panel_does_not_retire_the_original_recovery(monkeypatch):
    class OtherPanelBrowser(PanelBrowser):
        def observe(self, **options):
            observed = super().observe(**options)
            if self.stale_raised:
                for action in observed["actions"]:
                    if "context" in action:
                        action["context"]["heading"] = "Other filter options"
                observed["fingerprint"] = fingerprint(observed)
            return observed

    browser = OtherPanelBrowser(stale="before")
    payloads = []
    chosen = iter(["Accept", "Accept", "DONE"])

    def choose(page, *_):
        payloads.append(deepcopy(page["_candidate_request"]["state"]["progress_feedback"]))
        return choose_label(page, next(chosen))

    monkeypatch.setattr(worker_module, "choose", choose)
    with worker_module.Agent(browser.observe()["url"], GOAL, browser=browser,
                             verification=False, trace=False) as worker:
        worker.start_subtask(GOAL, progress_feedback=CONTEXT)
        original_url = worker.state["page"]["url"]
        for _ in range(3):
            worker.command("tick")
        assert worker.state["page"]["url"] == original_url
        assert payloads[-1]["reason"] == "stale_observation"
        assert payloads[-1]["recovery"]["action_was_executed"] is False
        assert payloads[-1]["recovery"]["proposed_action"]["context"]["heading"] == "Filter options"
        assert {key: payloads[-1][key] for key in CONTEXT} == CONTEXT
    assert browser.inputs == ["Accept"]


def test_select_recovery_matches_observed_value_after_option_and_node_remount(monkeypatch):
    class SelectBrowser(PanelBrowser):
        selection = ""

        def observe(self, **options):
            observed = super().observe(**options)
            observed["actions"].append({
                "id": f"select-{self.version}", "kind": "select", "node": 70 + self.version,
                "option": 90 + self.version, "label": "Category → Preferred", "value": "preferred",
                "current_value": self.selection, "role": "combobox",
                "context": {"id": f"selector-{self.version}", "kind": "region", "heading": "Category"},
            })
            observed["fingerprint"] = fingerprint(observed)
            return observed

        def act(self, action, page, text=None):
            if action["kind"] == "select" and not self.stale_raised:
                self.stale_raised = True
                self.version += 1
                raise StalePage("The selector and its option were replaced before input")
            super().act(action, page, text=text)
            if action["kind"] == "select":
                self.selection = action["value"]

    browser = SelectBrowser()
    payloads, selected = [], []

    def choose(page, *_):
        payloads.append(deepcopy(page["_candidate_request"]["state"]["progress_feedback"]))
        if len(payloads) == 3:
            return choose_label(page, "DONE")
        target, action = next(iter(model.action_space(page["actions"])[1]["SELECT"].items()))
        selected.append(deepcopy(action))
        return {**decision(action["id"]), "operation": "SELECT", "target": target}

    monkeypatch.setattr(worker_module, "choose", choose)
    with worker_module.Agent(browser.observe()["url"], GOAL, browser=browser,
                             verification=False, trace=False) as worker:
        worker.start_subtask(GOAL, progress_feedback=CONTEXT)
        for _ in range(3):
            worker.command("tick")
        assert len(worker.state["history"]) == 1
    assert selected[0]["option"] != selected[1]["option"]
    assert selected[0]["node"] != selected[1]["node"]
    assert selected[0]["value"] == selected[1]["value"] == browser.selection == "preferred"
    assert selected[0]["label"] == selected[1]["label"]
    assert payloads[1]["reason"] == "stale_observation"
    assert "recovery" not in payloads[-1] and payloads[-1].get("reason") != "stale_observation"
    assert {key: payloads[-1][key] for key in CONTEXT} == CONTEXT
    assert browser.inputs == ["Category → Preferred"]


def test_same_url_document_reload_retires_recovery_after_an_unrelated_observed_input(monkeypatch):
    class ReloadBrowser(PanelBrowser):
        epoch = 1000

        def observe(self, **options):
            observed = super().observe(**options)
            observed["page_key"] = [self.epoch, observed["url"]]
            return observed

        def act(self, action, page, text=None):
            super().act(action, page, text=text)
            if action["kind"] == "wait":
                self.epoch += 1

    browser = ReloadBrowser(stale="before")
    choices = iter(["Accept", "WAIT", "DONE"])
    payloads = []

    def choose(page, *_):
        payloads.append(deepcopy(page["_candidate_request"]["state"]["progress_feedback"]))
        return choose_label(page, next(choices))

    monkeypatch.setattr(worker_module, "choose", choose)
    with worker_module.Agent(browser.observe()["url"], GOAL, browser=browser,
                             verification=False, trace=False) as worker:
        worker.start_subtask(GOAL, progress_feedback=CONTEXT)
        original = deepcopy(worker.state["page"])
        for _ in range(3):
            worker.command("tick")
        assert worker.state["page"]["url"] == original["url"]
        assert worker.state["page"]["page_key"][0] != original["page_key"][0]
    assert payloads[1]["reason"] == "stale_observation"
    assert "recovery" not in payloads[-1] and payloads[-1].get("reason") != "stale_observation"
    assert {key: payloads[-1][key] for key in CONTEXT} == CONTEXT
    assert browser.inputs == ["Wait"]


def test_observed_semantic_progress_clears_repeated_state_without_dropping_task_context(monkeypatch):
    browser = PanelBrowser(constant=True)
    choices = iter(["Accept", "Accept", "Accept", "Option", "DONE"])
    payloads = []

    def choose(page, *_):
        payloads.append(deepcopy(page["_candidate_request"]["state"]["progress_feedback"]))
        return choose_label(page, next(choices))

    monkeypatch.setattr(worker_module, "choose", choose)
    with worker_module.Agent(browser.observe()["url"], GOAL, browser=browser,
                             verification=False, trace=False) as worker:
        worker.start_subtask(GOAL, progress_feedback=CONTEXT)
        for _ in range(3):
            worker.command("tick")
        assert worker.state["progress_feedback"]["reason"] == "repeated_state"
        browser.constant = False
        worker.command("tick")  # Selecting the unchecked option produces a genuinely new observation.
        assert browser.selected is True
        worker.command("tick")
    assert payloads[3]["reason"] == "repeated_state"
    assert payloads[-1].get("reason") != "repeated_state"
    assert {key: payloads[-1][key] for key in CONTEXT} == CONTEXT
