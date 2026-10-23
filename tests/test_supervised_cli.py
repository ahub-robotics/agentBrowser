"""Supervised entrypoints use one persistent run and never invoke a provider here."""

from unittest.mock import Mock

import pytest

from agentrun import cli
from scripts import evaluate_supervised_coches as evaluation


@pytest.fixture
def configured(monkeypatch):
    load = Mock()
    monkeypatch.setattr(cli, "load_environment", load)
    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.return_value = iter([
        {"run_id": "saved-1", "status": "ready", "elapsed_ms": 20},
        {"run_id": "saved-1", "status": "done", "elapsed_ms": 100,
         "verification": {"status": "satisfied"}},
    ])
    constructor = Mock(return_value=instance)
    constructor.resume.return_value = instance
    monkeypatch.setattr(cli, "SupervisedAgent", constructor)
    return load, constructor, instance


def test_new_supervised_run_combines_goal_once_and_prints_only_final(configured, capsys):
    _, constructor, instance = configured
    assert cli.main(["run", "--supervised", "--url", "https://example.test",
                     "--goal", "Inspect items", "--goal", "Collect their names"]) == 0
    constructor.assert_called_once_with("https://example.test", "Inspect items\nCollect their names")
    constructor.resume.assert_not_called()
    instance.__exit__.assert_called_once_with(None, None, None)
    assert capsys.readouterr().out == "Run: saved-1 · Status: done · Duration: 0.1 s\n"


def test_resume_uses_saved_run_and_feedback(configured, tmp_path):
    _, constructor, _ = configured
    assert cli.main(["run", "--resume", "saved-1", "--feedback", "Only the first three",
                     "--state-dir", str(tmp_path), "--close-tab"]) == 0
    constructor.assert_not_called()
    constructor.resume.assert_called_once_with(
        "saved-1", feedback="Only the first three", state_dir=str(tmp_path), close_tab_on_exit=True,
    )


@pytest.mark.parametrize("status", ["waiting_feedback", "browser_unavailable", "blocked", "unverified"])
def test_partial_states_remain_non_successful_and_show_question(configured, capsys, status):
    _, _, instance = configured
    instance.run.return_value = iter([{
        "run_id": "saved-1", "status": status, "elapsed_ms": 100, "question": "Which item should continue?",
    }])
    assert cli.main(["run", "--resume", "saved-1"]) == 2
    output = capsys.readouterr().out
    assert status in output and "Which item should continue?" in output


def test_done_without_evidence_does_not_succeed(configured):
    _, _, instance = configured
    instance.run.return_value = iter([{"run_id": "saved-1", "status": "done"}])
    assert cli.main(["run", "--resume", "saved-1"]) == 2


@pytest.mark.parametrize("arguments", [
    ["--resume", "run-1", "--goal", "Replace the goal"],
    ["--resume", "run-1", "--url", "https://other.test"],
    ["--url", "https://example.test", "--goal", "Inspect", "--feedback", "Continue"],
    ["--url", "https://example.test", "--goal", "Inspect", "--state-dir", "/tmp/state"],
    ["--supervised", "--goal", "Inspect"],
])
def test_invalid_argument_combinations_never_start(configured, arguments):
    load, constructor, _ = configured
    with pytest.raises(SystemExit) as caught:
        cli.main(["run", *arguments])
    assert caught.value.code == 2
    load.assert_not_called()
    constructor.assert_not_called()
    constructor.resume.assert_not_called()


def advertisement(number):
    return f"https://www.coches.net/bmw-z4-gasolina-2007-madrid-{number}-covo.aspx"


def listing():
    return {"url": evaluation.URL, "anchors": [
        {"href": advertisement(number), "result_card": True} for number in range(1, 4)
    ]}


def filled(number):
    return {"url": advertisement(number), "controls": [{"label": "Mensaje", "value": evaluation.MESSAGE}]}


def test_live_oracle_requires_three_distinct_fills_and_three_returns():
    observations = [listing(), filled(1), listing(), filled(1), listing(), filled(2), listing()]
    assert evaluation.outcome(observations)["status"] == "unknown"
    observations.append(filled(3))
    assert evaluation.outcome(observations)["status"] == "unknown"
    observations.append(listing())
    assert evaluation.outcome(observations)["status"] == "verified"


@pytest.mark.parametrize("action,text", [
    ({"kind": "click", "label": "Contactar", "submits_form": False}, None),
    ({"kind": "click", "label": "Enviar mensaje", "submits_form": True}, None),
    ({"kind": "fill", "label": "Email"}, "someone@example.test"),
    ({"kind": "fill", "label": "Mensaje"}, "Unapproved text"),
    ({"kind": "select", "label": "Contacto"}, None),
])
def test_live_guard_never_dispatches_contact_send_or_unapproved_input(monkeypatch, action, text):
    browser = object.__new__(evaluation.GuardedBrowser)
    browser.allowed_actions, browser.denied_actions = [], []
    mutate = Mock()
    monkeypatch.setattr(evaluation.Browser, "act", mutate)
    with pytest.raises(AssertionError, match="forbids"):
        browser.act(action, {"url": advertisement(1)}, text)
    mutate.assert_not_called()
    assert len(browser.denied_actions) == 1


