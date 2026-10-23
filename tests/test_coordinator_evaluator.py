"""Isolated evaluator checks: no Chrome, model endpoints or external contact submissions."""

import json
import re
import sqlite3
import uuid
from collections import Counter
from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import pytest

from AgentBrowser.browser import fingerprint
from scripts import evaluate_coordinator as evaluation


def test_local_fixture_has_fourteen_distinct_items_across_two_pages_and_reorders_duplicates():
    fixture = evaluation.LocalFixture()
    first = fixture.listing({"category": ["eligible"], "page": ["1"]})
    second_read = fixture.listing({"category": ["eligible"], "page": ["1"]})
    second_page = fixture.listing({"category": ["eligible"], "page": ["2"]})
    def links(body):
        return re.findall(r'data-result href="/items/(\d+)"', body)
    assert len(links(first)) == 8 and len(set(links(first))) == 7
    assert links(first) != links(second_read)
    assert {int(item) for item in links(first) + links(second_page)} == set(range(1, 15))
    assert "Next page" in first and "End of results." not in first
    assert "Next page" not in second_page and "End of results." in second_page
    assert not links(fixture.listing({}))


def complete_fixture():
    fixture = evaluation.LocalFixture()
    observations = []
    for item in range(1, 15):
        assert fixture.submit(item, evaluation.MESSAGE)["accepted"]
        observations.append({"item_id": str(item), "accepted": True})
        number = 1 if item <= 7 else 2
        fixture.listing({"category": ["eligible"], "page": [str(number)]})
        observations.append({"page": str(number), "end": number == 2})
    return fixture, observations


def test_independent_oracle_requires_exactly_one_submission_confirmation_and_return_per_item():
    fixture, observations = complete_fixture()
    assert evaluation.local_outcome(fixture.snapshot(), observations)["verified"]
    ledger = fixture.snapshot()
    assert Counter(ledger["returns"]) == Counter(range(1, 15))
    fixture.submit(1, evaluation.MESSAGE)
    assert not evaluation.local_outcome(fixture.snapshot(), observations)["verified"]
    assert not evaluation.local_outcome(ledger, observations[:-2])["verified"]
    ledger["returns"].pop()
    assert not evaluation.local_outcome(ledger, observations)["verified"]


def test_invalid_local_submission_is_rejected_and_never_counts_as_success():
    fixture = evaluation.LocalFixture()
    assert not fixture.submit(1, "wrong message")["accepted"]
    assert not fixture.submit(15, evaluation.MESSAGE)["accepted"]
    assert fixture.snapshot()["submissions"] == []
    assert len(fixture.snapshot()["rejected"]) == 2


@pytest.mark.parametrize("action,text", [
    ({"kind": "fill", "label": "Message"}, "External message"),
    ({"kind": "click", "label": "Contactar"}, None),
    ({"kind": "click", "href": "https://external.test/items/1"}, None),
    ({"kind": "click", "href": "http://127.0.0.1:9998/items/1"}, None),
    ({"kind": "click", "href": "http://127.0.0.1:9999/admin"}, None),
])
def test_local_guard_rejects_external_origins_and_unapproved_inputs(action, text):
    origin = "http://127.0.0.1:9999/results"
    assert not evaluation.permitted_action("local", origin, action, {"url": origin}, text)


@pytest.mark.parametrize("kind", ["fill", "select", "click"])
def test_web_guard_rejects_contact_controls_and_all_form_values(kind):
    url = "https://www.coches.net/segunda-mano/"
    assert not evaluation.permitted_action("coches", url, {"kind": kind, "label": "Contactar"}, {"url": url}, "hello")
    assert not evaluation.permitted_action("coches", url, {"kind": kind, "label": "Enviar"}, {"url": url}, "hello")


def test_fake_model_uses_production_contract_tools():
    model = evaluation.FixtureToolModel()
    policy = evaluation.SupervisorPolicy(model=model)
    answer = policy.invoke({"contract_version": 2, "phase": "initialize", "goal": evaluation.LOCAL_GOAL})
    assert answer["contract_version"] == 2 and answer["phase"] == "initialize"
    assert answer["contract"]["loops"][0]["limit"] is None
    assert [step["verification_scope"] for step in answer["contract"]["loops"][0]["steps"]] == [
        "destination", "current",
    ]
    assert [tool.name for tool in model.tools_seen] == ["initialize_plan"]
    assert model.calls == 1


class FixtureParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.actions, self.text, self.attrs = [], [], []
        self.current = None
        self.label = ""
        self.in_label = False
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.attrs.append((tag, attrs))
        if tag == "label":
            self.in_label, self.label = True, ""
        if tag in {"a", "button", "input"} and "disabled" not in attrs:
            action = {"id": f"node-{len(self.actions) + 1}", "node": len(self.actions) + 1,
                      "kind": "fill" if tag == "input" else "click",
                      "role": {"a": "link", "input": "textbox", "button": "button"}[tag],
                      "label": self.label if tag == "input" else "", "value": attrs.get("value", "")}
            if tag == "a":
                action["href"] = attrs["href"]
            self.actions.append(action)
            if tag in {"a", "button"}:
                self.current = action

    def handle_endtag(self, tag):
        if tag in {"a", "button"}:
            self.current = None
        if tag == "label":
            self.in_label = False

    def handle_data(self, text):
        self.text.append(text)
        if self.in_label:
            self.label += text
        if self.current is not None:
            self.current["label"] += text


class LocalBrowser:
    """Drive the actual fixture handlers in memory, retaining independent service evidence."""

    def __init__(self):
        self.target = "coordinator-fixture-" + uuid.uuid4().hex
        self.fixture = evaluation.LocalFixture()
        self.url = "http://127.0.0.1:9999/results"
        self.inputs, self.observations, self.values = [], [], {}
        self.html = self.fixture.listing({})

    def observe(self, screenshot=False):
        parsed = FixtureParser(self.html)
        for action in parsed.actions:
            if action.get("href"):
                action["href"] = urljoin(self.url, action["href"])
            if action["kind"] == "fill":
                action["value"] = self.values.get(action["label"], action["value"])
        texts = " ".join(parsed.text)
        observed = {"url": self.url, "title": "Coordinator acceptance library", "text": texts,
                    "actions": parsed.actions, "scroll": {"y": 0}}
        observed["fingerprint"] = fingerprint(observed)
        attrs = {key: value for _, attrs in parsed.attrs for key, value in attrs.items() if key.startswith("data-")}
        self.observations.append({"url": self.url, "item_id": attrs.get("data-item-id"),
                                  "accepted": attrs.get("data-accepted") == "true", "page": attrs.get("data-page"),
                                  "end": "data-end" in attrs})
        return observed

    def fresh(self, page, action=None):
        return page["fingerprint"] == self.observe()["fingerprint"]

    def act(self, action, page, text=None):
        assert evaluation.permitted_action("local", "http://127.0.0.1:9999/results", action, page, text)
        self.inputs.append({"kind": action["kind"], "label": action.get("label"), "href": action.get("href")})
        if action["kind"] == "fill":
            self.values[action["label"]] = text
        elif action.get("href"):
            self.url = action["href"]
            parsed = urlparse(self.url)
            self.html = (self.fixture.detail(int(parsed.path.rsplit("/", 1)[-1])) if "/items/" in parsed.path
                         else self.fixture.listing(parse_qs(parsed.query)))
            self.values = {}
        elif action["label"] == "Apply filter":
            category = self.values["Category"]
            self.url = "http://127.0.0.1:9999/results?" + urlencode({"category": category})
            self.html = self.fixture.listing({"category": [category]})
            self.values = {}
        elif action["label"] == "Submit locally":
            item = int(urlparse(self.url).path.rsplit("/", 1)[-1])
            self.fixture.submit(item, self.values["Message"])
            self.html = self.fixture.detail(item)
        else:
            raise AssertionError("Unexpected fixture action")

    def disconnect(self):
        pass


