"""Compact observed links remain usable when controls fill the evidence slice."""

from copy import deepcopy

from test_agent import page
from test_supervised_loops import GOAL, ITEM_A, ITEM_B, LIST_URL, LoopBrowser, listing, loop_update, reply

from agentrun.supervised import LegacySupervisedAgent as SupervisedAgent
from AgentBrowser.browser import fingerprint


def busy_page():
    observed = page()
    observed["url"] = LIST_URL
    observed["actions"] = [{
        "id": f"option-{index}", "node": index, "kind": "click", "role": "button", "label": f"Option {index}",
    } for index in range(400)]
    hrefs = [ITEM_A, ITEM_A, ITEM_B, "mailto:seller@example.test", "/relative", "https://long.test/" + "x" * 2001]
    for index, href in enumerate(hrefs, 1000):
        observed["actions"].append({
            "id": f"link-{index}", "node": index, "kind": "click", "role": "link",
            "label": f"Result {index}", "href": href, **({"href_truncated": True} if href == ITEM_B else {}),
        })
    observed["fingerprint"] = fingerprint(observed)
    return observed


def test_link_catalog_supplies_complete_unique_refs_beyond_400_unrelated_controls(tmp_path):
    browser = LoopBrowser(busy_page())
    payloads = []

    def policy(payload):
        payloads.append(deepcopy(payload))
        assert not any(fact.get("href") for fact in payload["evidence"])
        catalog = payload["observed_links"]
        assert len(catalog) == payload["observed_links_total"] == 1
        assert payload["observed_links_omitted"] == 0
        link = catalog[0]
        assert link["href"] == ITEM_A and link["label"] == "Result 1000"
        assert {"ref", "kind", "observed_at", "observed_url"} <= link.keys()
        assert link["ref"] not in {fact["ref"] for fact in payload["evidence"]}
        assert "inspect" in payload["available_actions"]
        assert payload["feedback_reasons"] == ["missing_user_information"]
        return reply(loops=[loop_update([link["ref"]])])

    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["metrics"]["supervisor_calls"] == 1
        item = next(iter(final["items"].values()))
        assert item["url"] == ITEM_A
        assert item["source_ref"] == payloads[0]["observed_links"][0]["ref"]
        assert item["source_ref"] in agent.state["sent_refs"]
        assert final["metrics"]["jev_calls"] == 0
    assert browser.inputs == []


def test_bounded_link_catalog_leaves_every_omitted_link_accessible_through_inspect(tmp_path):
    urls = tuple(f"https://example.test/items/{index}" for index in range(180))
    browser = LoopBrowser(listing(urls))
    seen = set()
    slices = []

    def policy(payload):
        catalog = payload["observed_links"]
        assert len(catalog) == 80
        assert payload["observed_links_total"] == 180
        assert payload["observed_links_omitted"] == 100
        assert len({link["href"] for link in catalog}) == 80
        seen.update(link["href"] for link in catalog)
        seen.update(fact["href"] for fact in payload["evidence"] if fact.get("href"))
        slices.append(payload["evidence_offset"])
        if payload["next_evidence_offset"] is not None:
            assert "inspect" in payload["available_actions"]
            return reply("inspect")
        assert "inspect" not in payload["available_actions"]
        assert set(payload["available_actions"]) == {"delegate", "finish", "feedback"}
        return reply()

    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert final["metrics"]["supervisor_calls"] == len(slices) == 2
        assert final["metrics"]["jev_calls"] == final["metrics"]["actions"] == 0
        assert seen == set(urls)
    assert browser.inputs == []


def test_catalog_uses_latest_observed_fact_for_repeated_url(tmp_path):
    browser = LoopBrowser(listing((ITEM_A,)))
    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=lambda _: reply()) as agent:
        original = next(fact["ref"] for fact in agent.state["evidence"].values() if fact.get("href") == ITEM_A)
        updated = listing((ITEM_A,))
        updated["actions"][0]["label"] = "Updated item title"
        updated["fingerprint"] = fingerprint(updated)
        agent._record_page(updated)
        payload = agent._payload()
        assert payload["observed_links_total"] == 1
        newest = payload["observed_links"][0]
        assert newest["label"] == "Updated item title"
        assert newest["ref"] != original
        assert newest["observed_at"] == agent.state["observation_seq"]
        assert newest["ref"] in agent.state["sent_refs"]
    assert browser.inputs == []


def test_previous_provider_http_400_keeps_observed_links_available_without_extra_jev_calls(tmp_path):
    urls = tuple(f"https://example.test/items/{index}" for index in range(14))
    browser = LoopBrowser(listing(urls))
    payloads = []

    def policy(payload):
        payloads.append(deepcopy(payload))
        assert len(payload["observed_links"]) == payload["observed_links_total"] == 14
        assert "inspect" not in payload["available_actions"]
        assert "provider_error" in payload["feedback_reasons"]
        assert payload["last_result"]["provider_error"]["provider_http_status"] == 400
        return reply(loops=[loop_update([link["ref"] for link in payload["observed_links"]])],
                     feedback_reason="provider_error")

    with SupervisedAgent(LIST_URL, GOAL, state_dir=tmp_path, browser=browser, supervisor=policy) as agent:
        agent.state["last_result"] = {
            "delegation_id": "earlier-provider-failure", "status": "blocked", "history": [],
            "evidence_refs": [], "provider_error": {"provider_error_kind": "request", "provider_http_status": 400},
        }
        final = list(agent.run())[-1]
        assert final["stop_reason"] == "supervisor_feedback"
        assert len(payloads) == 1
        assert {item["url"] for item in final["items"].values()} == set(urls)
        assert final["metrics"]["jev_calls"] == final["metrics"]["actions"] == 0
    assert browser.inputs == []