def test_live_guard_allows_only_the_expected_message_fill(monkeypatch):
    browser = object.__new__(evaluation.GuardedBrowser)
    browser.allowed_actions, browser.denied_actions = [], []
    mutate = Mock(return_value={"executed": "field"})
    monkeypatch.setattr(evaluation.Browser, "act", mutate)
    action = {"kind": "fill", "label": "Mensaje", "id": "field"}
    page = {"url": advertisement(1)}
    assert browser.act(action, page, evaluation.MESSAGE) == {"executed": "field"}
    mutate.assert_called_once_with(action, page, evaluation.MESSAGE)


def test_observed_advertiser_textarea_is_filled_without_clicking_contact():
    field = {"kind": "fill", "role": "textbox", "label": "Contacta con el anunciante",
             "id": "message", "node": 10, "value": ""}
    submit = {"kind": "click", "role": "button", "label": "Contactar", "submits_form": True,
              "id": "submit", "node": 11, "value": ""}
    page = {"url": advertisement(1), "actions": [field, submit]}
    choice = evaluation.choose_action(
        page, "Open the observed advertisement and fill its message.\n" + advertisement(1), [],
    )
    assert choice["choice"] == "message" and choice["target"] == "1"
    assert not evaluation.is_message(submit)
    assert evaluation.outcome([listing(), *[
        {"url": advertisement(number),
         "controls": [{"label": "Contacta con el anunciante", "value": evaluation.MESSAGE}]}
        if index % 2 == 0 else listing()
        for number in range(1, 4) for index in range(2)
    ]])["status"] == "verified"


def test_live_oracle_requires_the_advertisement_to_come_from_an_observed_result_card():
    observations = [filled(number) if index % 2 == 0 else listing()
                    for number in range(1, 4) for index in range(2)]
    assert evaluation.outcome(observations)["status"] == "unknown"


def test_policy_waits_for_acknowledged_navigation_without_repeating_click():
    link = {"id": "ad", "node": 1, "kind": "click", "label": "Ad", "href": advertisement(1)}
    wait = {"id": "wait", "kind": "wait", "label": "Wait"}
    page = {"url": evaluation.URL, "actions": [link, wait]}
    history = [{"kind": "click", "href": advertisement(1), "url": evaluation.URL}]
    result = evaluation.choose_action(
        page, "Open the observed advertisement and fill its message.\n" + advertisement(1), history,
    )
    assert result["choice"] == "wait"


def test_live_runner_does_not_claim_resume_without_a_verified_fill(tmp_path, monkeypatch):
    import json

    browser = Mock(target="owned-tab", allowed_actions=[], denied_actions=[])
    monkeypatch.setattr(evaluation, "GuardedBrowser", Mock(return_value=browser))
    agent = Mock(run_id="partial-run")
    agent.run.return_value = iter([{"run_id": "partial-run", "status": "waiting_feedback"}])
    constructor = Mock(return_value=agent)
    monkeypatch.setattr(evaluation, "SupervisedAgent", constructor)
    attach = Mock()
    monkeypatch.setattr(evaluation.Browser, "attach", attach)
    assert evaluation.main(["--output", str(tmp_path)]) == 2
    attach.assert_not_called()
    constructor.resume.assert_not_called()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["resumed_same_tab"] is report["interrupted_after_verified_fill"] is False


def test_live_runner_blocks_a_real_supervisor_if_injection_regresses(tmp_path, monkeypatch):
    import json

    browser = Mock(target="owned-tab", allowed_actions=[], denied_actions=[])
    monkeypatch.setattr(evaluation, "GuardedBrowser", Mock(return_value=browser))
    real_provider = Mock(side_effect=AssertionError("This provider must never be reached"))
    monkeypatch.setattr(evaluation.SupervisorPolicy, "_build", real_provider)

    def forgotten_injection(*args, **kwargs):
        return evaluation.SupervisorPolicy().invoke({})

    monkeypatch.setattr(evaluation, "SupervisedAgent", forgotten_injection)
    assert evaluation.main(["--output", str(tmp_path)]) == 2
    real_provider.assert_not_called()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["error"] == {"type": "AssertionError", "message": "Paid supervisor calls are forbidden"}
    assert report["model_http_calls"] == 0


def test_legacy_acceptance_goal_never_imports_the_contact_loop():
    from scripts import evaluate_coches

    assert "first advertisement" in evaluate_coches.GOAL
    assert "contact" not in evaluate_coches.GOAL.casefold()


