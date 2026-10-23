"""Public v2 audit, bounded provenance and observed redirect identity invariants."""

import hashlib
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import decision, page
from test_coordinator_recovery import (
    V2Supervisor,
    accepted_result,
    browser_for,
    contract,
    initialize,
    ready_agent,
)
from test_supervised_loops import LIST_URL, LoopBrowser, listing

from agentrun import SupervisedAgent
from agentrun.supervised import LegacySupervisedAgent
from AgentBrowser import agent as worker_module
from AgentBrowser import model
from AgentBrowser.browser import fingerprint


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No model endpoints"))))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text endpoint")))
    monkeypatch.setattr(worker_module, "choose", Mock(return_value={
        **decision("DONE"), "operation": "DONE", "target": None,
    }))


def test_second_payload_trim_authorizes_only_previous_and_actually_supplied_references(tmp_path, monkeypatch):
    observed = page()
    observed["actions"] = [{
        "id": f"field-{i}", "node": i, "kind": "fill", "role": "textbox", "label": f"Field {i}", "value": "x" * 2000,
    } for i in range(180)]
    observed["fingerprint"] = fingerprint(observed)
    plan = contract()
    plan["execution_constraints"] = ["Never invent a value. " + "a" * 1900 for _ in range(24)]
    policy = V2Supervisor(plan=plan)
    with ready_agent(tmp_path, supervisor=policy, browser=browser_for(observed)) as agent:
        initialize(agent)
        previous = set(agent.state["sent_refs"])
        # Inspect a new slice after the actual initialization/audit requests,
        # so omitted facts in this slice have never been supplied before.
        agent.state["evidence_offset"] = max(payload["next_evidence_offset"] or 0 for payload in policy.payloads)
        captured = []
        original = LegacySupervisedAgent._payload

        def capture_before_contract(owner):
            payload = original(owner)
            captured.append(deepcopy(payload))
            return payload

        monkeypatch.setattr(LegacySupervisedAgent, "_payload", capture_before_contract)
        payload = agent._payload()
        supplied = {fact["ref"] for fact in [*payload["evidence"], *payload["observed_links"]]}
        initially_offered = {fact["ref"] for fact in [*captured[0]["evidence"], *captured[0]["observed_links"]]}
        omitted = initially_offered - supplied - previous
        assert omitted, "The plan must force a second trim of genuinely new evidence"
        assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 120 * 1024
        assert set(agent.state["sent_refs"]) == previous | supplied
        assert not omitted & set(agent.state["sent_refs"])
        assert payload["next_evidence_offset"] == payload["evidence_offset"] + len(payload["evidence"])
        with pytest.raises(ValueError, match="not supplied"):
            agent._refs([next(iter(omitted))], required=True)
        agent.browser.act.assert_not_called()
        worker_module.choose.assert_not_called()


@pytest.mark.parametrize("offer_replacement", [False, True])
def test_rejected_audit_never_dispatches_browser_work(tmp_path, offer_replacement, capsys):
    phases = []

    def reject(payload):
        phases.append(payload["phase"])
        if payload["phase"] == "initialize":
            return {"contract": contract()}
        assert payload["phase"] == "audit"
        return {"approved": False, "problems": ["A requested outcome is still missing."],
                "replacement_contract": contract() if offer_replacement else None}

    with ready_agent(tmp_path, supervisor=reject) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "unverified" and result["stop_reason"] == "plan_audit_failed"
        assert phases == (["initialize", "audit", "audit", "audit"] if offer_replacement else
                          ["initialize", "audit", "initialize", "audit", "initialize", "audit"])
        assert agent.state["pending_delegation"] is None
        assert agent.state["delegation_sequence"] == 0
        assert agent.store.attempts() == []
        agent.browser.act.assert_not_called()
        worker_module.choose.assert_not_called()
    output = capsys.readouterr().out
    assert "Motivos del rechazo:" in output
    if offer_replacement:
        assert "Correcciones propuestas:" in output
        assert "Plan preparado:" in output
        assert "Auditoría 3/3 rechazada; se agotaron las 2 correcciones permitidas." in output


