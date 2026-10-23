"""Leaving a task releases control, and only explicit requests close its tab."""

from copy import deepcopy
from unittest.mock import Mock, call

import pytest
from test_agent import page

from AgentBrowser import agent as loop
from AgentBrowser import browser, demo


@pytest.fixture
def owned_browser(monkeypatch):
    instance = Mock()
    instance.observe.return_value = deepcopy(page())
    monkeypatch.setattr(loop, "Browser", Mock(return_value=instance))
    return instance


@pytest.mark.parametrize("status", ["done", "blocked", "unverified"])
def test_context_releases_control_and_keeps_tab_for_every_terminal_status(owned_browser, status):
    with loop.Agent("https://example.test", "Inspect a page", verification=False) as agent:
        agent.state["status"] = status
    owned_browser.disconnect.assert_called_once_with()
    owned_browser.close.assert_not_called()


@pytest.mark.parametrize("error", [RuntimeError("failure"), KeyboardInterrupt()])
@pytest.mark.parametrize("close_tab", [False, True])
def test_context_obeys_explicit_close_option_even_on_errors(owned_browser, error, close_tab):
    with pytest.raises(type(error)) as caught:
        with loop.Agent("https://example.test", "Inspect", verification=False, close_tab_on_exit=close_tab):
            raise error
    assert caught.value is error
    (owned_browser.close if close_tab else owned_browser.disconnect).assert_called_once_with()
    (owned_browser.disconnect if close_tab else owned_browser.close).assert_not_called()


def test_explicit_close_on_success_closes_tab(owned_browser):
    with loop.Agent("https://example.test", "Inspect", verification=False, close_tab_on_exit=True):
        pass
    owned_browser.close.assert_called_once_with()
    owned_browser.disconnect.assert_not_called()


@pytest.mark.parametrize("close_tab", [False, True])
@pytest.mark.parametrize("stage", ["observe", "install"])
def test_initial_observation_failure_obeys_tab_preference(owned_browser, monkeypatch, close_tab, stage):
    error = RuntimeError("Failed to observe")
    if stage == "observe":
        owned_browser.observe.side_effect = error
    else:
        monkeypatch.setattr(loop.Agent, "_install_page", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        loop.Agent("https://example.test", "Inspect", verification=False, close_tab_on_exit=close_tab)
    assert caught.value is error
    (owned_browser.close if close_tab else owned_browser.disconnect).assert_called_once_with()
    (owned_browser.disconnect if close_tab else owned_browser.close).assert_not_called()


@pytest.fixture
def cdp_session(monkeypatch):
    instance = browser.Browser.__new__(browser.Browser)
    instance.target, instance.session = "owned-tab", "owned-session"
    transport = Mock(return_value={})
    monkeypatch.setattr(browser, "cdp", transport)
    return instance, transport


def test_disconnect_is_idempotent_and_close_remains_explicit(cdp_session):
    instance, transport = cdp_session
    instance.disconnect()
    instance.disconnect()
    transport.assert_called_once_with("Target.detachFromTarget", sessionId="owned-session")
    assert instance.target == "owned-tab" and instance.session is None
    instance.close()
    instance.close()
    assert transport.call_args_list == [
        call("Target.detachFromTarget", sessionId="owned-session"),
        call("Target.closeTarget", targetId="owned-tab"),
    ]
    assert instance.target is None and instance.session is None


@pytest.mark.parametrize("operation", ["call", "observe", "act"])
def test_disconnected_instance_never_falls_back_to_another_tab(cdp_session, monkeypatch, operation):
    instance, transport = cdp_session
    instance.disconnect()
    transport.reset_mock()
    raw_operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", raw_operation)
    with pytest.raises(RuntimeError, match="disconnected"):
        if operation == "call":
            instance.call("Runtime.evaluate", expression="document.title")
        elif operation == "observe":
            instance.observe(screenshot=False)
        else:
            instance.fresh = Mock(return_value=True)
            instance.act({"id": "wait", "kind": "wait"}, {})
    transport.assert_not_called()
    raw_operation.assert_not_called()


def test_lost_session_cleanup_does_not_reconnect_or_close_the_tab(cdp_session):
    instance, transport = cdp_session
    transport.side_effect = RuntimeError("Session with given id not found.")
    instance.disconnect()
    instance.disconnect()
    transport.assert_called_once_with("Target.detachFromTarget", sessionId="owned-session")
    assert instance.target == "owned-tab" and instance.session is None


@pytest.mark.parametrize("close_tab", [False, True])
def test_inspector_cleanup_preserves_tab_unless_explicitly_requested(monkeypatch, close_tab):
    agent = Mock()
    monkeypatch.setattr(demo, "AGENT", agent)
    demo.close_browser(close_tab=close_tab)
    (agent.close if close_tab else agent.disconnect).assert_called_once_with()
    (agent.disconnect if close_tab else agent.close).assert_not_called()
    assert demo.AGENT is None