def test_direct_example_remains_available(monkeypatch, capsys):
    import test as example

    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.return_value = iter([{"status": "done", "elapsed_ms": 25}])
    direct, supervisor = Mock(return_value=instance), Mock()
    monkeypatch.setattr(example, "DirectAgent", direct)
    monkeypatch.setattr(example, "Agent", supervisor)
    monkeypatch.setattr(example, "load_environment", Mock())
    example.main(["--direct"])
    direct.assert_called_once_with("https://www.coches.net", example.GOAL, max_actions=300)
    supervisor.assert_not_called()
    output = capsys.readouterr().out
    assert "AgentRun · ejecución" in output and "done" in output and "0.0 s" in output


@pytest.mark.parametrize("status, verification, expected", [
    ("done", {"status": "satisfied"}, 0),
    ("done", {"status": "disabled"}, 2),
    ("done", {}, 2),
    ("waiting_feedback", {"status": "satisfied"}, 2),
    ("blocked", {"status": "incomplete"}, 2),
    ("unverified", {}, 2),
])
def test_live_example_exit_code_requires_done_and_verified(monkeypatch, capsys, status, verification, expected):
    import test as example

    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.return_value = iter([{
        "status": status, "verification": verification, "elapsed_ms": 250,
        "run_id": "saved-example", "question": "Pending clarification",
    }])
    monkeypatch.setattr(example, "Agent", Mock(return_value=instance))
    monkeypatch.setattr(example, "load_environment", Mock())
    assert example.main(["--no-input"]) == expected
    output = capsys.readouterr().out
    if status == "waiting_feedback":
        assert "Pending clarification" in output
        assert "Para continuar: uv run agentrun run --resume saved-example --feedback 'tu respuesta'" in output
        assert "AgentRun · ejecución" in output and status in output and "0.2 s" in output
        assert "saved-example" in output
    else:
        assert "AgentRun · ejecución" in output and status in output and "0.2 s" in output
        assert "saved-example" in output and "Pending clarification" in output
    instance.__exit__.assert_called_once_with(None, None, None)


def test_live_example_without_final_state_returns_error(monkeypatch, capsys):
    import test as example

    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.return_value = iter([])
    monkeypatch.setattr(example, "Agent", Mock(return_value=instance))
    monkeypatch.setattr(example, "load_environment", Mock())
    assert example.main([]) == 1
    assert capsys.readouterr().out == ""


def test_live_example_main_propagates_unsuccessful_exit_code(monkeypatch):
    import runpy
    from pathlib import Path

    import agentrun
    from AgentBrowser import demo

    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.return_value = iter([{"status": "waiting_feedback", "elapsed_ms": 10}])
    monkeypatch.setattr(agentrun, "SupervisedAgent", Mock(return_value=instance))
    monkeypatch.setattr(demo, "load_environment", Mock())
    monkeypatch.setattr("sys.argv", ["test.py"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "test.py"), run_name="__main__")
    assert caught.value.code == 2


@pytest.mark.parametrize("entrypoint", ["cli", "example"])
def test_supervised_entrypoints_continue_feedback_on_same_open_instance(monkeypatch, capsys, entrypoint):
    import test as example

    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.side_effect = [
        iter([{"status": "waiting_feedback", "run_id": "saved-1", "elapsed_ms": 200,
               "question": "Indica el dato pendiente."}]),
        iter([{"status": "done", "run_id": "saved-1", "elapsed_ms": 300,
               "verification": {"status": "satisfied"}}]),
    ]
    constructor = Mock(return_value=instance)
    module, name = (cli, "SupervisedAgent") if entrypoint == "cli" else (example, "Agent")
    monkeypatch.setattr(module, name, constructor)
    monkeypatch.setattr(module, "load_environment", Mock())

    def answer(_prompt):
        instance.__enter__.assert_called_once_with()
        instance.__exit__.assert_not_called()
        assert instance.run.call_count == 1
        return "El dato solicitado."

    monkeypatch.setattr("builtins.input", answer)
    arguments = ["run", "--supervised", "--url", "https://example.test", "--goal", "Inspect"]
    assert module.main(arguments if entrypoint == "cli" else []) == 0
    instance.submit_feedback.assert_called_once_with("El dato solicitado.")
    assert instance.run.call_count == 2
    constructor.resume.assert_not_called()
    instance.__exit__.assert_called_once_with(None, None, None)
    assert capsys.readouterr().out.count("Indica el dato pendiente.") == 1


