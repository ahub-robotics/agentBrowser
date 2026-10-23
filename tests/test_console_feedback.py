"""Console feedback waits in place without model calls, restarts or new tabs."""

import logging
import shlex
from copy import deepcopy
from unittest.mock import Mock

import pytest

from agentrun.console import GLOBAL_BUDGET_REASONS, run_console
from agentrun.supervised import SupervisorLimits


def paused(**kwargs):
    return {
        "run_id": "saved-1", "status": "waiting_feedback", "stop_reason": "supervisor_feedback",
        "question": "¿Qué dato falta?", "elapsed_ms": 2000,
        "metrics": {"supervisor_calls": 3, "actions": 4}, **kwargs,
    }


class ConsoleAgent:
    def __init__(self, states):
        self.states = states
        self.current = states[0]
        self.calls = 0
        self.feedback = []
        self.running = False
        self.entered = False
        self.exit_count = 0

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_):
        self.entered = False
        self.exit_count += 1

    def run(self):
        assert self.entered
        self.running = True
        self.current = self.states[self.calls]
        self.calls += 1
        try:
            if self.current.get("question"):
                logging.getLogger("agentrun").info("Feedback necesario: %s", self.current["question"])
            yield deepcopy(self.current)
        finally:
            self.running = False

    def submit_feedback(self, text):
        assert self.entered and not self.running
        self.feedback.append(text)

    def snapshot(self):
        return deepcopy(self.current)


@pytest.mark.parametrize("log_level", [logging.INFO, logging.WARNING])
def test_console_waits_in_same_context_and_reprompts_empty_input_without_running(
    monkeypatch, capsys, caplog, log_level,
):
    caplog.set_level(log_level, logger="agentrun")
    agent = ConsoleAgent([paused(), {"status": "done", "verification": {"status": "satisfied"}}])
    answers = iter(["", "   ", "  Usa el dato del objetivo.  "])

    def answer(_prompt):
        assert agent.entered and not agent.running
        assert agent.calls == 1 and agent.feedback == []
        assert agent.current["elapsed_ms"] == 2000
        assert agent.current["metrics"] == {"supervisor_calls": 3, "actions": 4}
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    with agent:
        result = run_console(agent)
    assert result.exit_code == 0 and result.state["status"] == "done"
    assert agent.calls == 2 and agent.feedback == ["Usa el dato del objetivo."]
    assert agent.exit_count == 1
    output = capsys.readouterr().out
    assert output.count("¿Qué dato falta?") == 1
    assert output.count("Escribe una respuesta para continuar.") == 2
    assert "¿Qué dato falta?" not in caplog.text
    assert not logging.getLogger("agentrun").filters


@pytest.mark.parametrize("error", [EOFError(), OSError("stdin is unavailable")])
def test_console_eof_keeps_pause_and_prints_usable_resume_command(monkeypatch, capsys, error):
    agent = ConsoleAgent([paused()])
    monkeypatch.setattr("builtins.input", Mock(side_effect=error))
    with agent:
        result = run_console(agent, state_dir="state folder", env_file="environment file.env")
    assert result.exit_code == 2 and result.state["status"] == "waiting_feedback"
    assert agent.feedback == [] and agent.calls == 1
    command = capsys.readouterr().out.split("Para continuar: ", 1)[1].strip()
    assert shlex.split(command) == [
        "uv", "run", "agentrun", "run", "--resume", "saved-1", "--state-dir", "state folder",
        "--env-file", "environment file.env", "--feedback", "tu respuesta",
    ]


def test_console_no_input_never_reads_stdin(monkeypatch, capsys):
    agent = ConsoleAgent([paused()])
    read = Mock(side_effect=AssertionError("Must not ask for input"))
    monkeypatch.setattr("builtins.input", read)
    with agent:
        result = run_console(agent, no_input=True)
    assert result.exit_code == 2 and result.question_shown
    read.assert_not_called()
    assert agent.feedback == [] and agent.calls == 1
    assert "--resume saved-1" in capsys.readouterr().out


@pytest.mark.parametrize("reason", sorted(GLOBAL_BUDGET_REASONS))
def test_console_does_not_wait_or_retry_an_exhausted_global_budget(monkeypatch, capsys, reason):
    agent = ConsoleAgent([paused(stop_reason=reason)])
    read = Mock(side_effect=AssertionError("Feedback cannot replenish budgets"))
    monkeypatch.setattr("builtins.input", read)
    with agent:
        result = run_console(agent)
    assert result.exit_code == 2
    assert agent.feedback == [] and agent.calls == 1
    read.assert_not_called()
    assert "no restablece los límites" in capsys.readouterr().out


@pytest.mark.parametrize("limit, state", [
    ({"max_supervisor_calls": 3}, paused()),
    ({"max_actions": 4}, paused()),
    ({"max_seconds": 2}, paused()),
    ({"max_decisions": 7}, paused(metrics={"jev_calls": 7})),
    ({"max_windows": 6}, paused(metrics={"candidate_windows": 6})),
])
def test_console_rejects_feedback_when_last_call_spent_budget_even_if_reason_is_feedback(
    monkeypatch, limit, state,
):
    agent = ConsoleAgent([state])
    agent.limits = SupervisorLimits(**limit)
    read = Mock(side_effect=AssertionError("Budget was spent by the last call"))
    monkeypatch.setattr("builtins.input", read)
    with agent:
        result = run_console(agent)
    assert result.exit_code == 2 and agent.calls == 1 and agent.feedback == []
    read.assert_not_called()


@pytest.mark.parametrize("interrupt_during", ["input", "run"])
def test_console_interrupt_returns_130_and_releases_context_once(monkeypatch, capsys, interrupt_during):
    agent = ConsoleAgent([paused()])
    if interrupt_during == "input":
        monkeypatch.setattr("builtins.input", Mock(side_effect=KeyboardInterrupt))
    else:
        agent.run = Mock(side_effect=KeyboardInterrupt)
    with agent:
        result = run_console(agent)
    assert result.exit_code == 130
    assert agent.exit_count == 1 and agent.feedback == []
    assert "--resume saved-1" in capsys.readouterr().out
    assert not logging.getLogger("agentrun").filters


def test_console_supports_multiple_distinct_feedback_rounds(monkeypatch, capsys):
    agent = ConsoleAgent([
        paused(question="Primera pregunta"), paused(question="Segunda pregunta", elapsed_ms=3000),
        {"status": "done", "verification": {"status": "satisfied"}},
    ])
    read = Mock(side_effect=["Primera respuesta", "Segunda respuesta"])
    monkeypatch.setattr("builtins.input", read)
    with agent:
        result = run_console(agent)
    assert result.exit_code == 0 and agent.calls == 3
    assert agent.feedback == ["Primera respuesta", "Segunda respuesta"]
    output = capsys.readouterr().out
    assert output.count("Primera pregunta") == output.count("Segunda pregunta") == 1


def test_console_reprompts_long_feedback_without_submitting_or_running(monkeypatch, capsys):
    agent = ConsoleAgent([paused(), {"status": "done", "verification": {"status": "satisfied"}}])
    answers = iter(["a" * 4001, "Una respuesta breve."])

    def answer(_prompt):
        assert agent.calls == 1 and agent.feedback == [] and agent.entered
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    with agent:
        result = run_console(agent)
    assert result.exit_code == 0
    assert agent.feedback == ["Una respuesta breve."]
    assert agent.calls == 2
    output = capsys.readouterr().out
    assert "máximo de 4000 caracteres" in output
    assert "a" * 4001 not in output
