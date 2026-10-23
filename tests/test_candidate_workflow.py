"""Offline CS01 integration: real request heads and graph, independently observed fake DOM."""

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from AgentBrowser import agent as loop
from AgentBrowser import model
from AgentBrowser.browser import StalePage
from AgentBrowser.candidates import DEFAULT_LIMITS


def inventory_page(kind="select", count=10):
    if kind == "select":
        nodes = [{
            "node": 1, "label": "Destination", "role": "combobox", "value": "initial",
            "operations": ["select"], "region": "form",
            "options": [{"option": index, "label": f"Option {index}", "value": f"value-{index}"}
                        for index in range(count)],
        }]
    else:
        nodes = [{
            "node": index + 1, "label": f"Result {index}", "role": "button",
            "operations": ["click"], "region": "root",
        } for index in range(count)]
    return {
        "url": "https://example.test/options", "title": "Options", "text": "Choose a destination",
        "marker": "dom-0", "fingerprint": "fingerprint-0", "actions": [],
        "inventory": {
            "version": 1, "nodes": nodes,
            "globals": [{"id": "wait", "kind": "wait", "label": "Wait"}],
            "coverage": {"scope": "test_document", "extraction_complete": True},
        },
    }


class InventoryBrowser:
    """The test checks selected/clicked state, rather than treating DONE as evidence."""

    def __init__(self, page):
        self.page = page
        self.revision = 0
        self.selected = None
        self.clicked = None
        self.inputs = []
        self.observations = 0

    def observe(self, *, screenshot=False):
        self.observations += 1
        return deepcopy(self.page)

    def fresh(self, page):
        return page["fingerprint"] == self.page["fingerprint"]

    def change(self):
        self.revision += 1
        self.page.update(marker=f"dom-{self.revision}", fingerprint=f"fingerprint-{self.revision}")

    def act(self, action, page, *, text=None):
        if not self.fresh(page):
            raise StalePage("Fake DOM changed before input")
        self.inputs.append(deepcopy(action))
        if action["kind"] == "select":
            self.selected = action["value"]
        elif action["kind"] == "click":
            self.clicked = action["node"]
        self.change()

    def close(self):
        pass


def answer(criteria, selected):
    assert selected in criteria
    return {"choice": selected, "confidence": 1, "probabilities": {
        item: float(item == selected) for item in criteria
    }}


def response(body, operation="MORE_CANDIDATES", target=None):
    questions = body["questions"]
    answers = {
        "operation": answer(questions["operation"]["criteria"], operation),
        # An unused malformed head must never affect the control path.
        "select_target": {"choice": "not-observed"},
        "click_target": {"choice": "not-observed"},
    }
    if target is not None:
        head = operation.lower() + "_target"
        answers[head] = answer(questions[head]["criteria"], target)
    return {"model": "offline-test", "answers": answers}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test")
    helper = Mock(side_effect=AssertionError("No text helper is expected in this suite"))
    monkeypatch.setattr(loop, "field_text", helper)
    calls = []

    def install_policy(policy):
        def post(_url, _key, body):
            # Match the transport's cumulative counter when a window is reused.
            body.http_attempts += 1
            calls.append(deepcopy(dict(body)))
            return policy(body)

        monkeypatch.setattr(model, "post_json", post)

    install_policy(response)

    def create(kind="select", count=10, **limits):
        browser = InventoryBrowser(inventory_page(kind, count))
        monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))
        limits = replace(DEFAULT_LIMITS, **{
            "max_nodes": 1, "max_options_per_select": 2, "max_actions": 3,
            "max_windows": 12, "max_queries": 12, **limits,
        })
        agent = loop.Agent("https://example.test/options", "Find the requested destination",
                           candidate_limits=limits, verification=False)
        return agent, browser

    return create, install_policy, calls, helper


def test_predict_preserves_window_and_act_advances_once_without_browser_or_helper(setup):
    create, _, calls, helper = setup
    agent, browser = create()
    initial = agent.snapshot()["page"]
    predicted = agent.command("predict")
    binding = agent.decision_body()
    assert predicted["status"] == "predicted"
    assert predicted["page"]["selection_digest"] == initial["selection_digest"]
    assert predicted["page"]["candidate_coverage"]["window_id"] == 1
    assert predicted["decision"]["operation"] == "MORE_CANDIDATES"
    assert predicted["decision"]["target"] is None
    assert "more_candidates_target" not in calls[0]["questions"]
    assert agent.snapshot()["candidate_controls"] == []
    result = agent.command("act", binding)
    assert result["status"] == "ready" and result["decision"] is None
    assert result["page"]["candidate_coverage"]["window_id"] == 2
    assert len(result["candidate_controls"]) == 1
    assert result["history"] == [] and browser.inputs == []
    assert result["model_calls"] == result["model_http_attempts"] == 1
    assert browser.observations == 1
    helper.assert_not_called()
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", binding)


