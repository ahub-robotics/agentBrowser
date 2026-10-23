"""Pure acceptance-oracle tests; these never open Chrome or call paid APIs."""

import json

import pytest

from AgentBrowser import model
from scripts import evaluate_coches as evaluation


@pytest.fixture(autouse=True)
def prohibit_paid_apis(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Live provider access is forbidden in acceptance-oracle unit tests")

    monkeypatch.setattr(model.CLIENT, "post", forbidden)


@pytest.fixture
def search():
    return {
        "url": "https://www.coches.net/search/?MakeIds%5B0%5D=7&ModelIds%5B0%5D=291&MaxPrice=20000",
        "title": "Search", "text": "BMW Z4 Madrid 20.000 €",
        "filter_summaries": [
            {"text": "BMW Z4", "source": "applied_filter"},
            {"text": "Madrid", "source": "applied_filter"},
        ],
        "headings": [], "controls": [],
        "anchors": [
            {"href": "https://www.coches.net/advertising", "text": "Banner", "result_card": False},
            {"href": "https://www.coches.net/bmw-z4-12345678-covo.aspx", "text": "BMW Z4", "result_card": True},
            {"href": "https://www.coches.net/bmw-z4-12345678-covo.aspx", "text": "Photo", "result_card": True},
            {"href": "https://www.coches.net/bmw-z4-98765432-covo.aspx", "text": "Another", "result_card": True},
        ],
    }


def test_applied_filters_and_first_observed_advertisement_are_independently_verified(search):
    final = {"url": "https://www.coches.net/bmw-z4-12345678-covo.aspx?tracking=one"}
    result = evaluation.evaluate_outcomes([search, final])
    assert result["status"] == "verified"
    assert all(item["status"] == "verified" for item in result["criteria"].values())


def test_matching_ad_text_does_not_prove_filters(search):
    search["filter_summaries"] = []
    search["url"] = "https://www.coches.net/search/"
    search["text"] = "BMW Z4 in Madrid for 20.000 €"
    result = evaluation.evaluate_outcomes([search, {"url": search["anchors"][1]["href"]}])
    assert result["status"] == "unknown"
    assert result["criteria"]["bmw_z4"]["status"] == "unknown"
    assert result["criteria"]["madrid"]["status"] == "unknown"
    assert result["criteria"]["price_max"]["status"] == "unknown"


def test_unknown_province_id_is_not_assumed_to_be_madrid(search):
    search["filter_summaries"] = [search["filter_summaries"][0]]
    search["url"] += "&ProvinceIds%5B0%5D=28"
    result = evaluation.filter_outcomes(search, [search])
    assert result["madrid"]["status"] == "unknown"


def test_selected_madrid_control_teaches_mapping_but_pending_selection_does_not_prove_applied(search):
    search["filter_summaries"] = [search["filter_summaries"][0]]
    panel = {"url": "https://www.coches.net/", "controls": [
        {"label": "Madrid", "pressed": "true", "in_panel": True,
         "related_ids": [{"id": "28", "name": "28", "value": ""}]},
    ]}
    assert evaluation.filter_outcomes(search, [panel])["madrid"]["status"] == "unknown"
    search["url"] += "&ProvinceIds%5B0%5D=28"
    assert evaluation.filter_outcomes(search, [panel, search])["madrid"]["status"] == "verified"


def test_unselected_option_does_not_teach_a_province_mapping(search):
    search["filter_summaries"] = [search["filter_summaries"][0]]
    search["url"] += "&ProvinceIds%5B0%5D=28"
    panel = {"controls": [{"label": "Madrid", "pressed": "false", "id": "28"}]}
    assert evaluation.filter_outcomes(search, [panel, search])["madrid"]["status"] == "unknown"


def test_multiple_provinces_do_not_pass_as_madrid_only(search):
    search["filter_summaries"] = [search["filter_summaries"][0]]
    search["url"] += "&ProvinceIds%5B0%5D=8&ProvinceIds%5B1%5D=28"
    panel = {"controls": [{"label": "Madrid", "pressed": "true", "id": "28"}]}
    assert evaluation.query_values(search)["provinceids"] == ["8", "28"]
    assert evaluation.filter_outcomes(search, [panel, search])["madrid"]["status"] == "unknown"


def test_model_ids_require_an_observed_mapping_and_bmw_scope(search):
    search["filter_summaries"] = [{"text": "BMW", "source": "applied_filter"}]
    assert evaluation.filter_outcomes(search, [search])["bmw_z4"]["status"] == "unknown"
    panel = {"controls": [{"label": "Z4", "pressed": "true", "related_ids": [{"id": "291"}]}]}
    assert evaluation.filter_outcomes(search, [panel, search])["bmw_z4"]["status"] == "verified"
    search["filter_summaries"] = []
    assert evaluation.filter_outcomes(search, [panel, search])["bmw_z4"]["status"] == "unknown"


@pytest.mark.parametrize("parameter", ["MinPrice=20000", "Price=20000", "MaxPrice=200000", "MaxPrice=19999"])
def test_other_price_or_wrong_upper_limit_does_not_pass(search, parameter):
    search["url"] = "https://www.coches.net/search/?" + parameter
    assert evaluation.filter_outcomes(search, [search])["price_max"]["status"] != "verified"


def test_explicit_applied_upper_bound_text_proves_price_without_guessed_parameter(search):
    search["url"] = "https://www.coches.net/search/"
    search["filter_summaries"].append({"text": "Hasta 20.000 €", "source": "applied_filter"})
    assert evaluation.filter_outcomes(search, [search])["price_max"]["status"] == "verified"


def test_opening_second_ad_is_a_failure_even_when_filters_are_correct(search):
    result = evaluation.evaluate_outcomes([search, {"url": search["anchors"][-1]["href"]}])
    assert result["status"] == "failed"
    assert result["criteria"]["first_result"]["status"] == "failed"


def test_matching_ad_id_on_another_host_does_not_prove_navigation(search):
    final = {"url": "https://example.test/bmw-z4-12345678-covo.aspx"}
    assert evaluation.evaluate_outcomes([search, final])["criteria"]["first_result"]["status"] == "unknown"


def test_unknown_card_structure_cannot_prove_first_result(search):
    for anchor in search["anchors"]:
        anchor["result_card"] = False
    result = evaluation.evaluate_outcomes([search, {"url": search["anchors"][1]["href"]}])
    assert result["criteria"]["first_result"]["status"] == "unknown"


def test_last_search_state_takes_precedence_over_earlier_success(search):
    changed = {**search, "url": search["url"].replace("20000", "30000")}
    result = evaluation.evaluate_outcomes([search, changed, {"url": search["anchors"][1]["href"]}])
    assert result["criteria"]["price_max"]["status"] == "failed"


def test_original_prompt_requires_kilometers_but_does_not_require_detail_navigation(search):
    result = evaluation.evaluate_outcomes([search], original_prompt=True)
    assert result["status"] == "unknown"
    assert "first_result" not in result["criteria"]
    search["url"] += "&MaxKm=150000"
    assert evaluation.evaluate_outcomes([search], original_prompt=True)["status"] == "verified"


def test_empty_observation_cannot_pass():
    result = evaluation.evaluate_outcomes([])
    assert result["status"] == "unknown"
    assert all(item["status"] == "unknown" for item in result["criteria"].values())


@pytest.mark.parametrize("close_tab", [False, True])
def test_live_runner_preserves_error_trace_and_respects_tab_preference(tmp_path, monkeypatch, close_tab):
    instances = []

    class FakeBrowser:
        def evaluate(self, _expression):
            return {"url": "https://www.coches.net/", "text": ""}

    class FakeAgent:
        def __init__(self, url, goal, **options):
            assert url == "https://www.coches.net"
            assert goal == evaluation.GOAL
            assert options == ({"close_tab_on_exit": True} if close_tab else {})
            self.browser = FakeBrowser()
            self.state = {"history": [], "status": "ready"}
            self.closed = False
            self.disconnected = False
            instances.append(self)

        def run(self):
            raise RuntimeError("Simulated provider failure")

        def close(self):
            self.closed = True

        def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(evaluation, "Agent", FakeAgent)
    monkeypatch.setattr(evaluation, "load_environment", lambda: None)
    args = ["--output", str(tmp_path)] + (["--close-tab"] if close_tab else [])
    assert evaluation.main(args) == 2
    assert instances[0].closed is close_tab
    assert instances[0].disconnected is not close_tab
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["error"]["type"] == "RuntimeError"
    assert report["outcome"]["status"] == "unknown"
    events = [json.loads(line)["event"] for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert events == ["start", "independent_dom", "error", "independent_dom", "report"]
