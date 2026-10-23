"""Offline CLI dispatch, exit status, and browser lifetime contracts."""

import json
from unittest.mock import Mock

import pytest

import agentrun
from agentrun import cli
from examples import flights
from AgentBrowser import Agent, demo


@pytest.fixture
def environment(monkeypatch):
    loader = Mock()
    monkeypatch.setattr(cli, "load_environment", loader)
    return loader


def runner(monkeypatch, statuses=("ready", "done"), error=None):
    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    if error:
        instance.run.side_effect = error
    else:
        instance.run.return_value = iter(
            {"status": status, "elapsed_ms": index * 10, "history": [],
             "verification": {"status": "satisfied" if status == "done" else "pending"}}
            for index, status in enumerate(statuses)
        )
    constructor = Mock(return_value=instance)
    monkeypatch.setattr(cli, "Agent", constructor)
    return constructor, instance


def test_public_namespace_reuses_the_langgraph_agent():
    assert agentrun.Agent is Agent


def test_default_command_loads_environment_before_inspector(environment, monkeypatch):
    events = []
    environment.side_effect = lambda path: events.append(("load", path))
    inspector = Mock(side_effect=lambda **kwargs: events.append(("inspect", kwargs)))
    monkeypatch.setattr(demo, "main", inspector)
    assert cli.main([]) == 0
    assert events == [("load", None), ("inspect", {"environment_loaded": True})]


@pytest.mark.parametrize("placement", ["before", "after"])
def test_run_passes_url_goals_and_explicit_environment(environment, monkeypatch, capsys, placement):
    constructor, instance = runner(monkeypatch)
    args = ["run", "--url", "https://example.test", "--goal", "Find a book", "--goal", "Open its details"]
    flag = ["--env-file", "custom.env"]
    assert cli.main(flag + args if placement == "before" else args + flag) == 0
    environment.assert_called_once_with("custom.env")
    constructor.assert_called_once_with("https://example.test", ["Find a book", "Open its details"])
    instance.__exit__.assert_called_once_with(None, None, None)
    output = capsys.readouterr()
    assert "ready" in output.out and "done" in output.out
    assert "Verify the final outcome independently" in output.out


def test_run_can_explicitly_close_the_working_tab(environment, monkeypatch):
    constructor, instance = runner(monkeypatch)
    assert cli.main(["run", "--url", "https://example.test", "--goal", "Find a book", "--close-tab"]) == 0
    constructor.assert_called_once_with("https://example.test", ["Find a book"], close_tab_on_exit=True)
    instance.__exit__.assert_called_once_with(None, None, None)


def test_blocked_run_is_nonzero_and_exits_browser_context(environment, monkeypatch, capsys):
    _, instance = runner(monkeypatch, statuses=("blocked",))
    assert cli.main(["run", "--url", "https://example.test", "--goal", "Find a book"]) == 2
    instance.__exit__.assert_called_once_with(None, None, None)
    assert "blocked" in capsys.readouterr().err


@pytest.mark.parametrize("failure,code", [(RuntimeError("secret-token"), 1), (KeyboardInterrupt(), 130)])
def test_errors_exit_browser_context_without_echoing_secrets(environment, monkeypatch, capsys, failure, code):
    _, instance = runner(monkeypatch, error=failure)
    assert cli.main(["run", "--url", "https://example.test", "--goal", "Find a book"]) == code
    assert instance.__exit__.call_args.args[:2] == (type(failure), failure)
    output = capsys.readouterr()
    assert "secret-token" not in output.out + output.err


def test_run_without_terminal_status_fails(environment, monkeypatch, capsys):
    _, instance = runner(monkeypatch, statuses=())
    assert cli.main(["run", "--url", "https://example.test", "--goal", "Find a book"]) == 1
    instance.__exit__.assert_called_once()
    assert "without reporting completion" in capsys.readouterr().err


def test_missing_environment_prevents_startup(environment, monkeypatch, capsys):
    constructor, _ = runner(monkeypatch)
    environment.side_effect = cli.EnvironmentFileError("Environment file does not exist.")
    assert cli.main(["run", "--url", "https://example.test", "--goal", "Find a book"]) == 2
    constructor.assert_not_called()
    assert "configuration error" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["run"], ["run", "--url", "https://example.test"], ["run", "--goal", "Search"]])
def test_incomplete_run_arguments_exit_before_loading_environment(environment, args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    environment.assert_not_called()


@pytest.mark.parametrize("flags,close_tab", [([], False), (["--keep-open"], False), (["--close-tab"], True)])
@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_flights_releases_connection_and_only_closes_when_requested(
    tmp_path, monkeypatch, flags, close_tab, snapshot_fails,
):
    agent = Mock()
    agent.run.return_value = iter([])
    agent.browser.target = "working-tab"
    agent.browser.session = "connected-session"
    agent.snapshot.return_value = {"page": {}}
    if snapshot_fails:
        agent.snapshot.side_effect = RuntimeError("Snapshot unavailable")

    def disconnected():
        agent.browser.session = None

    agent.disconnect.side_effect = disconnected
    constructor = Mock(return_value=agent)
    monkeypatch.setattr(flights, "Agent", constructor)
    monkeypatch.setattr(flights, "verify", Mock(return_value={"passed": True}))
    args = ["--output", str(tmp_path), *flags]
    if snapshot_fails:
        with pytest.raises(RuntimeError, match="Snapshot unavailable"):
            flights.main(args)
    else:
        flights.main(args)
        assert json.loads((tmp_path / "session.json").read_text()) == {
            "target": "working-tab", "session": "connected-session",
        }
    options = {"close_tab_on_exit": True} if close_tab else {}
    constructor.assert_called_once_with(flights.URL, flights.GOALS, **options)
    if close_tab:
        agent.close.assert_called_once_with()
        agent.disconnect.assert_not_called()
    else:
        agent.disconnect.assert_called_once_with()
        agent.close.assert_not_called()