def test_tick_advances_one_window_and_fresh_reads_do_not_rotate_candidates(setup):
    create, _, calls, _ = setup
    agent, browser = create()
    result = agent.command("tick")
    assert len(calls) == 1 and len(result["candidate_controls"]) == 1
    assert result["page"]["candidate_coverage"]["window_id"] == 2
    digest = result["page"]["selection_digest"]
    for _ in range(3):
        assert browser.fresh(agent.state["page"])
        assert agent.snapshot()["page"]["selection_digest"] == digest
    assert result["history"] == []


def test_repeated_prediction_preserves_window_but_counts_each_request_and_rebinds_decision(setup):
    create, _, calls, _ = setup
    agent, browser = create()
    first = agent.command("predict")
    old = agent.decision_body()
    second = agent.command("predict")
    assert first["page"]["selection_digest"] == second["page"]["selection_digest"]
    assert second["page"]["candidate_coverage"]["window_id"] == 1
    assert second["decision"]["decision_id"] != old["decision_id"]
    assert second["model_calls"] == second["model_http_attempts"] == len(calls) == 2
    assert second["candidate_windows"] == 1
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", old)
    assert browser.inputs == [] and second["candidate_controls"] == []


@pytest.mark.parametrize("kind,count", [("select", 10), ("click", 5)])
def test_run_reaches_late_candidate_after_four_reconsultations_and_verifies_effect(setup, kind, count):
    create, install_policy, calls, helper = setup
    agent, browser = create(kind, count)

    def seek(body):
        if browser.selected == "value-9" or browser.clicked == 5:
            return response(body, "DONE")
        for element in body["state"]["elements"]:
            if kind == "click" and element["label"] == "Result 4":
                return response(body, "CLICK", element["index"])
            for option in element.get("options", []):
                if option["value"] == "value-9":
                    return response(body, "SELECT", option["index"])
        return response(body)

    install_policy(seek)
    states = list(agent.run())
    assert [state["status"] for state in states] == ["ready"] * 5 + ["done"]
    assert len(calls) == 6 and states[-1]["model_calls"] == 6
    assert states[-1]["model_http_attempts"] == 6
    assert len(states[-1]["candidate_controls"]) == 4
    assert len(states[-1]["history"]) == len(browser.inputs) == 1
    assert browser.selected == "value-9" if kind == "select" else browser.clicked == 5
    helper.assert_not_called()
    assert all(state["stop_reason"] is None for state in states)


def test_done_alone_cannot_satisfy_independent_fake_dom_verification(setup):
    create, install_policy, _, _ = setup
    agent, browser = create()
    install_policy(lambda body: response(body, "DONE"))
    assert list(agent.run())[-1]["status"] == "done"
    assert browser.selected is None and browser.inputs == []


def test_old_window_binding_and_fingerprint_only_cannot_consume_new_decision(setup):
    create, _, _, _ = setup
    agent, browser = create()
    agent.command("predict")
    old = agent.decision_body()
    agent.command("act", old)
    agent.command("predict")
    current = agent.decision_body()
    assert old["fingerprint"] == current["fingerprint"]
    assert old["window_id"] != current["window_id"]
    for invalid in (old, {"fingerprint": current["fingerprint"]}):
        with pytest.raises(ValueError, match="Observe and choose"):
            agent.command("act", invalid)
        assert agent.decision_body() == current
    agent.command("act", current)
    assert agent.snapshot()["page"]["candidate_coverage"]["window_id"] == 3
    assert browser.inputs == []


def test_stale_control_recovers_inventory_without_resetting_global_budgets(setup):
    create, install_policy, calls, _ = setup
    agent, browser = create(max_queries=3, max_windows=3)
    agent.command("tick")
    started = agent.state["started_at"]
    previous_inventory = agent.state["page"]["candidate_coverage"]["inventory_id"]

    def change_before_advance(body):
        result = response(body)
        browser.change()
        return result

    install_policy(change_before_advance)
    recovered = agent.command("tick")
    assert recovered["status"] == "ready" and recovered["decision"] is None
    assert recovered["page"]["candidate_coverage"]["inventory_id"] != previous_inventory
    assert recovered["page"]["candidate_coverage"]["window_id"] == 1
    assert len(recovered["candidate_controls"]) == 1
    assert recovered["model_calls"] == 2 and agent.state["started_at"] == started
    assert browser.inputs == [] and recovered["history"] == []
    install_policy(lambda body: response(body, "BLOCKED"))
    last = agent.command("tick")
    assert last["page"]["candidate_coverage"]["more_available"] is False
    assert last["page"]["candidate_coverage"]["termination_reason"] == "window_limit"
    assert len(calls) == 3 and agent.state["model_calls"] == 3
    assert last["candidate_windows"] == 3 and last["status"] == "blocked"
    assert "MORE_CANDIDATES" not in calls[-1]["questions"]["operation"]["criteria"]


