"""Bounded supervisor cooldowns preserve deadlines and never replay browser input."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from langchain_openai import ChatOpenAI
from test_agent import decision, page
from test_coordinator_recovery import GOAL, V2Supervisor, accepted_result, browser_for

from agentrun import coordinator, supervised
from agentrun.supervised import SupervisedAgent, SupervisorLimits
from agentrun.supervisor_policy import SupervisorPolicy
from AgentBrowser import agent as worker_module
from AgentBrowser import model


def transient(delay=2):
    return model.ProviderError("billing", status_code=402, limit_source="openrouter_in_flight_budget",
                               limit_reason="in_flight_budget_exhausted", retry_after_seconds=delay)


class Clock:
    def __init__(self):
        self.now = 2_000_000_000.0
        self.sleeps = []
        self.interrupt = False
        self.wall_offset = 0
        self.wall_jump = 0

    def read(self):
        return self.now

    def utc(self):
        return self.now + self.wall_offset

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        self.wall_offset += self.wall_jump
        self.wall_jump = 0
        if self.interrupt:
            raise KeyboardInterrupt()


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    clock_module = SimpleNamespace(time=clock.utc, perf_counter=clock.read, monotonic=clock.read, sleep=clock.sleep)
    for module in (coordinator, supervised, worker_module):
        monkeypatch.setattr(module, "time", clock_module)
    return clock


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model, "CLIENT", Mock(post=Mock(side_effect=AssertionError("No paid APIs"))))
    monkeypatch.setattr(worker_module, "field_text", Mock(side_effect=AssertionError("No text API")))
    monkeypatch.setattr(worker_module, "choose", Mock(return_value={
        **decision("DONE"), "operation": "DONE", "target": None,
    }))


def agent_for(tmp_path, supervisor, **kwargs):
    return SupervisedAgent(page()["url"], GOAL, run_id="provider-recovery", state_dir=tmp_path,
                           supervisor=supervisor, browser=kwargs.pop("browser", browser_for()), trace=False, **kwargs)


class FlakySupervisor(V2Supervisor):
    def __init__(self, errors, *, phase="initialize", after_worker=False):
        super().__init__(accepted_result)
        self.errors = list(errors)
        self.fail_phase = phase
        self.after_worker = after_worker
        self.http_attempts = 0

    def __call__(self, payload):
        self.http_attempts += 1
        if (payload["phase"] == self.fail_phase and (not self.after_worker or payload.get("last_result"))
                and self.errors):
            self.payloads.append(deepcopy(payload))
            raise self.errors.pop(0)
        return super().__call__(payload)


@pytest.mark.parametrize(("value", "expected"), [
    ("120", 120), (" 0 ", 0), ("999999999999999999999", 2**31 - 1),
    ("-1", None), ("NaN", None), ("PRIVATE_RESPONSE", None), (True, None),
    ({"Retry-After": 120}, None), ([], None), (float("inf"), None), (1.1, 2), (10**400, 2**31 - 1),
])
def test_retry_after_parser_never_retains_untrusted_text(value, expected):
    assert model.provider_retry_after(value) == expected


def test_retry_after_http_date_rounds_up_and_uses_longest_valid_source():
    now = 2_000_000_000.25
    future = format_datetime(datetime.fromtimestamp(now + 120, timezone.utc), usegmt=True)
    assert model.provider_retry_after(future, now=now) == 120
    assert model.provider_retry_after_headers({"Retry-After": "110"}, {"retry-after": future}, now=now) == 120
    assert model.provider_retry_after_headers({"Retry-After": "PRIVATE"}, {"Retry-After": "130"}) == 130


@pytest.mark.parametrize("value", [True, "120", "PRIVATE_BODY", {}, [], float("nan"), -1])
def test_provider_error_rejects_unsafe_retry_metadata(value):
    error = model.ProviderError("billing", status_code=402, retry_after_seconds=value)
    assert "provider_retry_after_seconds" not in error.metadata
    assert "PRIVATE" not in str(error) + json.dumps(error.metadata)


@pytest.mark.parametrize(("response_headers", "metadata_headers", "expected"), [
    ({"Retry-After": "120"}, {}, 120), ({}, {"Retry-After": "120"}, 120),
    ({"Retry-After": "60"}, {"Retry-After": "120"}, 120),
    ({"Retry-After": "PRIVATE"}, {"Retry-After": "121"}, 121),
])
def test_sdk_extracts_only_safe_retry_after_without_implicit_http_retry(response_headers, metadata_headers, expected):
    requests = []

    def transport(request):
        requests.append(request.content)
        return httpx.Response(402, headers=response_headers, json={"error": {
            "code": 402, "message": "PRIVATE_RESPONSE", "metadata": {
                "limit_source": "openrouter_in_flight_budget", "reason": "in_flight_budget_exhausted",
                "headers": {**metadata_headers, "Authorization": "PRIVATE_HEADER"},
            },
        }})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        policy = SupervisorPolicy(model=ChatOpenAI(
            model="offline", api_key="PRIVATE_KEY", base_url="https://openrouter.ai/api/v1",
            max_retries=0, http_client=client,
        ))
        with pytest.raises(model.ProviderError) as caught:
            policy.invoke({"contract_version": 2, "phase": "review", "goal": GOAL})
    assert len(requests) == policy.http_attempts == 1
    assert caught.value.metadata["provider_retry_after_seconds"] == expected
    assert caught.value.metadata["provider_limit_reason"] == "in_flight_budget_exhausted"
    assert "PRIVATE" not in json.dumps(caught.value.metadata) + str(caught.value)


@pytest.mark.parametrize(("status", "source", "reason"), [
    (402, "openrouter_credits", None), (402, "openrouter_key_limit", None),
    (402, "openrouter_in_flight_budget", "weight_exceeds_budget"),
    (402, None, None), (429, "openrouter_in_flight_budget", "in_flight_budget_exhausted"),
    (403, "openrouter_workspace_budget", None),
])
def test_other_provider_failures_do_not_wait_or_retry(tmp_path, clock, status, source, reason):
    error = model.ProviderError("billing", status_code=status, limit_source=source,
                                limit_reason=reason, retry_after_seconds=1)
    policy = FlakySupervisor([error])
    with agent_for(tmp_path, policy) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "blocked" and final["stop_reason"] == "supervisor_provider_error"
        assert agent.state["provider_retry_count"] == 0 and agent.state["provider_retry"] is None
        agent.browser.act.assert_not_called()
    assert policy.http_attempts == 1 and not clock.sleeps


def test_two_global_retries_preserve_counts_and_never_repeat_worker(tmp_path, clock, monkeypatch):
    choose = Mock(side_effect=[{**decision("e3"), "operation": "CLICK", "target": "2"},
                               {**decision("DONE"), "operation": "DONE", "target": None}])
    monkeypatch.setattr(worker_module, "choose", choose)
    policy = FlakySupervisor([transient(), transient()], phase="review", after_worker=True)
    fake = browser_for()
    with agent_for(tmp_path, policy, browser=fake) as agent:
        states = list(agent.run())
        final = states[-1]
        assert final["status"] == "done"
        assert agent.state["provider_retry_count"] == 2 and agent.state["provider_retry"] is None
        assert "provider_error" not in agent.state
        assert final["metrics"]["supervisor_calls"] == final["metrics"]["supervisor_http_attempts"] == 6
        assert final["metrics"]["actions"] == 1 and final["metrics"]["jev_calls"] == 2
        assert any(state["status"] == "waiting_provider" for state in states)
        assert agent.state["elapsed_seconds"] == 0
        assert final["provider_wait_seconds"] == 4
        assert final["provider_wait_limit_seconds"] == 600
        assert len(agent.store.attempts()) == 1
        reviewed = [payload for payload in policy.payloads if payload.get("last_result")]
        assert len({payload["last_result"]["delegation_id"] for payload in reviewed}) == 1
        assert len({max(fact["observed_at"] for fact in payload["evidence"]) for payload in reviewed}) == 3
    assert sum(clock.sleeps) == 4 and max(clock.sleeps) <= 1
    assert fake.act.call_count == 1 and choose.call_count == 2


def test_third_transient_failure_is_technical_stop_and_count_never_resets(tmp_path, clock):
    policy = FlakySupervisor([transient(), transient(), transient()])
    with agent_for(tmp_path, policy) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "blocked" and final["stop_reason"] == "supervisor_provider_retry_exhausted"
        assert agent.state["provider_retry_count"] == 2 and final["question"] == ""
        agent.browser.act.assert_not_called()
    assert policy.http_attempts == 3 and sum(clock.sleeps) == 4


def test_retry_allowance_is_global_across_successful_phases(tmp_path, clock):
    class EachPhaseFailsOnce(V2Supervisor):
        def __init__(self):
            super().__init__()
            self.failed_phases = set()
            self.http_attempts = 0

        def __call__(self, payload):
            self.http_attempts += 1
            if payload["phase"] not in self.failed_phases:
                self.failed_phases.add(payload["phase"])
                raise transient(1)
            return super().__call__(payload)

    policy = EachPhaseFailsOnce()
    with agent_for(tmp_path, policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_provider_retry_exhausted"
        assert agent.state["provider_retry_count"] == 2 and agent.state["contract_phase"] == "review"
        agent.browser.act.assert_not_called()
    assert sum(clock.sleeps) == 2 and policy.http_attempts == 5


def test_server_delay_is_never_shortened_to_fit_limits(tmp_path, clock):
    policy = FlakySupervisor([transient(301)])
    with agent_for(tmp_path, policy, limits=SupervisorLimits(max_seconds=1000)) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_provider_retry_delay_exceeded"
        assert agent.state["provider_retry_count"] == 0
    assert not clock.sleeps and policy.http_attempts == 1


@pytest.mark.parametrize("delay", [None, 120, 300])
def test_provider_cooldown_does_not_consume_active_execution_budget(tmp_path, clock, delay):
    policy = FlakySupervisor([transient(delay)], phase="audit")
    with agent_for(tmp_path, policy, limits=SupervisorLimits(max_seconds=30)) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "done"
        assert final["provider_wait_seconds"] == (delay or 120)
        assert final["elapsed_ms"] == 0
        assert agent.state["provider_retry_count"] == 1
    assert sum(clock.sleeps) == (delay or 120)


def test_calls_and_fresh_observation_consume_active_time_after_cooldown(tmp_path, clock):
    class TimedSupervisor(FlakySupervisor):
        def __call__(self, payload):
            clock.now += 2
            return super().__call__(payload)

    policy = TimedSupervisor([transient(120)])
    fake = browser_for()
    original_observe = fake.observe.side_effect

    def observe(**kwargs):
        clock.now += 1
        return original_observe(**kwargs)

    with agent_for(tmp_path, policy, browser=fake, limits=SupervisorLimits(max_seconds=60)) as agent:
        fake.observe.side_effect = observe
        before_observations = fake.observe.call_count
        final = list(agent.run())[-1]
        observations = fake.observe.call_count - before_observations
        assert final["status"] == "done"
        assert agent.state["elapsed_seconds"] == 2 * policy.http_attempts + observations
        assert final["provider_wait_seconds"] == 120
        assert agent._active_start is None


def test_cooldown_cannot_hide_an_exhausted_active_budget(tmp_path, clock):
    class SlowFailingSupervisor(FlakySupervisor):
        def __call__(self, payload):
            clock.now += 20
            return super().__call__(payload)

    policy = SlowFailingSupervisor([transient(120)])
    with agent_for(tmp_path, policy, limits=SupervisorLimits(max_seconds=10)) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "time_budget" and final["provider_wait_seconds"] == 0
        assert final["provider_retry_count"] == 0
    assert policy.http_attempts == 1 and not clock.sleeps


def test_supervisor_call_budget_prevents_scheduling_another_request(tmp_path, clock):
    policy = FlakySupervisor([transient()])
    with agent_for(tmp_path, policy, limits=SupervisorLimits(max_supervisor_calls=1)) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_budget"
    assert not clock.sleeps and policy.http_attempts == 1


def test_resume_preserves_pending_cooldown_and_reobserves_without_input(tmp_path, clock):
    policy = FlakySupervisor([transient(4)])
    with agent_for(tmp_path, policy) as first:
        first._decide()
        pending = deepcopy(first.state["provider_retry"])
        assert first.store.load_state()["provider_retry"] == pending
        first._decide()  # No inline sleep or request before the graph wait node.
        assert first.state["status"] == "waiting_provider" and policy.http_attempts == 1
    clock.now += 1
    reconnect = browser_for()
    with SupervisedAgent.resume("provider-recovery", state_dir=tmp_path, browser=reconnect,
                                supervisor=V2Supervisor(accepted_result), trace=False) as resumed:
        resumed._active_start = clock.now
        before = reconnect.observe.call_count
        resumed._decide()
        assert resumed.state["status"] == "waiting_provider"
        assert resumed.state["provider_retry"] == pending
        resumed._wait_provider_retry()
        assert resumed.state["status"] == "ready" and resumed.state["provider_retry"] is None
        assert resumed.state["provider_retry_count"] == 1
        assert reconnect.observe.call_count == before + 1
        assert resumed.state["elapsed_seconds"] == 0
        assert resumed.state["provider_wait_seconds"] == 4
        assert resumed.state["supervisor_calls"] == 1
        reconnect.act.assert_not_called()
    assert sum(clock.sleeps) == 3


def test_interrupted_wait_stays_durable_and_does_not_spend_another_retry(tmp_path, clock):
    policy = FlakySupervisor([transient(4)])
    with agent_for(tmp_path, policy) as agent:
        agent._decide()
        agent._active_start = clock.now
        clock.interrupt = True
        with pytest.raises(KeyboardInterrupt):
            agent._wait_provider_retry()
        saved = agent.store.load_state()
        assert saved["provider_retry_count"] == 1 and saved["provider_retry"] is not None
        assert saved["provider_wait_seconds"] == 1 and saved["elapsed_seconds"] == 0
        assert saved["provider_retry"]["wait_accounted_at"] == clock.now
        assert agent._active_start is None
        assert policy.http_attempts == 1
        agent.browser.act.assert_not_called()


def test_interrupted_wait_resume_counts_each_second_once_and_ignores_time_after_deadline(tmp_path, clock):
    policy = FlakySupervisor([transient(4)])
    with agent_for(tmp_path, policy) as first:
        clock.interrupt = True
        with pytest.raises(KeyboardInterrupt):
            list(first.run())
        pending = deepcopy(first.state["provider_retry"])
        assert first.state["provider_wait_seconds"] == 1
    clock.interrupt = False
    clock.now += 100
    reconnect = browser_for()
    with SupervisedAgent.resume("provider-recovery", state_dir=tmp_path, browser=reconnect,
                                supervisor=V2Supervisor(accepted_result), trace=False) as resumed:
        assert resumed.state["provider_retry"] == pending
        final = list(resumed.run())[-1]
        assert final["status"] == "done"
        assert final["provider_wait_seconds"] == 4 and final["elapsed_ms"] == 0
        assert final["provider_retry_count"] == 1
        reconnect.act.assert_not_called()
    assert sum(clock.sleeps) == 1


def test_interrupt_after_accounting_offline_time_keeps_checkpoint_atomic(tmp_path, clock, monkeypatch):
    policy = FlakySupervisor([transient(4)])
    with agent_for(tmp_path, policy) as first:
        first._decide()
    clock.now += 1
    with SupervisedAgent.resume("provider-recovery", state_dir=tmp_path, browser=browser_for(),
                                supervisor=V2Supervisor(accepted_result), trace=False) as resumed:
        emit = resumed.trace.emit

        def interrupt(event, *args, **kwargs):
            if event == "provider.retry_wait_started":
                raise KeyboardInterrupt()
            return emit(event, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(resumed.trace, "emit", interrupt)
            with pytest.raises(KeyboardInterrupt):
                list(resumed.run())
        saved = resumed.store.load_state()
        assert saved["provider_wait_seconds"] == 1
        assert saved["provider_retry"]["wait_accounted_at"] == clock.utc()
        assert saved["provider_retry"]["next_retry_at"] == clock.utc() + 3
        assert saved["elapsed_seconds"] == 0 and saved["supervisor_calls"] == 1
    clock.now += 1
    with SupervisedAgent.resume("provider-recovery", state_dir=tmp_path, browser=browser_for(),
                                supervisor=V2Supervisor(accepted_result), trace=False) as resumed_again:
        final = list(resumed_again.run())[-1]
        assert final["status"] == "done" and final["provider_wait_seconds"] == 4
        assert final["elapsed_ms"] == 0 and final["provider_retry_count"] == 1
        resumed_again.browser.act.assert_not_called()
    assert sum(clock.sleeps) == 2


def test_pending_snapshot_consumer_time_is_paused_before_wait_node(tmp_path, clock):
    policy = FlakySupervisor([transient(4)])
    with agent_for(tmp_path, policy) as agent:
        stream = agent.run()
        waiting = next(stream)
        assert waiting["status"] == "waiting_provider" and agent._active_start is None
        clock.now += 2
        final = list(stream)[-1]
        assert final["status"] == "done"
        assert final["provider_wait_seconds"] == 4 and final["elapsed_ms"] == 0
    assert sum(clock.sleeps) == 2


@pytest.mark.parametrize("utc_jump", [-600, -60, 60, 600])
def test_live_cooldown_uses_monotonic_duration_despite_utc_corrections(tmp_path, clock, utc_jump):
    policy = FlakySupervisor([transient(120)])
    clock.wall_jump = utc_jump
    with agent_for(tmp_path, policy) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "done"
        assert final["provider_wait_seconds"] == 120 and final["elapsed_ms"] == 0
        assert final["provider_retry_count"] == 1
        agent.browser.act.assert_not_called()
    assert sum(clock.sleeps) == 120


@pytest.mark.parametrize("utc_jump", [-600, 600])
def test_utc_jump_before_wait_node_does_not_reset_or_expire_live_timer(tmp_path, clock, utc_jump):
    policy = FlakySupervisor([transient(120)])
    with agent_for(tmp_path, policy) as agent:
        stream = agent.run()
        assert next(stream)["status"] == "waiting_provider"
        clock.wall_offset += utc_jump
        final = list(stream)[-1]
        assert final["status"] == "done" and final["provider_wait_seconds"] == 120
        assert final["elapsed_ms"] == 0
    assert sum(clock.sleeps) == 120


@pytest.mark.parametrize("utc_jump", [-600, 600])
def test_interrupted_clock_correction_persists_remaining_duration_for_resume(tmp_path, clock, utc_jump):
    policy = FlakySupervisor([transient(4)])
    with agent_for(tmp_path, policy) as first:
        clock.wall_jump = utc_jump
        clock.interrupt = True
        with pytest.raises(KeyboardInterrupt):
            list(first.run())
        saved = first.store.load_state()
        assert saved["provider_wait_seconds"] == 1 and saved["elapsed_seconds"] == 0
        assert saved["provider_retry"]["next_retry_at"] == clock.utc() + 3
        assert saved["provider_retry"]["wait_accounted_at"] == clock.utc()
    clock.interrupt = False
    clock.now += 1
    with SupervisedAgent.resume("provider-recovery", state_dir=tmp_path, browser=browser_for(),
                                supervisor=V2Supervisor(accepted_result), trace=False) as resumed:
        final = list(resumed.run())[-1]
        assert final["status"] == "done" and final["provider_retry_count"] == 1
        assert final["provider_wait_seconds"] == 4 and final["elapsed_ms"] == 0
        resumed.browser.act.assert_not_called()
    assert sum(clock.sleeps) == 3  # One second occurred while disconnected.


def test_offline_utc_rollback_conservatively_preserves_remaining_wait(tmp_path, clock):
    policy = FlakySupervisor([transient(4)])
    with agent_for(tmp_path, policy) as first:
        first._decide()
    clock.wall_offset -= 600
    with SupervisedAgent.resume("provider-recovery", state_dir=tmp_path, browser=browser_for(),
                                supervisor=V2Supervisor(accepted_result), trace=False) as resumed:
        final = list(resumed.run())[-1]
        assert final["status"] == "done" and final["provider_wait_seconds"] == 4
        assert final["elapsed_ms"] == 0 and final["provider_retry_count"] == 1
    assert sum(clock.sleeps) == 4


def test_legacy_pending_retry_is_migrated_without_refunding_old_active_time(tmp_path, clock):
    policy = FlakySupervisor([transient(4)])
    with agent_for(tmp_path, policy) as first:
        first._decide()
        first.state["provider_retry"].pop("wait_accounted_at")
        first.state.pop("provider_wait_seconds")
        first.state["elapsed_seconds"] = 30
        first._save()
    clock.now += 1
    with SupervisedAgent.resume("provider-recovery", state_dir=tmp_path, browser=browser_for(),
                                supervisor=V2Supervisor(accepted_result), trace=False) as resumed:
        final = list(resumed.run())[-1]
        assert final["status"] == "done"
        assert final["elapsed_ms"] == 30000 and final["provider_wait_seconds"] == 4
        assert final["provider_retry_count"] == 1


def test_wait_budget_cannot_be_exceeded_or_reset_when_scheduling(tmp_path, clock):
    policy = FlakySupervisor([transient(120)])
    with agent_for(tmp_path, policy) as agent:
        agent.state.update(provider_wait_seconds=500, provider_retry_count=1)
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_provider_wait_budget"
        assert final["provider_wait_seconds"] == 500 and final["provider_retry_count"] == 1
        assert final["provider_retry"] is None
    assert not clock.sleeps and policy.http_attempts == 1


def test_resumed_pending_wait_respects_remaining_global_wait_budget(tmp_path, clock):
    policy = FlakySupervisor([transient(120)])
    with agent_for(tmp_path, policy) as agent:
        agent._decide()
        agent.state["provider_wait_seconds"] = 500
        agent._wait_provider_retry()
        assert agent.state["stop_reason"] == "supervisor_provider_wait_budget"
        assert agent.state["provider_wait_seconds"] == 500 and agent.state["provider_retry_count"] == 1
        assert agent._active_start is None
    assert not clock.sleeps and policy.http_attempts == 1


def test_two_maximum_cooldowns_fit_global_wait_budget(tmp_path, clock):
    policy = FlakySupervisor([transient(300), transient(300)])
    with agent_for(tmp_path, policy, limits=SupervisorLimits(max_seconds=30)) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "done" and final["provider_wait_seconds"] == 600
        assert final["elapsed_ms"] == 0 and final["provider_retry_count"] == 2
    assert sum(clock.sleeps) == 600


def test_resumed_wait_budget_tolerates_utc_checkpoint_rounding(tmp_path, clock):
    policy = FlakySupervisor([])
    with agent_for(tmp_path, policy) as agent:
        agent.state.update(provider_retry_count=2, provider_wait_seconds=301.0123451, provider_retry={
            "next_retry_at": clock.utc() + 298.9876549, "wait_accounted_at": clock.utc(),
            "delay_seconds": 300, "phase": "initialize",
        })
        final = list(agent.run())[-1]
        assert final["status"] == "done" and final["provider_retry_count"] == 2
        assert final["provider_wait_seconds"] == 600
        assert final["elapsed_ms"] == 0
    assert sum(clock.sleeps) == pytest.approx(298.9876549)


def test_failed_post_cooldown_observation_stops_before_next_model_request(tmp_path, clock):
    policy = FlakySupervisor([transient(1)])
    with agent_for(tmp_path, policy) as agent:
        agent._decide()
        agent._active_start = clock.now
        agent.browser.observe.side_effect = RuntimeError("Original tab is gone")
        agent._wait_provider_retry()
        assert agent.state["status"] == "blocked" and agent.state["stop_reason"] == "observation_unavailable"
        assert policy.http_attempts == 1
        agent.browser.act.assert_not_called()


def test_fresh_observation_can_exhaust_remaining_active_budget(tmp_path, clock):
    policy = FlakySupervisor([transient(120)])
    with agent_for(tmp_path, policy, limits=SupervisorLimits(max_seconds=1)) as agent:
        observe = agent.browser.observe.side_effect

        def slow_observe(**kwargs):
            clock.now += 2
            return observe(**kwargs)

        agent.browser.observe.side_effect = slow_observe
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "time_budget" and final["elapsed_ms"] == 2000
        assert final["provider_wait_seconds"] == 120
        assert agent._active_start is None
        agent.browser.act.assert_not_called()
    assert policy.http_attempts == 1


@pytest.mark.parametrize(("pending", "count"), [
    ("invalid", 1), ({}, 1), ([], 1), ({"next_retry_at": "invalid", "phase": "initialize"}, 1),
    ({"next_retry_at": 2_000_000_010, "delay_seconds": 10, "phase": "initialize"}, True),
    ({"next_retry_at": 2_000_000_010, "delay_seconds": 10, "phase": "initialize"}, "1"),
    ({"next_retry_at": 2_000_000_010, "delay_seconds": 10, "phase": "initialize"}, -1),
    ({"next_retry_at": 2_000_000_010, "delay_seconds": 10, "phase": "initialize"}, 3),
    ({"next_retry_at": 2_000_000_010, "delay_seconds": 10, "phase": "review"}, 1),
])
def test_corrupt_retry_state_stops_without_reset_or_request(tmp_path, clock, pending, count):
    policy = FlakySupervisor([])
    with agent_for(tmp_path, policy) as agent:
        agent.state.update(provider_retry=pending, provider_retry_count=count)
        agent._wait_provider_retry()
        assert agent.state["stop_reason"] == "supervisor_provider_retry_state_invalid"
        assert agent.state["provider_retry_count"] == count
        assert policy.http_attempts == 0 and not clock.sleeps
        agent.browser.act.assert_not_called()


@pytest.mark.parametrize(("key", "value"), [
    ("next_retry_at", -1), ("next_retry_at", float("inf")), ("next_retry_at", float("nan")),
    ("next_retry_at", True), ("next_retry_at", 2_000_000_011),
    ("delay_seconds", -1), ("delay_seconds", True), ("delay_seconds", "10"),
    ("delay_seconds", float("nan")), ("delay_seconds", 301),
    ("wait_accounted_at", "invalid"), ("wait_accounted_at", float("nan")),
    ("wait_accounted_at", 1_999_999_999), ("wait_accounted_at", 2_000_000_011),
    ("wait_accounted_at", True),
])
def test_corrupt_pending_time_is_never_replaced_with_a_fresh_deadline(tmp_path, clock, key, value):
    policy = FlakySupervisor([])
    with agent_for(tmp_path, policy) as agent:
        pending = {"next_retry_at": clock.now + 10, "delay_seconds": 10,
                   "wait_accounted_at": clock.now, "phase": "initialize", key: value}
        agent.state.update(provider_retry=pending, provider_retry_count=1)
        agent._wait_provider_retry()
        assert agent.state["stop_reason"] == "supervisor_provider_retry_state_invalid"
        assert agent.state["provider_wait_seconds"] == 0
        assert policy.http_attempts == 0 and not clock.sleeps
        agent.browser.act.assert_not_called()


@pytest.mark.parametrize("value", [-1, True, "120", float("nan"), float("inf"), 601])
def test_corrupt_global_wait_counter_is_not_reset(tmp_path, clock, value):
    policy = FlakySupervisor([transient(1)])
    with agent_for(tmp_path, policy) as agent:
        agent.state["provider_wait_seconds"] = value
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_provider_retry_state_invalid"
        assert final["provider_retry_count"] == 0 and final["provider_retry"] is None
        assert not clock.sleeps