def test_supervisor_plan_and_audit_reasons_are_printed_as_bullets(tmp_path, capsys):
    def policy(payload):
        if payload["phase"] == "initialize":
            return {"contract": contract()}
        if payload["phase"] == "audit":
            return {"approved": False, "problems": ["Falta comprobar el resultado visible."],
                    "replacement_contract": None}
        raise AssertionError("No browser work should be scheduled after a rejected audit")

    with ready_agent(tmp_path, supervisor=policy) as agent:
        list(agent.run())

    output = capsys.readouterr().out
    assert "Plan preparado:" in output
    assert "• Tarea: Click the observed Go button once and verify the result." in output
    assert "Motivos del rechazo:" in output
    assert "• Falta comprobar el resultado visible." in output
    assert "Auditoría 1/3 rechazada sin plan corregido; se solicita la corrección 1/2 al planificador." in output


@pytest.mark.parametrize("problems", [
    ["No actionable defect remains."],
    ["The contract omits a requested outcome.", "No actionable issues remain."],
])
def test_prose_never_overrides_rejected_audit_boolean(tmp_path, problems):
    response = {"approved": False, "problems": [
        *problems,
    ], "replacement_contract": None}

    def policy(payload):
        if payload["phase"] == "initialize":
            return {"contract": contract()}
        if payload["phase"] == "audit":
            return response
        raise AssertionError("The next phase must be progress review")

    with ready_agent(tmp_path, supervisor=policy) as agent:
        agent._decide()
        agent._decide()
        assert agent.state["contract_phase"] == "initialize"
        assert agent.state["plan_revision"]["problems"] == problems
        assert agent.state["pending_delegation"] is None
        agent.browser.act.assert_not_called()
        worker_module.choose.assert_not_called()
        assert response["approved"] is False


def test_replacement_contract_requires_its_own_approved_audit_before_one_input(tmp_path, monkeypatch):
    observed = page()
    browser = browser_for(observed)
    replacement = contract()
    replacement["tasks"][0]["instruction"] = "Click Go once, then inspect its visible confirmation."
    events, audits = [], []

    def policy(payload):
        events.append(payload["phase"])
        if payload["phase"] == "initialize":
            return {"contract": contract()}
        if payload["phase"] == "audit":
            browser.act.assert_not_called()
            audits.append(deepcopy(payload["contract"]))
            if len(audits) == 1:
                return {"approved": False, "problems": ["Clarify the observable confirmation."],
                        "replacement_contract": replacement}
            assert payload["contract"]["tasks"][0]["instruction"] == replacement["tasks"][0]["instruction"]
            return {"approved": True}
        return accepted_result(payload)

    def act(*_, **__):
        assert events == ["initialize", "audit", "audit", "review"]
        events.append("browser_input")
        observed.update(text="Result visibly confirmed", title="Complete")
        observed["fingerprint"] = fingerprint(observed)

    browser.act.side_effect = act
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=lambda state, *_: {
        **decision("DONE" if state["title"] == "Complete" else "e3"),
        "operation": "DONE" if state["title"] == "Complete" else "CLICK", "target": None,
    }))
    with ready_agent(tmp_path, supervisor=policy, browser=browser) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "done"
        assert agent.state["audit_replacements"] == 1
        assert agent.state["contract"]["tasks"][0]["instruction"] == replacement["tasks"][0]["instruction"]
        assert len(agent.store.attempts()) == 1
    browser.act.assert_called_once()
    assert events == ["initialize", "audit", "audit", "review", "browser_input", "review"]


REDIRECT_GOAL = "Inspect each distinct result once."
ALIAS = "https://example.test/items/alias"
DESTINATION = "https://example.test/items/canonical"


def redirect_plan():
    template = {
        "id": "inspect", "instruction": "Open the observed item and inspect its visible description.",
        "success_criteria": ["The item's description is visible."], "requirement_ids": ["inspect"],
        "goal_quote": REDIRECT_GOAL, "verification_scope": "destination",
    }
    return {"version": 2, "requirements": [{
        "id": "inspect", "goal_quote": REDIRECT_GOAL, "description": "Inspect all distinct results once.",
        "status": "unknown", "evidence_refs": [],
    }], "tasks": [], "loops": [{
        "id": "results", "goal_quote": REDIRECT_GOAL, "description": "Distinct observed results",
        "requirement_ids": ["inspect"], "depends_on": [], "limit": None, "order": "observed",
        "discovery": {**template, "id": "discover", "verification_scope": "current"}, "steps": [template],
    }], "execution_constraints": []}


