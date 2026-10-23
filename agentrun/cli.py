"""Command-line entry points for the inspector and browser agent."""

import argparse
import sys

from AgentBrowser.usage import print_model_usage

from . import Agent, SupervisedAgent, SupervisorLimits
from .config import EnvironmentFileError, load_environment
from .console import run_console

_STATUSES = {"ready", "observing", "predicted", "done", "blocked", "unverified"}
_STOP_REASONS = {
    "inventory_exhausted", "extraction_incomplete", "window_limit", "decision_budget", "time_budget",
    "inspection_limit", "candidate_budget_exceeded", "budget_insufficient", "no_progress", "action_budget",
    "observation_not_ready", "verification_budget", "verification_no_progress", "repeated_state",
    "verification_invalid_response", "verification_unconfigured", "verification_provider_error",
    "verification_invalid_input", "verification_budget_insufficient",
}


def _progress_details(state: dict) -> str:
    """Describe window coverage separately from extraction and executed actions."""
    details = []
    if "model_calls" in state:
        details.append(
            f"{state['model_calls']} Jev decisions / {state.get('model_http_attempts', 0)} HTTP attempts"
            f" / {len(state.get('candidate_controls', []))} reconsultations"
            f" / {state.get('verification_calls', 0)} evidence reviews"
        )
    coverage = (state.get("page") or {}).get("candidate_coverage")
    if coverage:
        details.append(
            f"window {coverage['window_id']}: {coverage['nodes_admitted']} elements, "
            f"{coverage['actions_admitted']} actions, {coverage['pending_known']} collected actions pending"
        )
        window = "full inventory" if coverage["budget_complete"] else "partial inventory"
        extraction = "complete within scope" if coverage["extraction_complete"] else "incomplete"
        details.append(f"window: {window}; extraction: {extraction}")
    reason = state.get("stop_reason") or (coverage or {}).get("termination_reason")
    if reason:
        details.append(f"candidate stop: {reason if reason in _STOP_REASONS else 'unknown_limit'}")
    return "  " + "  ".join(details) if details else ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Jev-powered AgentRun browser agent.")
    parser.add_argument("--env-file", help="Load this dotenv file instead of the default environment files.")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="Run a natural-language browser task.")
    run.add_argument("--url", help="Initial browser URL.")
    run.add_argument("--goal", action="append", help="Repeat to combine ordered goals into one task.")
    run.add_argument("--supervised", action="store_true", help="Delegate subtasks through the persistent supervisor.")
    run.add_argument("--resume", metavar="RUN_ID", help="Resume an existing supervised run on its original tab.")
    run.add_argument("--feedback", help="Answer a pending supervisor question while resuming.")
    run.add_argument("--state-dir", help="Local directory for supervised run state.")
    run.add_argument("--close-tab", action="store_true", help="Close the working tab when the run ends.")
    run.add_argument("--no-input", action="store_true",
                     help="Leave supervised feedback paused instead of reading stdin.")
    run.add_argument("--no-trace", action="store_true", help="Disable local detailed traces for this session.")
    run.add_argument("--trace-dir", help="Root directory for local detailed traces.")
    for name in ("actions", "decisions", "windows", "supervisor-calls"):
        run.add_argument("--max-" + name, type=int,
                         help="Global limit for a new run, or an explicit increase when resuming.")
    run.add_argument("--max-seconds", type=float,
                      help="Active time limit for a new run, or an explicit increase when resuming.")
    run.add_argument("--env-file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    from .observability import add_trace_commands

    add_trace_commands(commands)
    return parser


def _trace_options(args):
    if args.no_trace:
        return {"trace": False}
    if args.trace_dir:
        from AgentBrowser.observability import TraceConfig

        return {"trace": TraceConfig(directory=args.trace_dir)}
    return {}


def _run(url: str, goals: list[str], *, close_tab: bool = False, **trace_options) -> int:
    final_status = None
    final_state = None
    verification = {}
    options = {"close_tab_on_exit": True} if close_tab else {}
    options.update(trace_options)
    with Agent(url, goals, **options) as agent:
        for state in agent.run():
            final_state = state
            status = state.get("status")
            final_status = status if status in _STATUSES else "unknown"
            verification = state.get("verification", {})
            elapsed = state.get("elapsed_ms", 0)
            elapsed = round(elapsed) if isinstance(elapsed, (int, float)) else 0
            print(
                f"{elapsed:>5} ms  {len(state.get('history', []))} actions  {final_status}"
                + _progress_details(state),
                flush=True,
            )
    print_model_usage(final_state.get("model_usage") if final_state else None)
    if final_status == "done":
        if verification.get("status") == "satisfied":
            print("Evidence review satisfied. Verify the final outcome independently.")
            return 0
        print("Jev reported DONE, but the result has not been verified.", file=sys.stderr)
        return 2
    if final_status == "unverified":
        print("AgentRun stopped: evidence is insufficient to verify completion.", file=sys.stderr)
        return 2
    if final_status == "blocked":
        print("AgentRun stopped: the task is blocked or its execution budget was reached.", file=sys.stderr)
        return 2
    print("AgentRun stopped without reporting completion.", file=sys.stderr)
    return 1


def _run_supervised(args) -> int:
    options = {"close_tab_on_exit": True} if args.close_tab else {}
    options.update(_trace_options(args))
    limit_values = {key: getattr(args, key) for key in (
        "max_actions", "max_decisions", "max_windows", "max_seconds", "max_supervisor_calls",
    ) if getattr(args, key, None) is not None}
    if limit_values:
        if args.resume:
            options["limit_overrides"] = limit_values
        else:
            options["limits"] = SupervisorLimits(**limit_values)
    if args.state_dir:
        options["state_dir"] = args.state_dir
    if args.resume:
        agent = SupervisedAgent.resume(args.resume, feedback=args.feedback, **options)
    else:
        agent = SupervisedAgent(args.url, "\n".join(args.goal), **options)
    with agent:
        result = run_console(agent, no_input=args.no_input, state_dir=args.state_dir, env_file=args.env_file)
    final = result.state
    if final is None:
        print("AgentRun stopped without reporting completion.", file=sys.stderr)
        return 1
    status = final.get("status", "unknown")
    elapsed = final.get("elapsed_ms", 0)
    elapsed = elapsed / 1000 if isinstance(elapsed, (int, float)) else 0
    provider_wait = final.get("provider_wait_seconds", 0)
    print(f"Run: {final.get('run_id', args.resume or 'unknown')} · Status: {status} "
          f"· Duration: {elapsed + provider_wait:.1f} s")
    if provider_wait:
        print(f"Active time: {elapsed:.1f} s · Provider wait: {provider_wait:.1f} s")
    if final.get("question") and not result.question_shown:
        print(final["question"])
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    """Load configuration, dispatch the command, and return a process exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "traces":
        from .observability import handle_trace_command

        return handle_trace_command(args)
    limits = {key: getattr(args, key) for key in (
        "max_actions", "max_decisions", "max_windows", "max_seconds", "max_supervisor_calls",
    ) if getattr(args, key, None) is not None}
    if limits:
        if not getattr(args, "supervised", False) and not getattr(args, "resume", None):
            parser.error("Budget options require a --supervised or --resume run.")
        try:
            SupervisorLimits(**limits)
        except ValueError as error:
            parser.error(str(error))
    if args.command == "run":
        if args.resume:
            if args.url or args.goal:
                parser.error("--resume uses the saved URL and goal; omit --url and --goal")
        elif not args.url or not args.goal:
            parser.error("new runs require --url and --goal")
        if args.feedback is not None and not args.resume:
            parser.error("--feedback requires --resume")
        if args.state_dir and not (args.supervised or args.resume):
            parser.error("--state-dir requires --supervised or --resume")
    try:
        load_environment(args.env_file)
        if args.command == "run":
            if args.supervised or args.resume:
                return _run_supervised(args)
            return _run(args.url, args.goal, close_tab=args.close_tab, **_trace_options(args))
        # Import after loading configuration: the inspector reads its port at import.
        from AgentBrowser import demo

        demo.main(environment_loaded=True)
        return 0
    except EnvironmentFileError as error:
        print(f"AgentRun configuration error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("AgentRun interrupted.", file=sys.stderr)
        return 130
    except Exception:
        # API and browser exception messages may include credentials or page content.
        print("AgentRun failed. Check browser connectivity and model environment settings.", file=sys.stderr)
        return 1
