"""Wait for human feedback without releasing a supervised browser session."""

import logging
import shlex
from dataclasses import dataclass

from AgentBrowser.usage import print_model_usage

GLOBAL_BUDGET_REASONS = {"action_budget", "decision_budget", "window_limit", "time_budget", "supervisor_budget"}


@dataclass(frozen=True)
class ConsoleResult:
    state: dict | None
    exit_code: int
    question_shown: bool = False


class _ConsoleQuestion(logging.Filter):
    def filter(self, record):
        # The console presents this exact question once when it can accept an
        # answer. All action, delegation and diagnostic logs remain unchanged.
        return record.msg != "Feedback necesario: %s"


def _resume_hint(state, *, state_dir=None, env_file=None):
    if not state or not state.get("run_id"):
        return
    command = ["uv", "run", "agentrun", "run", "--resume", state["run_id"]]
    if state_dir:
        command.extend(["--state-dir", str(state_dir)])
    if env_file:
        command.extend(["--env-file", str(env_file)])
    command.extend(["--feedback", "tu respuesta"])
    print("Para continuar: " + shlex.join(command), flush=True)


def _budget_exhausted(agent, state):
    if state.get("stop_reason") in GLOBAL_BUDGET_REASONS:
        return True
    limits = getattr(agent, "limits", None)
    metrics = state.get("metrics", {})
    for key, limit_name in (
        ("actions", "max_actions"), ("jev_calls", "max_decisions"),
        ("candidate_windows", "max_windows"), ("supervisor_calls", "max_supervisor_calls"),
    ):
        limit = getattr(limits, limit_name, None)
        used = metrics.get(key)
        if type(limit) in (int, float) and type(used) in (int, float) and used >= limit:
            return True
    seconds = getattr(limits, "max_seconds", None)
    elapsed_ms = state.get("elapsed_ms")
    return (type(seconds) in (int, float) and type(elapsed_ms) in (int, float)
            and elapsed_ms >= seconds * 1000)


def run_console(agent, *, no_input=False, state_dir=None, env_file=None):
    """Run and answer pauses in place. The caller owns the surrounding context."""
    final = None
    question_shown = False
    try:
        while True:
            final = None
            question_shown = False
            logger = logging.getLogger("agentrun")
            question_filter = _ConsoleQuestion()
            logger.addFilter(question_filter)
            try:
                for final in agent.run():
                    pass
            finally:
                logger.removeFilter(question_filter)
            if final is None:
                return ConsoleResult(None, 1)
            if final.get("status") != "waiting_feedback":
                print_model_usage(final.get("model_usage"))
                successful = (final.get("status") == "done"
                              and final.get("verification", {}).get("status") == "satisfied")
                return ConsoleResult(final, 0 if successful else 2)
            if final.get("question"):
                print(final["question"], flush=True)
                question_shown = True
            if _budget_exhausted(agent, final):
                print("El feedback no restablece los límites de esta ejecución.", flush=True)
                print_model_usage(final.get("model_usage"))
                return ConsoleResult(final, 2, question_shown)
            if no_input:
                print_model_usage(final.get("model_usage"))
                _resume_hint(final, state_dir=state_dir, env_file=env_file)
                return ConsoleResult(final, 2, question_shown)
            while True:
                try:
                    answer = input("Tu respuesta: ").strip()
                except (EOFError, OSError):
                    print_model_usage(final.get("model_usage"))
                    _resume_hint(final, state_dir=state_dir, env_file=env_file)
                    return ConsoleResult(final, 2, question_shown)
                if len(answer) > 4000:
                    print("La respuesta admite un máximo de 4000 caracteres. Escribe una más breve.", flush=True)
                    continue
                if answer:
                    break
                print("Escribe una respuesta para continuar.", flush=True)
            logger.addFilter(question_filter)
            try:
                agent.submit_feedback(answer)
            finally:
                logger.removeFilter(question_filter)
    except KeyboardInterrupt:
        print("Ejecución interrumpida.", flush=True)
        if final is None:
            final = agent.snapshot()
        print_model_usage(final.get("model_usage"))
        _resume_hint(final, state_dir=state_dir, env_file=env_file)
        return ConsoleResult(final, 130, question_shown)