class RedirectBrowser(LoopBrowser):
    def act(self, action, page, text=None):
        super().act(action, page, text=text)
        if action.get("href") == ALIAS:
            self.page["url"] = DESTINATION
            self.page["fingerprint"] = fingerprint(self.page)


def redirect_review(payload):
    last = payload.get("last_result")
    if last is None:
        return {"next_action": "continue", "discovered": [{
            "loop_id": "results", "item_refs": [link["ref"] for link in payload["observed_links"]],
        }]}
    item = payload["items"][last["item_id"]]
    refs = [fact["ref"] for fact in payload["evidence"]
            if fact.get("kind") == "page" and "text" in fact and fact.get("observed_url") == DESTINATION]
    subtask = {"delegation_id": last["delegation_id"], "status": "satisfied", "evidence_refs": refs}
    if item["url"] == ALIAS:
        subtask["destination_ref"] = next(iter(item["navigation_destinations"]))
        return {"next_action": "continue", "subtask_review": subtask,
                "requirement_updates": [{"id": "inspect", "status": "satisfied", "evidence_refs": refs}],
                "loop_reviews": [{"loop_id": "results", "exhausted": True, "evidence_refs": refs}]}
    return {"next_action": "continue", "subtask_review": subtask}


def redirect_chooser(state, _goal, _history):
    if state["url"] == LIST_URL:
        action = next(action for action in state["actions"] if action.get("href") in {ALIAS, DESTINATION})
    else:
        # The next item can be opened only after the coordinator delegates it.
        # A completed prior destination is returned as a worker result first.
        action = None
    return {**decision(action["id"] if action else "DONE"),
            "operation": "CLICK" if action else "DONE", "target": None}


def test_new_link_order_is_durable_with_its_observation(tmp_path):
    browser = LoopBrowser(listing((ALIAS,)))
    with SupervisedAgent(LIST_URL, REDIRECT_GOAL, browser=browser, state_dir=tmp_path,
                         supervisor=V2Supervisor(plan=redirect_plan())) as agent:
        agent._record_page(listing((DESTINATION, ALIAS)))
        saved = agent.store.load_state()
        assert saved["observed_url_order"] == [ALIAS, DESTINATION]
        assert any(fact.get("href") == DESTINATION for fact in saved["evidence"].values())
        assert browser.inputs == []


def test_observed_redirect_merges_an_unstarted_alias_and_completes_one_item(tmp_path, monkeypatch):
    browser = RedirectBrowser(listing((ALIAS, DESTINATION)))
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=redirect_chooser))
    with SupervisedAgent(LIST_URL, REDIRECT_GOAL, browser=browser, state_dir=tmp_path,
                         supervisor=V2Supervisor(redirect_review, plan=redirect_plan())) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "done"
        assert len(result["items"]) == 1
        item = next(iter(result["items"].values()))
        assert item["url"] == ALIAS and item["aliases"] == [DESTINATION]
        assert item["status"] == "completed" and item["visited_refs"]
        duplicate_id = hashlib.sha256(DESTINATION.encode()).hexdigest()[:24]
        assert agent.state["item_aliases"][duplicate_id] == item["item_id"]
        assert len(agent.store.attempts()) == 1
    assert [action["href"] for action in browser.inputs] == [ALIAS]


