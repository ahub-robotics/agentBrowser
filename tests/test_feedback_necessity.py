"""User input is reserved for blocked outcomes, not routine subtask boundaries."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import decision, page
from test_supervisor_corrections import GOAL as SEARCH_GOAL
from test_supervisor_corrections import SearchBrowser, response, text_ref

from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from AgentBrowser import agent as jev
from AgentBrowser.browser import fingerprint


@pytest.mark.parametrize("invalid", ["unknown_requirement", "already_satisfied", "empty_reason", "no_blocked_ids"])
def test_unnecessary_or_unbound_feedback_ends_unverified_after_one_repair_without_asking_user(tmp_path, invalid):
    browser = SearchBrowser()
    calls = []

    def policy(payload):
        calls.append(deepcopy(payload))
        answer = response(
            feedback_reason="missing_user_information", missing_information="The requested book's title",
            blocked_requirement_ids=["goal"], user_intervention="Only the user can identify the requested book.",
        )
        if invalid == "unknown_requirement":
            answer["blocked_requirement_ids"] = ["invented-requirement"]
        elif invalid == "already_satisfied":
            answer["requirements"][0].update(status="satisfied", evidence_refs=[text_ref(payload)])
        elif invalid == "empty_reason":
            answer["user_intervention"] = ""
        else:
            answer["blocked_requirement_ids"] = []
        return answer

    with SupervisedAgent(page()["url"], SEARCH_GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "unverified"
        assert final["stop_reason"] == "supervisor_invalid_response"
        assert final["question"] == ""
        assert len(calls) == final["metrics"]["supervisor_calls"] == 2
        assert calls[1]["validation_error"]["browser_action_executed"] is False
        assert final["metrics"]["jev_calls"] == final["metrics"]["actions"] == 0
        assert list(agent.run())[-1]["status"] == "unverified"
        with pytest.raises(ValueError):
            agent.submit_feedback("Do not ask me to fix an invalid model response")
        assert len(calls) == 2
    assert browser.inputs == []


class LibraryBrowser:
    target = "library-feedback-necessity"

    def __init__(self):
        self.stage = 0
        self.value = ""
        self.inputs = []
        self.disconnect = Mock()
        self.close = Mock()

    def observe(self, screenshot=False):
        labels = ["Open library", "Inspect results", "Book title", "Book title"]
        observed = {
            "url": f"https://library.test/stage/{self.stage}", "title": "Library",
            "text": ["Library entry", "Library open", "Results inspected; book title missing", "Book title recorded"][
                self.stage
            ],
            "scroll": {"y": 0}, "actions": [{
                "id": "fill-title" if self.stage >= 2 else f"step-{self.stage}",
                "node": self.stage + 1, "kind": "fill" if self.stage >= 2 else "click",
                "role": "textbox" if self.stage >= 2 else "button", "label": labels[self.stage], "value": self.value,
            }],
        }
        observed["fingerprint"] = fingerprint(observed)
        return observed

    def fresh(self, observed, action=None):
        return observed["fingerprint"] == self.observe()["fingerprint"]

    def act(self, action, observed, text=None):
        self.inputs.append(action["id"])
        if action["kind"] == "fill":
            self.value = text
            self.stage = 3
        else:
            self.stage += 1


def test_several_subtasks_continue_automatically_and_only_missing_user_value_pauses(tmp_path, monkeypatch):
    goal = "Open the library. Inspect the results. Fill the requested book title."
    browser = LibraryBrowser()
    requirements = [
        {"id": key, "goal_quote": quote, "description": quote, "status": "unknown", "evidence_refs": []}
        for key, quote in [("library", "Open the library"), ("results", "Inspect the results"),
                           ("title", "Fill the requested book title")]
    ]
    decisions = []
    payloads = []

    def delegate(instruction, criteria, requirements):
        return {"action": "delegate", "requirements": requirements, "task": {
            "id": "current", "instruction": instruction, "success_criteria": [criteria],
        }}

    def policy(payload):
        payloads.append(deepcopy(payload))
        current = deepcopy(payload["requirements"] or requirements)
        if browser.stage == 0:
            result = delegate("Open the library", "The library is visibly open", current)
        elif browser.stage == 1:
            current[0].update(status="satisfied", evidence_refs=[text_ref(payload)])
            result = delegate("Inspect the results", "The results are visibly inspected", current)
        elif browser.stage == 2:
            current[1].update(status="satisfied", evidence_refs=[text_ref(payload)])
            if not payload["feedback"]:
                result = {
                    "action": "feedback", "requirements": current,
                    "feedback_reason": "missing_user_information", "missing_information": "The book title",
                    "blocked_requirement_ids": ["title"],
                    "user_intervention": "The goal does not identify a book; only the user can supply its title.",
                    "question": "Which book title should I enter?",
                }
            else:
                result = delegate("Fill the requested book title with Dune", "The title field contains Dune", current)
        else:
            ref = next(fact["ref"] for fact in payload["evidence"]
                       if fact.get("kind") == "control" and fact.get("value") == "Dune")
            current[2].update(status="satisfied", evidence_refs=[ref])
            result = {"action": "finish", "requirements": current, "evidence_refs": [ref]}
        decisions.append(result["action"])
        return result

    def choose(observed, instruction, history):
        if "CURRENT SUBTASK:\nOpen the library" in instruction:
            return decision("step-0" if browser.stage == 0 else "DONE")
        if "CURRENT SUBTASK:\nInspect the results" in instruction:
            return decision("step-1" if browser.stage == 1 else "DONE")
        return decision("fill-title" if not browser.value else "DONE")

    monkeypatch.setattr(jev, "choose", choose)
    text_model = Mock(return_value=("Dune", {"model": "offline", "latency_ms": 0}))
    monkeypatch.setattr(jev, "field_text", text_model)
    with SupervisedAgent("https://library.test/stage/0", goal, state_dir=tmp_path,
                         browser=browser, supervisor=policy) as agent:
        states = list(agent.run())
        paused = states[-1]
        assert decisions == ["delegate", "delegate", "feedback"]
        assert [state["status"] for state in states].count("waiting_feedback") == 1
        assert paused["question"] == "Which book title should I enter?"
        assert paused["requirements"][0]["status"] == paused["requirements"][1]["status"] == "satisfied"
        assert paused["requirements"][2]["status"] == "unknown"
        assert browser.inputs == ["step-0", "step-1"]
        text_model.assert_not_called()

        agent.submit_feedback("Dune")
        final = list(agent.run())[-1]
        assert final["status"] == "done" and final["question"] == ""
        assert decisions == ["delegate", "delegate", "feedback", "delegate", "finish"]
        assert payloads[-2]["feedback"] == ["Dune"]
        assert browser.inputs == ["step-0", "step-1", "fill-title"]
        assert final["metrics"]["actions"] == 3
        assert browser.value == "Dune"
        text_model.assert_called_once()
    browser.close.assert_not_called()
