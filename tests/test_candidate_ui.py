"""Offline consumer contracts for candidate windows and manual decision binding."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

from agentrun import cli

ROOT = Path(__file__).resolve().parents[1]


def coverage(**changes):
    return {
        "window_id": 2,
        "nodes_admitted": 1,
        "actions_admitted": 13,
        "presented_unique_actions": 24,
        "pending_known": 8,
        "budget_complete": False,
        "extraction_complete": False,
        "more_available": True,
        **changes,
    }


def test_cli_keeps_reconsultations_distinct_from_actions_and_reports_partial_coverage(monkeypatch, capsys):
    state = {
        "status": "blocked", "history": [], "elapsed_ms": 5,
        "model_calls": 4, "model_http_attempts": 5,
        "candidate_controls": [{"status": "advanced"}, {"status": "limited"}],
        "page": {"candidate_coverage": coverage()}, "stop_reason": "decision_budget",
    }
    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.run.return_value = iter([state])
    monkeypatch.setattr(cli, "Agent", Mock(return_value=instance))
    assert cli._run("https://example.test", ["Find the option"]) == 2
    output = capsys.readouterr()
    assert "0 actions  blocked" in output.out
    assert "4 Jev decisions / 5 HTTP attempts / 2 reconsultations" in output.out
    assert "window 2: 1 elements, 13 actions, 8 collected actions pending" in output.out
    assert "window: partial inventory; extraction: incomplete" in output.out
    assert "candidate stop: decision_budget" in output.out


def _inspector(script):
    """Exercise the real browser script against a small offline DOM/HTTP boundary."""
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
const requests = [];
const initialState = {page: null, status: 'idle', history: [], decision: null};
const context = {
  assert, requests, responseStates: [], Blob, setTimeout,
  document: {
    querySelector: () => ({content: 'test-token'}),
    querySelectorAll: () => [],
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        textContent: '', innerHTML: '', hidden: false, disabled: false, checked: false,
        listeners: {}, addEventListener(name, callback) {this.listeners[name] = callback;},
      });
      return elements.get(id);
    },
  },
  fetch: async (url, options) => {
    if (options) requests.push({url, body: JSON.parse(options.body)});
    const response = context.responseStates.shift() || initialState;
    return {ok: true, json: async () => response};
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('AgentBrowser/static/app.js', 'utf8'), context);
setImmediate(async () => {
  try { await vm.runInContext(SCRIPT, context); }
  catch (error) { console.error(error); process.exitCode = 1; }
});
""".replace("SCRIPT", json.dumps("(async () => {" + script + "})()"))
    result = subprocess.run(["node", "-e", harness], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_inspector_binds_manual_reconsultation_to_decision_and_exports_control_only_trace():
    script = """
state = {
  page: {fingerprint: 'page-now', title: 'Example', url: 'https://example.test', actions: [],
    candidate_coverage: COVERAGE},
  decision: {operation: 'MORE_CANDIDATES', choice: 'MORE_CANDIDATES', target: null,
    decision_id: 'd2', inventory_id: 'i1', window_id: 2, selection_digest: 'digest2',
    fingerprint: 'decision-dom', operation_probabilities: {MORE_CANDIDATES: 1}},
  status: 'predicted', elements: [], history: [], elapsed_ms: 0, model_calls: 2,
  candidate_controls: [{status: 'advanced'}], decisions: [],
};
render();
assert.equal($('candidate-window').textContent, 'Window 2');
assert.equal($('download').disabled, false);
assert.equal($('execute').textContent, 'Inspect more candidates');
assert.match($('candidate-scope').textContent, /Window includes part.*Page extraction incomplete/);
assert.equal($('confidence').textContent, '—');
await $('execute').listeners.click();
assert.equal(requests.length, 1);
assert.equal(requests[0].url, '/api/act');
assert.equal(JSON.stringify(requests[0].body), JSON.stringify({
  decision_id: 'd2', inventory_id: 'i1', window_id: 2,
  selection_digest: 'digest2', fingerprint: 'decision-dom',
}));
""".replace("COVERAGE", json.dumps(coverage()))
    _inspector(script)


def test_inspector_automatic_reconsultation_follows_server_budget_not_browser_action_count():
    _inspector("""
const base = {
  page: {title: 'Example', url: 'https://example.test', actions: []},
  status: 'ready', elements: [], history: [], elapsed_ms: 0, max_steps: 0,
};
state = {...base};
responseStates.push({...base}, {...base, status: 'blocked', stop_reason: 'decision_budget'});
await $('auto').listeners.click();
assert.equal(requests.length, 2);
assert.equal(requests.every(request => request.url === '/api/tick'), true);
assert.equal(state.status, 'blocked');
assert.equal(automatic, false);
""")