@pytest.mark.parametrize("restart", [False, True])
def test_fourteen_items_use_strict_tools_complete_once_and_verify_independently(tmp_path, restart):
    browser = LocalBrowser()
    with evaluation.simulated_providers("local") as (supervisor, blocked):
        agent = evaluation.SupervisedAgent(browser.url, evaluation.LOCAL_GOAL, browser=browser, supervisor=supervisor,
                                          state_dir=tmp_path, limits=evaluation.limits())
        if restart:
            stream = agent.run()
            for _state in stream:
                if browser.fixture.snapshot()["submissions"]:
                    stream.close()
                    break
            run_id = agent.run_id
            agent.disconnect()
            agent = evaluation.SupervisedAgent.resume(
                run_id, browser=browser, supervisor=supervisor, state_dir=tmp_path,
            )
            assert agent.run_id == run_id and agent.browser.target == browser.target
        with agent:
            final = list(agent.run())[-1]
            assert final["status"] == "done", (final.get("stop_reason"), agent.state.get("validation_error"))
            outcome = evaluation.local_outcome(browser.fixture.snapshot(), browser.observations)
            assert outcome["verified"], outcome
            assert final["metrics"]["actions"] <= 180
            assert not any(mock.call_count for mock in blocked)
            assert len([action for action in browser.inputs if action["label"] == "Submit locally"]) == 14
            assert len({row["item_id"] for row in browser.fixture.snapshot()["submissions"]}) == 14


def test_evaluator_cli_requires_explicit_live_models_and_restricts_web_origin(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(evaluation, "load_environment", lambda: (_ for _ in ()).throw(AssertionError("No credentials")))
    monkeypatch.setattr(evaluation, "run_one", lambda output, **kwargs: calls.append(deepcopy(kwargs)) or {
        "verified": True, "status": "done", "stop_reason": None,
    })
    assert evaluation.main(["--scenario", "all", "--output", str(tmp_path)]) == 0
    assert len(calls) == 5 and [call["scenario"] for call in calls] == ["local"] * 3 + ["coches"] * 2
    assert all(call["live_models"] is False for call in calls)
    assert json.loads((tmp_path / "summary.json").read_text())["verified"]
    with pytest.raises(SystemExit):
        evaluation.main(["--url", "https://unrelated.test/"])


def saved_evaluation(tmp_path, *, live=True):
    source = tmp_path / "previous"
    source.mkdir()
    run_id = "original-fixture-run"
    origin = "http://127.0.0.1:50595/results"
    fixture = evaluation.LocalFixture()
    fixture.submit(1, evaluation.MESSAGE)
    metrics = {"actions": 5, "jev_calls": 8, "supervisor_calls": 4, "supervisor_http_attempts": 0,
               "jev_http_attempts": 0, "text_calls": 1, "candidate_windows": 8}
    report = {"scenario": "local", "models": "live" if live else "simulated-strict-tools", "run_id": run_id,
              "goal": evaluation.LOCAL_GOAL, "metrics": metrics, "allowed_actions": [{"kind": "click"}],
              "denied_actions": [], "blocked_model_attempts": 0}
    observations = [{"url": origin, "page": "1", "end": False},
                    {"url": "http://127.0.0.1:50595/items/1", "item_id": "1", "accepted": True}]
    for name, data in (("report", report), ("ledger", fixture.snapshot()), ("observations", observations),
                       ("trace", [{"status": "blocked", "metrics": metrics}])):
        (source / f"{name}.json").write_text(json.dumps(data))
    state = {"contract_version": 2, "run_id": run_id, "goal": evaluation.LOCAL_GOAL, "initial_url": origin,
             "target": "owned-original-tab", "limits": {"max_actions": 180, "max_supervisor_calls": 120},
             "supervisor_calls": 4, "supervisor_http_attempts": 0,
             "worker": {"model_calls": 8, "model_http_attempts": 0, "text_model_calls": 1, "candidate_windows": 8}}
    path = source / "state" / run_id / "state.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE supervisor_runs (run_id TEXT PRIMARY KEY, state_json TEXT)")
        connection.execute("CREATE TABLE supervisor_attempts (id TEXT, run_id TEXT, state TEXT)")
        connection.execute("INSERT INTO supervisor_runs VALUES (?,?)", (run_id, json.dumps(state)))
        attempts = [(str(index), run_id, "observed") for index in range(5)] + [("reject", run_id, "rejected")]
        connection.executemany("INSERT INTO supervisor_attempts VALUES (?,?,?)", attempts)
    return source


def test_resume_metadata_requires_explicit_live_mode_new_output_and_original_origin(tmp_path):
    source = saved_evaluation(tmp_path)
    with pytest.raises(ValueError, match="--live-models"):
        evaluation.load_resume_evaluation(source, tmp_path / "continued")
    with pytest.raises(ValueError, match="separate"):
        evaluation.load_resume_evaluation(source, source, live_models=True)
    saved = evaluation.load_resume_evaluation(source, tmp_path / "continued", live_models=True)
    assert saved["port"] == 50595
    assert saved["state"]["target"] == "owned-original-tab"
    fixture = evaluation.LocalFixture(ledger=saved["ledger"])
    assert fixture.pending_return == 1
    fixture.listing({"category": ["eligible"]})
    assert fixture.snapshot()["returns"] == [1]
    assert len(fixture.snapshot()["submissions"]) == 1
    assert saved["ledger"]["returns"] == []