def test_redirect_conflicting_with_an_attempted_item_rolls_back_completion(tmp_path, monkeypatch):
    browser = RedirectBrowser(listing((ALIAS, DESTINATION)))
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=redirect_chooser))
    with SupervisedAgent(LIST_URL, REDIRECT_GOAL, browser=browser, state_dir=tmp_path,
                         supervisor=V2Supervisor(redirect_review, plan=redirect_plan())) as agent:
        initialize(agent)
        agent._decide()  # Both observed URLs registered; only alias is delegated.
        other = next(item for item in agent.state["items"].values() if item["url"] == DESTINATION)
        # A durable prior attempt at that identity prevents merging its work,
        # even if the next observation claims to prove this alias complete.
        other["started_observation"] = agent.state["observation_seq"]
        agent._save()
        result = list(agent.run())[-1]
        assert result["status"] == "unverified" and result["stop_reason"] == "supervisor_invalid_response"
        assert len(result["items"]) == 2
        assert all(item["status"] != "completed" for item in result["items"].values())
        assert result["requirements"][0]["status"] == "unknown"
        assert agent.state["loops"]["results"]["exhausted"] is False
        assert agent.state["item_aliases"] == {}
        assert len(agent.store.attempts()) == 1
    assert [action["href"] for action in browser.inputs] == [ALIAS]


@pytest.mark.parametrize("bounded", [False, True])
def test_exhausted_discovery_still_schedules_items_and_blocks_dependent_work(tmp_path, bounded):
    goal = "Inspect exactly two distinct results once." if bounded else REDIRECT_GOAL
    plan = redirect_plan()
    plan["requirements"][0]["goal_quote"] = goal
    loop = plan["loops"][0]
    loop["goal_quote"] = goal
    loop["limit"] = 2 if bounded else None
    loop["discovery"]["goal_quote"] = goal
    loop["steps"][0]["goal_quote"] = goal
    plan["tasks"] = [{
        "id": "review-finished-work", "instruction": "Review the completed result inspection.",
        "success_criteria": ["All distinct results have been inspected."], "goal_quote": goal,
        "requirement_ids": ["inspect"], "depends_on": ["results"],
    }]
    browser = LoopBrowser(listing((ALIAS, DESTINATION)))

    def review(payload):
        evidence = [fact["ref"] for fact in payload["evidence"] if fact.get("kind") == "page" and "text" in fact]
        return {
            "next_action": "continue",
            "discovered": [{"loop_id": "results", "item_refs": [fact["ref"] for fact in payload["observed_links"]]}],
            "loop_reviews": [{"loop_id": "results", "exhausted": True, "evidence_refs": evidence}],
            # A model's broad completion claim must not skip scheduled item work.
            "requirement_updates": [{"id": "inspect", "status": "satisfied", "evidence_refs": evidence}],
        }

    with SupervisedAgent(LIST_URL, goal, browser=browser, state_dir=tmp_path,
                         supervisor=V2Supervisor(review, plan=plan)) as agent:
        initialize(agent)
        agent._decide()
        assert agent.state["loops"]["results"]["exhausted"] is True
        assert agent.state["requirements"][0]["status"] == "satisfied"
        assert agent.state["status"] != "done"
        assert agent.state["active_work"]["kind"] == "item"
        assert agent.state["pending_delegation"]["input"]["id"] == "inspect"
        assert agent.state["task_runs"] == {}
        assert not agent._dependencies_satisfied(["results"])
        assert sorted(item["status"] for item in agent.state["items"].values()) == ["active", "pending"]
        assert browser.inputs == []
        worker_module.choose.assert_not_called()


def test_positive_empty_scope_exhaustion_releases_discovery_without_replaying_it(tmp_path):
    browser = LoopBrowser(listing(()))

    def review(payload):
        if not payload.get("last_result"):
            return {"next_action": "continue"}
        refs = [fact["ref"] for fact in payload["evidence"] if fact.get("kind") == "page" and "text" in fact]
        # The settled page explicitly contains zero results and an end marker.
        # No separate subtask claim is needed to record discovery coverage.
        if not payload["loops"]["results"]["exhausted"]:
            return {"next_action": "continue",
                    "loop_reviews": [{"loop_id": "results", "exhausted": True, "evidence_refs": refs}]}
        return {"next_action": "continue",
                "requirement_updates": [{"id": "inspect", "status": "satisfied", "evidence_refs": refs}]}

    with SupervisedAgent(LIST_URL, REDIRECT_GOAL, browser=browser, state_dir=tmp_path,
                         supervisor=V2Supervisor(review, plan=redirect_plan())) as agent:
        initialize(agent)
        agent._decide()
        agent._delegate()
        agent._decide()
        assert agent.state["active_work"] is None
        assert agent.state["requirements"][0]["status"] == "unknown"
        assert agent.state["pending_delegation"] is None
        result = list(agent.run())[-1]
        assert result["status"] == "done"
        assert agent.state["loops"]["results"]["exhausted"] is True
        assert agent.state["active_work"] is None
        assert agent.state["items"] == {}
        assert agent.state["delegation_sequence"] == 1
        assert worker_module.choose.call_count == 1
        assert browser.inputs == []


