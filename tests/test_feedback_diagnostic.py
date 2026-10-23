"""Offline coverage of the explicit real-browser feedback diagnostic."""

import json
import sqlite3
from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import page

from scripts import check_supervised_feedback as diagnostic


@pytest.mark.parametrize("answer", ["Search", None])
def test_diagnostic_uses_isolated_state_and_readonly_source_checkpoint(tmp_path, monkeypatch, answer):
    source_dir = tmp_path / "original"
    source_run = "original-run"
    source_path = source_dir / source_run / "state.sqlite"
    source_path.parent.mkdir(parents=True)
    original = {"run_id": source_run, "target": "existing-tab", "status": "waiting_feedback", "private": "not-copied"}
    with sqlite3.connect(source_path) as db:
        db.execute("CREATE TABLE supervisor_runs (run_id TEXT PRIMARY KEY, state_json TEXT)")
        db.execute("INSERT INTO supervisor_runs VALUES (?,?)", (source_run, json.dumps(original)))
    before = diagnostic.checkpoint_digest(source_path)
    browser = Mock(target="existing-tab", denied_inputs=0)
    browser.observe.side_effect = lambda **_: deepcopy(page())
    browser.evaluate.return_value = {"url": page()["url"], "title": "Search", "private": "not-copied"}
    attach = Mock(return_value=browser)
    monkeypatch.setattr(diagnostic.ReadOnlyBrowser, "attach", attach)
    read = Mock(return_value=answer) if answer is not None else Mock(side_effect=EOFError)
    monkeypatch.setattr("builtins.input", read)
    output = tmp_path / "diagnostic"
    exit_code = diagnostic.main([
        "--run-id", source_run, "--source-state-dir", str(source_dir), "--output", str(output),
    ])
    assert exit_code == (0 if answer else 2)
    report = json.loads((output / "report.json").read_text())
    assert report["verified"] is bool(answer)
    assert report["status"] == ("done" if answer else "waiting_feedback")
    assert report["diagnostic_run_id"] != source_run
    assert report["source_checkpoint_unchanged"]
    assert diagnostic.checkpoint_digest(source_path) == before
    assert report["same_run_through_feedback"] and report["same_tab_through_feedback"]
    assert report["waiting_excluded_from_active_time"]
    assert report["model_calls"] == report["blocked_model_attempts"] == 0
    assert report["browser_inputs"] == report["blocked_browser_inputs"] == 0
    assert report["simulated_supervisor_calls"] == (2 if answer else 1)
    assert report["feedback_received"] == (1 if answer else 0)
    assert "not-copied" not in (output / "report.json").read_text()
    attach.assert_called_once_with("existing-tab")
    browser.act.assert_not_called()
    browser.close.assert_not_called()


def test_diagnostic_blocks_every_browser_mutation():
    browser = diagnostic.ReadOnlyBrowser.__new__(diagnostic.ReadOnlyBrowser)
    with pytest.raises(AssertionError, match="forbids all browser inputs"):
        browser.act({"kind": "click"}, page())
    assert browser.denied_inputs == 1


def test_source_run_path_cannot_escape_state_directory(tmp_path):
    with pytest.raises(ValueError, match="Invalid source run ID"):
        diagnostic.source_target("../../other", tmp_path)