def test_fixture_start_binds_exact_requested_loopback_port_without_reallocation(monkeypatch):
    server = Mock(server_port=50595)
    factory = Mock(return_value=server)
    monkeypatch.setattr(evaluation, "ThreadingHTTPServer", factory)
    fixture = evaluation.LocalFixture()
    fixture.start(port=50595)
    assert factory.call_args.args[0] == ("127.0.0.1", 50595)
    assert fixture.url == "http://127.0.0.1:50595/results"
    fixture.close()
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()


def test_resume_evaluator_retains_ledger_tab_run_budgets_and_original_artifacts(tmp_path, monkeypatch):
    source = saved_evaluation(tmp_path)
    original = {path: path.read_bytes() for path in source.glob("*.json")}
    captured = {}

    def start(fixture, port=0):
        captured["port"], captured["fixture"] = port, fixture
        fixture.server = SimpleNamespace(server_port=port)
        return fixture

    monkeypatch.setattr(evaluation.LocalFixture, "start", start)
    monkeypatch.setattr(evaluation.LocalFixture, "close", lambda _self: None)

    def attach(target, *, origin, observations, previous_report, fixture):
        captured.update(target=target, origin=origin, observations=observations)
        return SimpleNamespace(target=target, allowed_actions=deepcopy(previous_report["allowed_actions"]),
                               denied_actions=[], disconnect=Mock(), fixture=fixture)

    monkeypatch.setattr(evaluation.GuardedBrowser, "attach_evaluation", attach)

    class Agent:
        def __init__(self):
            self.final = {"status": "done", "metrics": {"actions": 58, "jev_calls": 86, "supervisor_calls": 32}}

        def run(self):
            fixture = captured["fixture"]
            assert fixture.pending_return == 1
            fixture.listing({"category": ["eligible"], "page": ["1"]})
            for item in range(2, 15):
                fixture.submit(item, evaluation.MESSAGE)
                captured["observations"].append({"url": f"http://127.0.0.1:50595/items/{item}",
                                                  "item_id": str(item), "accepted": True})
                number = 1 if item <= 7 else 2
                fixture.listing({"category": ["eligible"], "page": [str(number)]})
                captured["observations"].append({"url": captured["origin"], "page": str(number), "end": number == 2})
            yield self.final

        def snapshot(self):
            return self.final

        def disconnect(self):
            pass

    resume = Mock(return_value=Agent())
    monkeypatch.setattr(evaluation.SupervisedAgent, "resume", resume)
    output = tmp_path / "continued"
    report = evaluation.resume_evaluation(source, output, live_models=True)
    assert report["verified"] and report["same_original_tab"] and report["same_fixture_origin"]
    assert captured["port"] == 50595 and captured["target"] == "owned-original-tab"
    assert resume.call_args.args == ("original-fixture-run",)
    assert set(resume.call_args.kwargs) == {"state_dir", "browser"}
    assert resume.call_args.kwargs["state_dir"] == source / "state"
    assert report["run_id"] == "original-fixture-run" and report["initial_submission_count"] == 1
    assert report["resume_start_metrics"] == json.loads((source / "report.json").read_text())["metrics"]
    assert len(json.loads((output / "ledger.json").read_text())["submissions"]) == 14
    assert len(json.loads((output / "trace.json").read_text())) == 2
    assert all(path.read_bytes() == data for path, data in original.items())


def test_resume_guard_blocks_previously_recorded_submit_before_browser_input(monkeypatch):
    fixture = evaluation.LocalFixture()
    fixture.submit(1, evaluation.MESSAGE)
    browser = evaluation.GuardedBrowser.__new__(evaluation.GuardedBrowser)
    browser.scenario, browser.origin, browser.fixture = "local", "http://127.0.0.1:50595/results", fixture
    browser.allowed_actions, browser.denied_actions = [], []
    act = Mock(side_effect=AssertionError("Input must never dispatch"))
    monkeypatch.setattr(evaluation.Browser, "act", act)
    with pytest.raises(AssertionError, match="guard rejected"):
        browser.act({"kind": "click", "label": "Submit locally"}, {"url": "http://127.0.0.1:50595/items/1"})
    act.assert_not_called()
    assert len(browser.denied_actions) == 1 and not browser.allowed_actions