@pytest.mark.parametrize("entrypoint", ["cli", "example"])
@pytest.mark.parametrize("close_tab", [False, True])
def test_console_interrupt_preserves_entrypoint_close_preference(monkeypatch, entrypoint, close_tab):
    import test as example

    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.return_value = iter([{
        "status": "waiting_feedback", "run_id": "saved-1", "elapsed_ms": 200, "question": "¿Qué falta?",
    }])
    constructor = Mock(return_value=instance)
    module, name = (cli, "SupervisedAgent") if entrypoint == "cli" else (example, "Agent")
    monkeypatch.setattr(module, name, constructor)
    monkeypatch.setattr(module, "load_environment", Mock())
    monkeypatch.setattr("builtins.input", Mock(side_effect=KeyboardInterrupt))
    arguments = ["run", "--supervised", "--url", "https://example.test", "--goal", "Inspect"]
    arguments = arguments if entrypoint == "cli" else []
    assert module.main(arguments + (["--close-tab"] if close_tab else [])) == 130
    if entrypoint == "example":
        from agentrun import SupervisorLimits

        expected = {"limits": SupervisorLimits(max_actions=300)}
    else:
        expected = {}
    if close_tab:
        expected["close_tab_on_exit"] = True
    assert constructor.call_args.kwargs == expected
    instance.submit_feedback.assert_not_called()
    instance.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("reshuffle", [False, True])
def test_acceptance_policy_runs_three_items_across_persistent_resume(tmp_path, monkeypatch, reshuffle):
    """Exercise the exact live scripted policies offline before opening Chrome."""
    from AgentBrowser.browser import fingerprint

    class Browser:
        target = "evaluation-tab"

        def __init__(self):
            self.current = None
            self.values = {}
            self.inputs = []
            self.observations = []
            self.returns = 0

        def observe(self, screenshot=False):
            if self.current is None:
                start = 1 + 3 * self.returns if reshuffle else 1
                actions = [
                    {"id": f"ad-{number}", "node": number, "kind": "click", "role": "link",
                     "label": f"Advertisement {number}", "href": advertisement(number), "value": ""}
                    for number in range(start, start + 3)
                ]
                observed = {"url": evaluation.URL, "anchors": [
                    {"href": advertisement(number), "result_card": True} for number in range(start, start + 3)
                ]}
                url = evaluation.URL
            else:
                url = advertisement(self.current)
                value = self.values.get(self.current, "")
                actions = [
                    {"id": "message", "node": 10, "kind": "fill", "role": "textbox",
                     "label": "Mensaje", "value": value},
                    {"id": "back", "kind": "back", "label": "Back to listing",
                     "href": evaluation.URL, "history_entry": 1, "current_history_entry": 2},
                ]
                observed = {"url": url, "controls": [{"label": "Mensaje", "value": value}]}
            self.observations.append(observed)
            page = {"url": url, "title": "Advertisement evaluation", "text": "Observed advertisements",
                    "actions": actions, "scroll": {"y": 0}, "marker": str((url, self.values)),
                    "page_key": [1, url], "guards": {}}
            page["fingerprint"] = fingerprint(page)
            return page

        def fresh(self, page, action=None):
            return True

        def act(self, action, page, text=None):
            self.inputs.append(action["kind"])
            if action["kind"] == "click":
                self.current = int(action["id"].split("-")[1])
            elif action["kind"] == "fill":
                self.values[self.current] = text
            elif action["kind"] == "back":
                self.current = None
                self.returns += 1

        def disconnect(self):
            pass

    browser = Browser()
    failures = []
    original_run_subtask = evaluation.worker_module.Agent.run_subtask

    def checked_subtask(self, *args, **kwargs):
        try:
            yield from original_run_subtask(self, *args, **kwargs)
        except Exception as error:
            failures.append(repr(error))
            raise

    monkeypatch.setattr(evaluation.worker_module.Agent, "run_subtask", checked_subtask)
    monkeypatch.setattr(evaluation.worker_module, "choose", evaluation.ScriptedPolicy())
    monkeypatch.setattr(evaluation.worker_module, "field_text", lambda _: (
        evaluation.MESSAGE, {"model": "simulated", "latency_ms": 0, "usage": {}},
    ))
    agent = evaluation.SupervisedAgent(
        evaluation.URL, evaluation.GOAL, browser=browser,
        supervisor=evaluation.ScriptedSupervisor(), state_dir=tmp_path,
    )
    stream = agent.run()
    for final in stream:
        if browser.values:
            stream.close()
            break
    agent.disconnect()
    resumed = evaluation.SupervisedAgent.resume(
        final["run_id"], browser=browser, supervisor=evaluation.ScriptedSupervisor(), state_dir=tmp_path,
    )
    with resumed:
        states = list(resumed.run())
    assert states[-1]["status"] == "done", failures
    assert evaluation.outcome(browser.observations)["status"] == "verified"
    assert browser.inputs == ["click", "fill", "back"] * 3
    assert set(browser.values) == ({1, 4, 7} if reshuffle else {1, 2, 3})
    assert states[-1]["metrics"]["text_calls"] == 3