def test_redirect_merge_below_requested_count_reopens_discovery_instead_of_finishing(tmp_path, monkeypatch):
    goal = "Inspect exactly two distinct results once."
    plan = redirect_plan()
    plan["requirements"][0]["goal_quote"] = goal
    loop = plan["loops"][0]
    loop["goal_quote"] = goal
    loop["limit"] = 2
    loop["discovery"]["goal_quote"] = goal
    loop["steps"][0]["goal_quote"] = goal
    browser = RedirectBrowser(listing((ALIAS, DESTINATION)))
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=redirect_chooser))

    def review(payload):
        result = redirect_review(payload)
        if payload.get("last_result") is None:
            refs = [fact["ref"] for fact in payload["evidence"] if fact.get("kind") == "page" and "text" in fact]
            result["loop_reviews"] = [{"loop_id": "results", "exhausted": True, "evidence_refs": refs}]
        else:
            # Two observed URLs collapse to one actual identity. The old scope
            # claim must no longer satisfy an explicit two-distinct-items goal.
            result.pop("loop_reviews")
        return result

    with SupervisedAgent(LIST_URL, goal, browser=browser, state_dir=tmp_path,
                         supervisor=V2Supervisor(review, plan=plan)) as agent:
        initialize(agent)
        agent._decide()
        assert agent.state["loops"]["results"]["exhausted"] is True
        assert len(agent.state["items"]) == 2
        agent._delegate()
        agent._decide()
        assert len(agent.state["items"]) == 1
        item = next(iter(agent.state["items"].values()))
        assert item["status"] == "completed" and item["aliases"] == [DESTINATION]
        assert agent.state["requirements"][0]["status"] == "satisfied"
        assert agent.state["status"] != "done"
        assert agent.state["loops"]["results"]["exhausted"] is False
        assert agent.state["active_work"]["kind"] == "discover"
        assert agent.state["pending_delegation"]["input"]["scope"] == "discover"
        assert not agent._dependencies_satisfied(["results"])
        assert len(agent.store.attempts()) == 1
    assert [action["href"] for action in browser.inputs] == [ALIAS]