def test_resume_cli_requires_separate_output_before_any_provider_loading(tmp_path, monkeypatch):
    source = saved_evaluation(tmp_path)
    env = Mock(side_effect=AssertionError("Do not load providers for rejected arguments"))
    monkeypatch.setattr(evaluation, "load_environment", env)
    with pytest.raises(SystemExit):
        evaluation.main(["--resume-evaluation", str(source), "--live-models"])
    with pytest.raises(SystemExit):
        evaluation.main(["--resume-evaluation", str(source), "--output", str(tmp_path / "continued")])
    env.assert_not_called()


def test_web_oracle_requires_applied_initial_filters_and_three_returns_to_filtered_results():
    links = [{"href": f"https://www.coches.net/bmw-z4-{item}-covo.aspx", "result_card": True}
             for item in range(1, 4)]
    listing = {"url": evaluation.COCHES_URL, "title": "BMW Z4 en Madrid", "anchors": links,
               "headings": [{"text": "14 Coches BMW Z4 en Madrid", "source": "search_heading"}]}
    observations = [listing]
    for link in links:
        observations.extend([{"url": link["href"], "title": "BMW Z4 2.0i", "text": "Precio 19.990 €"}, listing])
    result = evaluation.coches_outcome(observations)
    assert result["verified"] and result["filters_provided_by_initial_url"]
    assert all(value["status"] == "verified" for value in result["filter_outcomes"].values())
    assert result["filter_outcomes"]["price_max"]["evidence"]["source"] == "explicit URL upper bound"
    unfiltered = deepcopy(observations)
    for row in unfiltered:
        if not evaluation.detail_id(row["url"]):
            row["url"] = "https://www.coches.net/segunda-mano/"
    assert not evaluation.coches_outcome(unfiltered)["verified"]
    missing_heading = deepcopy(observations)
    for row in missing_heading:
        row.pop("headings", None)
    assert not evaluation.coches_outcome(missing_heading)["verified"]


@pytest.mark.parametrize("advanced", ["actions", "supervisor_calls", "jev_calls"])
def test_resume_rejects_stale_ledger_before_binding_and_accepts_latest_artifacts(tmp_path, monkeypatch, advanced):
    source = saved_evaluation(tmp_path)
    report = json.loads((source / "report.json").read_text())
    checkpoint = source / "state" / report["run_id"] / "state.sqlite"
    with sqlite3.connect(checkpoint) as connection:
        state = json.loads(connection.execute("SELECT state_json FROM supervisor_runs").fetchone()[0])
        if advanced == "actions":
            connection.execute("INSERT INTO supervisor_attempts VALUES (?,?,?)", ("new", report["run_id"], "observed"))
        elif advanced == "supervisor_calls":
            state["supervisor_calls"] += 1
        else:
            state["worker"]["model_calls"] += 1
        connection.execute("UPDATE supervisor_runs SET state_json=?", (json.dumps(state),))
    bind = Mock(side_effect=AssertionError("Stale source must fail before binding"))
    attach = Mock(side_effect=AssertionError("Stale source must fail before browser access"))
    monkeypatch.setattr(evaluation.LocalFixture, "start", bind)
    monkeypatch.setattr(evaluation.GuardedBrowser, "attach_evaluation", attach)
    with pytest.raises(ValueError, match="Stale evaluation artifacts"):
        evaluation.resume_evaluation(source, tmp_path / "rejected-output", live_models=True)
    bind.assert_not_called()
    attach.assert_not_called()
    assert not (tmp_path / "rejected-output").exists()

    latest = tmp_path / "latest"
    latest.mkdir()
    for name in ("ledger", "observations", "trace"):
        (latest / f"{name}.json").write_bytes((source / f"{name}.json").read_bytes())
    report["metrics"][advanced] += 1
    report["state_dir"] = str(source / "state")
    (latest / "report.json").write_text(json.dumps(report))
    restored = evaluation.load_resume_evaluation(latest, tmp_path / "accepted-output", live_models=True)
    assert restored["state_dir"] == source / "state"
    assert restored["report"]["metrics"][advanced] == report["metrics"][advanced]
