"""Budget options apply to new runs and may extend saved runs explicitly."""

from unittest.mock import Mock

import pytest

from agentrun import cli
from agentrun.supervisor_policy import SupervisorPolicy


@pytest.mark.parametrize("option", [
    "--max-actions", "--max-decisions", "--max-windows", "--max-seconds", "--max-supervisor-calls",
])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_invalid_budgets_fail_before_loading_credentials(monkeypatch, option, value):
    load = Mock()
    monkeypatch.setattr(cli, "load_environment", load)
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "--supervised", "--url", "https://example.test", "--goal", "Inspect", option, value])
    assert error.value.code == 2
    load.assert_not_called()


def test_resume_forwards_explicit_budget_increases(monkeypatch):
    monkeypatch.setattr(cli, "load_environment", Mock())
    agent = Mock()
    agent.__enter__ = Mock(return_value=agent)
    agent.__exit__ = Mock(return_value=False)
    agent.run.return_value = iter([{"run_id": "saved", "status": "blocked", "elapsed_ms": 0}])
    resume = Mock(return_value=agent)
    monkeypatch.setattr(cli.SupervisedAgent, "resume", resume)
    cli.main(["run", "--resume", "saved", "--max-actions", "240", "--max-seconds", "1200"])
    assert resume.call_args.kwargs["limit_overrides"] == {"max_actions": 240, "max_seconds": 1200.0}


def test_resume_budgets_only_increase_saved_limits():
    from dataclasses import asdict

    from agentrun.supervised import SupervisorLimits, raise_saved_limits

    saved = asdict(SupervisorLimits(max_actions=60, max_seconds=300))
    raised = raise_saved_limits(saved, {"max_actions": 240, "max_seconds": 1200})
    assert (raised.max_actions, raised.max_seconds) == (240, 1200)
    with pytest.raises(ValueError, match="only raise"):
        raise_saved_limits(saved, {"max_actions": 30})


def test_explicit_evaluation_limits_reach_new_supervised_run(monkeypatch):
    monkeypatch.setattr(cli, "load_environment", Mock())
    agent = Mock()
    agent.__enter__ = Mock(return_value=agent)
    agent.__exit__ = Mock(return_value=False)
    agent.run.return_value = iter([{
        "run_id": "new", "status": "done", "elapsed_ms": 0, "verification": {"status": "satisfied"},
    }])
    constructor = Mock(return_value=agent)
    monkeypatch.setattr(cli, "SupervisedAgent", constructor)
    assert cli.main([
        "run", "--supervised", "--url", "https://example.test", "--goal", "Inspect",
        "--max-actions", "180", "--max-decisions", "360", "--max-windows", "360",
        "--max-seconds", "900", "--max-supervisor-calls", "120",
    ]) == 0
    limits = constructor.call_args.kwargs["limits"]
    assert (limits.max_actions, limits.max_decisions, limits.max_windows,
            limits.max_seconds, limits.max_supervisor_calls) == (180, 360, 360, 900, 120)


def test_delta_reviews_do_not_reserve_plan_sized_completions():
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="offline", api_key="offline-placeholder")
    policy = SupervisorPolicy(model=model)
    policy._build(contract_version=2, phase="initialize")
    assert model.max_tokens == 4096
    policy._build(contract_version=2, phase="audit")
    assert model.max_tokens == 2048
    policy._build(contract_version=2, phase="review")
    assert model.max_tokens == 2048
    assert policy.http_attempts == 0