@pytest.mark.parametrize("fresh_destination", [False, True])
def test_destination_step_requires_its_own_fresh_destination_reference(tmp_path, monkeypatch, fresh_destination):
    plan = redirect_plan()
    step = plan["loops"][0]["steps"][0]
    plan["loops"][0]["steps"].append({**step, "id": "confirm-again"})
    browser = LoopBrowser(listing((DESTINATION,)))
    monkeypatch.setattr(worker_module, "choose", Mock(side_effect=redirect_chooser))
    previous_destination = []

    def review(payload):
        last = payload.get("last_result")
        if last is None:
            return {"next_action": "continue", "discovered": [{
                "loop_id": "results", "item_refs": [link["ref"] for link in payload["observed_links"]],
            }]}
        pages = [fact for fact in payload["evidence"] if fact.get("kind") == "page" and "text" in fact]
        destinations = [fact for fact in pages if fact.get("observed_url") == DESTINATION]
        if last["task"]["id"] == "inspect":
            previous_destination[:] = [max(destinations, key=lambda fact: fact["observed_at"])]
            refs = [previous_destination[0]["ref"]]
        else:
            after = last["started_observation"]
            eligible = ([fact for fact in destinations if fact["observed_at"] > after]
                        if fresh_destination else previous_destination)
            assert eligible
            assert (eligible[0]["observed_at"] > after) == fresh_destination
            current_list = max((fact for fact in pages if fact.get("observed_url") == LIST_URL),
                               key=lambda fact: fact["observed_at"])
            assert current_list["observed_at"] > after
            refs = [eligible[0]["ref"], current_list["ref"]]
        result = {"next_action": "continue", "subtask_review": {
            "delegation_id": last["delegation_id"], "status": "satisfied", "evidence_refs": refs,
        }}
        if last["task"]["id"] == "confirm-again":
            result.update(requirement_updates=[{"id": "inspect", "status": "satisfied", "evidence_refs": refs}],
                          loop_reviews=[{"loop_id": "results", "exhausted": True, "evidence_refs": refs}])
        return result

    with SupervisedAgent(LIST_URL, REDIRECT_GOAL, browser=browser, state_dir=tmp_path,
                         supervisor=V2Supervisor(review, plan=plan)) as agent:
        initialize(agent)
        agent._decide()
        agent._delegate()
        agent._decide()
        assert agent.state["pending_delegation"]["input"]["id"] == "confirm-again"
        # A fresh destination observation exists for the second step, but the
        # review must cite it: an old destination plus a fresh list is insufficient.
        browser.page["text"] = "Updated destination details and confirmation are visible."
        browser.page["fingerprint"] = fingerprint(browser.page)
        agent.worker._install_page(browser.observe())
        agent._delegate()
        browser.page = deepcopy(browser.initial)
        agent.worker._install_page(browser.observe())
        result = list(agent.run())[-1]
        item = next(iter(result["items"].values()))
        if fresh_destination:
            assert result["status"] == "done"
            assert set(item["completed_steps"]) == {"inspect", "confirm-again"}
        else:
            assert result["status"] == "unverified"
            assert result["stop_reason"] == "supervisor_invalid_response"
            assert set(item["completed_steps"]) == {"inspect"}
            assert result["requirements"][0]["status"] == "unknown"
            assert agent.state["loops"]["results"]["exhausted"] is False
        assert len(agent.store.attempts()) == 1
    assert [action["href"] for action in browser.inputs] == [DESTINATION]


def test_resume_before_first_registration_preserves_first_observed_order(tmp_path):
    # More than nine nodes also exposes JSON key sorting (node 10 before 2).
    urls = tuple(f"https://example.test/items/{name}" for name in (
        "zeta", "beta", "omega", "alpha", "theta", "delta",
        "gamma", "sigma", "eta", "lambda", "kappa", "iota",
    ))
    original = LoopBrowser(listing(urls))
    original.target = f"{tmp_path.name}-order-before-registration"
    with SupervisedAgent(LIST_URL, REDIRECT_GOAL, browser=original, state_dir=tmp_path,
                         run_id="before-registration", supervisor=V2Supervisor(plan=redirect_plan())) as agent:
        initialize(agent)
        assert agent.state["items"] == {} and agent.state["pending_delegation"] is None
        assert original.inputs == []

    submitted_urls = []

    def reverse_refs(payload):
        refs_by_url = {fact["href"]: fact["ref"] for fact in payload["observed_links"]}
        assert set(refs_by_url) == set(urls)
        submitted_urls.extend(reversed(urls))
        return {"next_action": "continue", "discovered": [{
            "loop_id": "results", "item_refs": [refs_by_url[url] for url in reversed(urls)],
        }]}

    # The same tab now presents another order, before any links were registered.
    reconnected = LoopBrowser(listing(tuple(reversed(urls))))
    reconnected.target = original.target
    with SupervisedAgent.resume("before-registration", state_dir=tmp_path, browser=reconnected,
                                supervisor=V2Supervisor(reverse_refs, plan=redirect_plan())) as resumed:
        assert resumed.state["items"] == {}
        resumed._decide()
        registered = sorted(resumed.state["items"].values(), key=lambda item: item["registration_order"])
        assert [item["url"] for item in registered] == list(urls)
        active = resumed.state["items"][resumed.state["active_work"]["item_id"]]
        assert active["url"] == urls[0]
        assert resumed.state["pending_delegation"]["input"]["item_ref"] == active["item_id"]
        assert resumed.store.attempts() == []
        assert reconnected.inputs == []
    assert submitted_urls == list(reversed(urls))
    worker_module.choose.assert_not_called()