def test_window_limit_removes_reconsultation_and_exposes_reason_without_false_completion(setup):
    create, install_policy, calls, _ = setup
    agent, browser = create(max_windows=2)

    def missing(body):
        allowed = body["questions"]["operation"]["criteria"]
        return response(body, "MORE_CANDIDATES" if "MORE_CANDIDATES" in allowed else "BLOCKED")

    install_policy(missing)
    final = list(agent.run())[-1]
    assert len(calls) == 2 and final["status"] == "blocked"
    assert final["page"]["candidate_coverage"]["termination_reason"] == "window_limit"
    assert final["page"]["candidate_coverage"]["pending_known"] > 0
    assert "MORE_CANDIDATES" not in calls[-1]["questions"]["operation"]["criteria"]
    assert browser.inputs == []


def test_failed_provider_attempts_consume_query_budget_without_retrying_or_acting(setup):
    create, install_policy, calls, _ = setup
    agent, browser = create(max_queries=2)

    def unavailable(_body):
        raise RuntimeError("Simulated provider failure")

    install_policy(unavailable)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Simulated provider failure"):
            agent.command("tick")
    with pytest.raises(ValueError, match="model-call budget"):
        agent.command("tick")
    assert len(calls) == agent.state["model_calls"] == agent.state["model_http_attempts"] == 2
    assert agent.state["decision"] is None and agent.state["decisions"] == []
    assert agent.state["candidate_controls"] == [] and browser.inputs == []
    assert agent.state["stop_reason"] == "decision_budget"


def test_paginated_legacy_action_adapter_also_requires_window_binding(setup):
    create, _, _, _ = setup
    agent, browser = create("click", 3)
    inventory = browser.page.pop("inventory")
    browser.page["actions"] = [
        {"id": f"legacy-{node['node']}", "node": node["node"], "kind": "click",
         "label": node["label"], "role": "button"}
        for node in inventory["nodes"]
    ] + inventory["globals"]
    agent._install_page(deepcopy(browser.page))
    first = agent.command("predict")
    old = agent.decision_body()
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", {"fingerprint": first["page"]["fingerprint"]})
    agent.command("act", old)
    agent.command("predict")
    current = agent.decision_body()
    assert old["fingerprint"] == current["fingerprint"]
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", old)
    assert agent.decision_body() == current
    agent.command("act", current)
    assert browser.inputs == []


def test_run_publishes_terminal_snapshot_when_query_budget_is_exhausted(setup):
    create, _, calls, _ = setup
    agent, browser = create(max_queries=2)
    states = list(agent.run())
    assert states[-1]["status"] == "blocked"
    assert states[-1]["stop_reason"] == "decision_budget"
    assert states[-1]["model_calls"] == states[-1]["model_http_attempts"] == len(calls) == 2
    assert states[-1]["history"] == [] and browser.inputs == []


def test_browser_actions_and_reobservation_do_not_renew_window_budget(setup):
    create, install_policy, calls, _ = setup
    agent, browser = create(max_windows=2)
    install_policy(lambda body: response(body, "WAIT"))
    states = list(agent.run())
    assert states[-1]["status"] == "blocked"
    assert states[-1]["stop_reason"] == "window_limit"
    assert states[-1]["candidate_windows"] == 2
    assert len(calls) == states[-1]["model_calls"] == 2
    assert len(browser.inputs) == len(states[-1]["history"]) == 2
    assert all(action["kind"] == "wait" for action in browser.inputs)


def test_elapsed_time_budget_consumes_pending_control_without_advancing_or_input(setup):
    create, _, calls, _ = setup
    agent, browser = create(max_seconds=1)
    agent.command("predict")
    binding = agent.decision_body()
    agent.state["started_at"] = loop.time.perf_counter() - 2
    result = agent.command("act", binding)
    assert result["status"] == "blocked"
    assert agent.state["decision"] is None
    assert agent.state["status"] == "blocked" and agent.state["stop_reason"] == "time_budget"
    assert agent.state["candidate_controls"] == [] and browser.inputs == []
    assert len(calls) == 1


def test_snapshots_are_json_serializable_and_keep_full_inventory_and_transport_private(setup):
    create, _, _, _ = setup
    agent, _ = create()
    snapshots = [agent.snapshot(), agent.command("predict"), agent.command("act", agent.decision_body())]
    for snapshot in snapshots:
        restored = json.loads(json.dumps(snapshot))
        assert "inventory" not in restored["page"]
        assert "browser" not in restored
        assert not any(key.startswith("_") for key in restored["page"])
        assert restored["page"]["candidate_coverage"]["actions_admitted"] <= 3
    assert snapshots[-1]["candidate_controls"][0]["status"] == "advanced"
