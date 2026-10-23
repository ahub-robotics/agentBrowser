"""Evidence review gates the actual graph, including manual execution, without paid APIs."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_agent import decision, page

from AgentBrowser import agent as loop
from AgentBrowser.browser import StalePage
from AgentBrowser.candidates import DEFAULT_LIMITS
from AgentBrowser.verification import VerificationError, prepare_context, validate_assessment


def pick(action="e3"):
    result = decision(action)
    result["operation"] = {"e3": "CLICK", "e1": "TYPE_TEXT", "wait": "WAIT"}.get(action, action)
    return result


def review(payload, *, status="unsatisfied", disposition="allow"):
    output = {
        "requirements": [{"id": "outcome", "goal_quote": payload["goal"], "description": "Requested outcome",
                          "status": status, "evidence_refs": [payload["evidence"][0]["ref"]]}],
        "action_assessment": disposition, "feedback": "The observed selection is still incomplete.",
    }
    return validate_assessment(output, prepare_context(payload)), {"model": "offline", "http_attempts": 1}


def session(monkeypatch, reviewer, **kwargs):
    browser = Mock(fresh=Mock(return_value=True), observe=Mock(return_value=page()))
    monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))
    agent = loop.Agent("https://example.test", "Find a book", verifier=reviewer, **kwargs)
    return agent, browser


def test_incomplete_confirmation_never_executes_and_is_not_offered_again(monkeypatch):
    reviewer = Mock(side_effect=lambda p: review(p, disposition="reconsider"))
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    state = agent.command("tick")
    browser.act.assert_not_called()
    assert state["history"] == [] and state["decision"] is None
    assert state["status"] == "ready" and state["verification_calls"] == 1
    assert "e3" not in {action["id"] for action in state["page"]["actions"]}
    assert state["progress_feedback"]["action_was_executed"] is False


def test_changed_state_releases_pre_input_rejection(monkeypatch):
    agent, browser = session(monkeypatch, lambda p: review(p, disposition="reconsider"))
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    agent.command("tick")
    updated = deepcopy(page())
    updated["actions"][0]["value"] = "Book"
    agent._install_page(updated)
    assert "e3" in {action["id"] for action in agent.state["page"]["actions"]}
    browser.act.assert_not_called()


def test_unknown_done_recovers_with_bound_and_never_claims_success(monkeypatch):
    reviewer = Mock(side_effect=lambda p: review(p, status="unknown"))
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick("DONE")))
    states = list(agent.run())
    assert [state["status"] for state in states] == ["ready", "unverified"]
    assert states[-1]["stop_reason"] == "verification_no_progress"
    browser.act.assert_not_called()
    assert len(states[-1]["history"]) == 0
    assert states[-1]["verification_calls"] == 1


def test_already_correct_outcome_can_finish_without_edit(monkeypatch):
    agent, browser = session(monkeypatch, lambda p: review(p, status="satisfied"))
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick("DONE")))
    assert agent.command("tick")["status"] == "done"
    browser.act.assert_not_called()


def test_predict_then_act_reuses_same_review_and_executes_once(monkeypatch):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    predicted = agent.command("predict")
    binding = agent.decision_body()
    assert predicted["status"] == "predicted"
    browser.act.assert_not_called()
    agent.command("act", binding)
    reviewer.assert_called_once()
    browser.act.assert_called_once()
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", binding)
    browser.act.assert_called_once()


def test_page_change_during_review_prevents_input(monkeypatch):
    browser = None

    def reviewer(payload):
        browser.fresh.return_value = False
        return review(payload)

    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    state = agent.command("tick")
    assert state["status"] == "ready" and state["decision"] is None
    browser.act.assert_not_called()


def test_layout_change_retains_unexecuted_target_for_next_policy_and_review(monkeypatch):
    payloads = []

    def reviewer(payload):
        payloads.append(deepcopy(payload))
        if len(payloads) == 1:
            browser.fresh.return_value = False
        return review(payload)

    agent, browser = session(monkeypatch, reviewer)
    before = deepcopy(page())
    before["actions"][2].update(role="link", label="First visible item", context={
        "id": "results", "kind": "region", "heading": "Results", "selector": "never sent",
    })
    agent._install_page(before)
    after = deepcopy(before)
    after.update(text="A new banner moved the results below the viewport", fingerprint="layout-shift")
    after["actions"] = [item for item in after["actions"] if item["id"] != "e3"]

    def observe(*, screenshot):
        browser.fresh.return_value = True
        return after

    browser.observe.side_effect = observe
    chooser = Mock(side_effect=[pick(), pick("wait"), pick("DONE")])
    monkeypatch.setattr(loop, "choose", chooser)
    recovered = agent.command("tick")
    feedback = recovered["progress_feedback"]
    cancelled = feedback["recovery"]
    assert cancelled["action_was_executed"] is False
    assert cancelled["proposed_action"] == {
        "operation": "CLICK", "kind": "click", "label": "First visible item", "role": "link",
        "context": {"id": "results", "kind": "region", "heading": "Results"},
    }
    assert cancelled["observed_changes"][0]["kind"] == "control_no_longer_observed"
    assert recovered["history"] == [] and recovered["decision"] is None
    assert agent._navigation_source == [] and agent._historical_evidence == {}
    browser.act.assert_not_called()
    agent.command("predict")
    submitted = chooser.call_args.args[0]["_candidate_request"]
    assert submitted["state"]["progress_feedback"] == feedback
    # Inspecting/waiting after a cancelled proposal adds no semantic review.
    assert len(payloads) == 1
    agent.command("predict")
    assert payloads[-1]["progress_feedback"] == feedback
    assert payloads[-1]["history"] == []
    assert payloads[-1]["execution_history"] == []
    assert len(payloads) == 2
    browser.act.assert_not_called()


def test_review_failure_is_unverified_and_accounts_http_attempts(monkeypatch):
    reviewer = Mock(side_effect=VerificationError("Invalid review", reason="invalid_response",
                                                  metadata={"http_attempts": 2}))
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    state = agent.command("tick")
    assert state["status"] == "unverified"
    assert state["verification_http_attempts"] == 2
    browser.act.assert_not_called()


def test_verification_budget_stops_before_review_and_input(monkeypatch):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer, max_verification_calls=1)
    agent.state["verification_calls"] = 1
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    state = agent.command("tick")
    assert state["status"] == "unverified" and state["stop_reason"] == "verification_budget"
    reviewer.assert_not_called()
    browser.act.assert_not_called()


def test_loading_observations_do_not_call_models_or_replay_input(monkeypatch):
    reviewer = Mock()
    agent, browser = session(monkeypatch, reviewer)
    loading = deepcopy(page())
    loading.update(text="", readiness={"status": "loading"})
    agent._install_page(loading)
    browser.observe.return_value = loading
    chooser = Mock()
    monkeypatch.setattr(loop, "choose", chooser)
    states = list(agent.run())
    assert [state["status"] for state in states] == ["observing", "observing", "blocked"]
    assert states[-1]["stop_reason"] == "observation_not_ready"
    chooser.assert_not_called()
    reviewer.assert_not_called()
    browser.act.assert_not_called()


def test_typing_uses_only_text_helper_when_no_transition_requires_review(monkeypatch):
    reviewer = Mock()
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick("e1")))
    helper = Mock(return_value=("book", {"model": "text-offline", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_text", helper)
    agent.command("tick")
    reviewer.assert_not_called()
    helper.assert_called_once()
    browser.act.assert_called_once()


@pytest.mark.parametrize("changed", ["feedback", "requirements", "readiness", "coverage", "document"])
def test_manual_authorization_is_invalidated_by_every_changed_review_input(monkeypatch, changed):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    agent.command("predict")
    binding = agent.decision_body()
    if changed == "feedback":
        agent.state["progress_feedback"] = {"feedback": "A newly observed requirement remains incomplete"}
    elif changed == "requirements":
        updated = deepcopy(agent.state["verification"]["requirements"])
        updated[0]["status"] = "unknown"
        agent.state["verification"]["requirements"] = updated
    elif changed == "readiness":
        agent.state["page"]["readiness"] = {"status": "ready", "reason": "new observation"}
    elif changed == "coverage":
        agent.state["page"]["candidate_coverage"]["extraction_complete"] = False
    else:
        agent._candidate_raw_page["page_key"] = [999, agent.state["page"]["url"]]
    agent.command("act", binding)
    assert reviewer.call_count == 2
    assert agent.state["verification_calls"] == agent.state["verification_http_attempts"] == 2
    browser.act.assert_called_once()


def test_reviewer_receives_unknown_extraction_and_observation_readiness(monkeypatch):
    reviewer = Mock(side_effect=review)
    agent, _browser = session(monkeypatch, reviewer)
    observed = deepcopy(page())
    observed.update(omitted_actions=5, readiness={"status": "ready", "document_state": "complete"})
    agent._install_page(observed)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    agent.command("predict")
    context = prepare_context(reviewer.call_args.args[0])
    assert context["coverage"]["window"]["extraction_complete"] is False
    assert context["coverage"]["readiness"]["status"] == "ready"


def test_policy_filtering_is_not_reported_as_a_dom_change(monkeypatch):
    reviewer = Mock(side_effect=lambda payload: review(payload, disposition="reconsider"))
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[pick(), pick("wait")]))
    rejected = agent.command("tick")
    assert "e3" not in {action["id"] for action in rejected["page"]["actions"]}
    observed = agent.command("tick")
    assert observed["history"][-1]["effects"] == [{"kind": "no_observed_change"}]
    assert observed["model_calls"] == observed["candidate_windows"] == 2
    assert observed["verification_calls"] == 1
    browser.act.assert_called_once()
    assert browser.act.call_args.args[0]["kind"] == "wait"


def test_transition_review_feedback_reaches_the_text_helper(monkeypatch):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    agent._review_due = True
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick("e1")))
    helper = Mock(return_value=("book", {"model": "text-offline", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_text", helper)
    agent.command("tick")
    reviewer.assert_called_once()
    assert helper.call_args.args[0]["progress_feedback"]["requirements"][0]["status"] == "unsatisfied"
    browser.act.assert_called_once()


def test_rejection_invalidates_the_original_manual_binding(monkeypatch):
    reviewer = Mock(side_effect=lambda payload: review(payload, disposition="reconsider"))
    agent, browser = session(monkeypatch, reviewer)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    agent._prepare()
    agent._choose(None)
    binding = agent.decision_body()
    agent.command("act", binding)
    assert agent.state["decision"] is None
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", binding)
    assert agent.state["model_calls"] == agent.state["candidate_windows"] == 1
    browser.act.assert_not_called()


def test_review_recovery_cannot_reset_the_global_candidate_window_budget(monkeypatch):
    reviewer = Mock(side_effect=lambda payload: review(payload, disposition="reconsider"))
    agent, browser = session(monkeypatch, reviewer, candidate_limits=replace(DEFAULT_LIMITS, max_windows=1))
    chooser = Mock(return_value=pick())
    monkeypatch.setattr(loop, "choose", chooser)
    states = list(agent.run())
    assert [state["status"] for state in states] == ["ready", "blocked"]
    assert states[-1]["stop_reason"] == "window_limit"
    assert states[-1]["model_calls"] == states[-1]["candidate_windows"] == 1
    chooser.assert_called_once()
    reviewer.assert_called_once()
    browser.act.assert_not_called()


def test_rejected_inventory_operation_preserves_other_operations_and_rebinds_window(monkeypatch):
    observed = deepcopy(page())
    observed["inventory"] = {
        "nodes": [{"node": 10, "label": "Search", "role": "textbox", "value": "",
                   "operations": ["fill", "click"]}],
        "globals": [{"id": "wait", "kind": "wait", "label": "Wait"}],
        "coverage": {"extraction_complete": True},
    }
    browser = Mock(fresh=Mock(return_value=True), observe=Mock(return_value=observed))
    monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))
    reviewer = Mock(side_effect=lambda payload: review(payload, disposition="reconsider"))
    agent = loop.Agent("https://example.test", "Find a book", verifier=reviewer)
    agent._review_due = True
    click = {**pick(), "choice": "e10_click", "probabilities": {"e10_click": 1}}
    fill = {**pick("e1"), "choice": "e10_fill", "probabilities": {"e10_fill": 1}}
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[click, fill]))
    agent._prepare()
    agent._choose(None)
    stale_binding = agent.decision_body()
    agent.command("act", stale_binding)
    assert {action["id"] for action in agent.state["page"]["actions"]} == {"e10_fill", "wait"}
    assert agent.state["page"]["candidate_coverage"]["review_excluded_actions"] == 1
    agent.command("predict")
    assert agent.decision_body()["selection_digest"] != stale_binding["selection_digest"]
    with pytest.raises(ValueError, match="Observe and choose"):
        agent.command("act", stale_binding)
    assert agent.state["decision"]["choice"] == "e10_fill"
    assert agent.state["model_calls"] == agent.state["candidate_windows"] == 2
    browser.act.assert_not_called()


@pytest.mark.parametrize("old_status", ["satisfied", "unsatisfied", "unknown"])
def test_previous_requirements_never_send_unavailable_refs_to_the_reviewer(monkeypatch, old_status):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    agent.state["verification"]["requirements"] = [{
        "id": "outcome", "goal_quote": "Find a book", "description": "Requested outcome",
        "status": old_status, "evidence_refs": ["obsolete-document:node:10"],
    }]
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    agent.command("predict")
    payload = reviewer.call_args.args[0]
    previous = payload["previous_requirements"][0]
    assert previous["id"] == "outcome" and previous["goal_quote"] == "Find a book"
    assert previous["status"] == "unknown" and previous["evidence_refs"] == []
    available = {fact["ref"] for fact in payload["evidence"]}
    assert all(set(item["evidence_refs"]) <= available for item in payload["previous_requirements"])
    agent.command("act", agent.decision_body())
    reviewer.assert_called_once()
    browser.act.assert_called_once()


def test_readiness_and_extraction_are_citable_current_facts(monkeypatch):
    reviewer = Mock(side_effect=review)
    agent, _browser = session(monkeypatch, reviewer)
    observed = deepcopy(page())
    observed.update(readiness={"status": "ready", "reads": 4, "waited_ms": 300},
                    page_key=[123, observed["url"]])
    agent._install_page(observed)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    agent.command("predict")
    context = prepare_context(reviewer.call_args.args[0])
    fact = next(item for item in context["evidence"] if item["kind"] == "observation")
    assert fact["readiness"] == {"status": "ready"}
    assert fact["coverage"] == {}
    assert fact["document"] == [123, observed["url"]]
    assert fact["ref"].endswith(":observation")
    result = {
        "requirements": [{"id": "outcome", "goal_quote": "Find a book", "description": "Requested outcome",
                          "status": "unknown", "evidence_refs": [fact["ref"]]}],
        "action_assessment": "allow", "feedback": "Observation is ready; outcome remains unverified.",
    }
    assert validate_assessment(result, context)["all_satisfied"] is False


def test_predict_act_does_not_repeat_review_for_requirements_without_supporting_refs(monkeypatch):
    def reviewer(payload):
        result = {
            "requirements": [{"id": "outcome", "goal_quote": "Find a book", "description": "Requested outcome",
                              "status": "unsatisfied", "evidence_refs": []}],
            "action_assessment": "allow", "feedback": "The selected action may advance the pending goal.",
        }
        return validate_assessment(result, prepare_context(payload)), {"model": "offline", "http_attempts": 1}

    reviewer_mock = Mock(side_effect=reviewer)
    agent, browser = session(monkeypatch, reviewer_mock)
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    agent.command("predict")
    agent.command("act", agent.decision_body())
    reviewer_mock.assert_called_once()
    browser.act.assert_called_once()


@pytest.mark.parametrize("change_feedback", [False, True])
def test_transition_review_remains_required_for_typing_between_predict_and_act(monkeypatch, change_feedback):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    agent._review_due = True
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick("e1")))
    helper = Mock(return_value=("book", {"model": "text-offline", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_text", helper)
    agent.command("predict")
    assert agent.state["decision"]["review_trigger"] == "transition"
    assert agent._review_due is False
    if change_feedback:
        agent.state["progress_feedback"] = {"feedback": "New feedback after the prediction"}
    agent.command("act", agent.decision_body())
    assert reviewer.call_count == (2 if change_feedback else 1)
    if change_feedback:
        assert reviewer.call_args.args[0]["progress_feedback"]["feedback"] == "New feedback after the prediction"
    helper.assert_called_once()
    browser.act.assert_called_once()


@pytest.mark.parametrize("changed", ["feedback", "effects"])
def test_stale_text_cache_keeps_the_actual_helper_input_when_state_mutates_in_place(monkeypatch, changed):
    agent, browser = session(monkeypatch, Mock(side_effect=review))
    if changed == "effects":
        agent.state["history"].append({"action": "Earlier observation", "text": None,
                                       "effects": [{"kind": "no_observed_change"}]})
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick("e1")))
    helper = Mock(side_effect=[
        ("old value", {"model": "text-offline", "latency_ms": 1}),
        ("updated value", {"model": "text-offline", "latency_ms": 1}),
    ])
    monkeypatch.setattr(loop, "field_text", helper)
    browser.act.side_effect = [StalePage("The page changed before any browser input"), None]
    agent.command("predict")
    with pytest.raises(StalePage):
        agent.command("act", agent.decision_body())
    actual_input = deepcopy(agent.pending_text[0])
    if changed == "feedback":
        agent.state["progress_feedback"]["feedback"] = "New evidence changes the field meaning"
    else:
        agent.state["history"][-1]["effects"].append({"kind": "value", "after": "Updated evidence"})
    assert agent.pending_text[0] == actual_input
    agent.command("predict")
    agent.command("act", agent.decision_body())
    assert helper.call_count == 2
    assert browser.act.call_args.kwargs["text"] == "updated value"
    assert helper.call_args_list[0].args[0] == actual_input


@pytest.mark.parametrize("stale_after_input", [False, True])
def test_navigation_retains_observed_source_as_history_even_when_post_action_read_fails(monkeypatch, stale_after_input):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    before = deepcopy(page())
    link = next(action for action in before["actions"] if action["id"] == "e3")
    link.update(role="link", label="First visible item", href="https://example.test/item/1", href_truncated=False)
    agent._install_page(before)
    after = deepcopy(before)
    after.update(url=link["href"], title="Item detail", text="Detail document", fingerprint="after-navigation")
    after["actions"] = [{"id": "wait", "kind": "wait", "label": "Wait"}]
    observations = [StalePage("Navigation interrupted the read"), after] if stale_after_input else [after]
    browser.observe.side_effect = observations
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[pick(), pick("DONE")]))
    first = agent.command("tick")
    assert first["page"]["url"] == link["href"]
    assert len(first["history"]) == 1
    browser.act.assert_called_once()
    if stale_after_input:
        assert first["progress_feedback"]["recovery"]["action_was_executed"] is True
        assert first["progress_feedback"]["recovery"]["proposed_action"]["label"] == "First visible item"
    source = agent._navigation_source
    assert len(source) <= 16 and source[0]["selected_control"] is True
    assert source[0]["href"] == link["href"]
    assert source[0]["observed_before_step"] == 1
    agent.command("predict")
    context = prepare_context(reviewer.call_args.args[0])
    current_url = next(fact for fact in context["evidence"] if "url" in fact and not fact.get("historical"))
    old_url = next(fact for fact in context["evidence"] if "url" in fact and fact.get("navigation_source"))
    assert current_url["url"] == link["href"] and old_url["url"] == before["url"]
    assert old_url["historical"] is True
    selected = next(fact for fact in context["evidence"] if fact.get("selected_control"))
    assert selected["ref"] == source[0]["ref"] and selected["historical"] is True
    assert len({fact["ref"] for fact in context["evidence"]}) == len(context["evidence"])
    browser.act.assert_called_once()


def test_delayed_navigation_retains_source_across_same_url_observations(monkeypatch):
    agent, browser = session(monkeypatch, Mock(side_effect=review))
    before = deepcopy(page())
    before["actions"][2].update(role="link", href="https://example.test/detail", href_truncated=False)
    agent._install_page(before)
    after = {**deepcopy(before), "url": "https://example.test/detail", "fingerprint": "new-document"}
    browser.observe.side_effect = [before, after]
    monkeypatch.setattr(loop, "choose", Mock(return_value=pick()))
    agent.command("tick")
    assert agent._navigation_source == []
    assert agent._pending_navigation_source["facts"][0]["selected_control"] is True
    # A later freshness read discovers the navigation, without another input.
    browser.fresh.return_value = False
    agent._prepare()
    assert agent._pending_navigation_source is None
    assert agent._navigation_source[0]["href"] == after["url"]
    assert agent._navigation_source[0]["observed_before_step"] == 1
    browser.act.assert_called_once()


def test_executed_same_label_controls_keep_context_before_observation_and_in_review(monkeypatch):
    reviewer = Mock(side_effect=review)
    agent, browser = session(monkeypatch, reviewer)
    first = deepcopy(page())
    first["actions"][2].update(label="Accept", context={
        "id": "model", "kind": "dialog", "heading": "Model", "selector": "not-for-history",
    })
    second = deepcopy(first)
    second.update(fingerprint="price-panel", text="Price panel")
    second["actions"][2]["context"] = {"id": "price", "kind": "dialog", "heading": "Price"}
    agent._install_page(first)

    def observe(*, screenshot):
        assert screenshot is False
        assert len(agent.state["history"]) == browser.act.call_count
        expected = "Model" if browser.act.call_count == 1 else "Price"
        assert agent.state["history"][-1]["context"]["heading"] == expected
        assert set(agent.state["history"][-1]["context"]) == {"id", "kind", "heading"}
        return deepcopy(second)

    browser.observe.side_effect = observe
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[pick(), pick(), pick("DONE")]))
    agent.command("tick")
    agent.command("tick")
    agent.command("predict")
    sent = reviewer.call_args.args[0]["history"]
    assert [entry["action"] for entry in sent] == ["Accept", "Accept"]
    assert [entry["context"]["heading"] for entry in sent] == ["Model", "Price"]
    executed = reviewer.call_args.args[0]["execution_history"]
    assert [entry["context"]["heading"] for entry in executed] == ["Model", "Price"]
    assert [entry["step"] for entry in executed] == [1, 2]
    previous_context = agent._request_context()
    agent.state["history"][0]["context"]["heading"] = "Changed panel"
    assert agent._request_context() != previous_context
    assert browser.act.call_count == 2
