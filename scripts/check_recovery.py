"""Real Chrome, local generic fixtures only; no models or paid API calls."""

import json
from dataclasses import replace
from pathlib import Path

from AgentBrowser.browser import Browser, StalePage


def main():
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/recovery.html"
    browser = Browser(fixture.as_uri())
    passed = []
    try:
        page = browser.observe(screenshot=False)
        accepts = [a for a in page["actions"] if a["label"] == "Accept"]
        assert {a["context"]["heading"] for a in accepts} == {"Origin", "Destination"}
        required = next(n for n in page["inventory"]["nodes"] if n.get("required"))
        assert required["invalid"] and required["validation_message"]
        passed.append("Duplicate controls retain separate contexts and native validity evidence")

        links = [n for n in page["inventory"]["nodes"] if n["role"] == "link"]
        assert [n["href"] for n in links] == [fixture.as_uri()+"#first-result", fixture.as_uri()+"#second-result"]
        assert all(n["operations"] == ["click"] for n in links)
        passed.append("Link evidence contains absolute observed destinations in DOM order without new operations")

        assert any(a["label"] == "Popup search" and a["kind"] == "fill" for a in page["actions"])
        assert any(a["label"] == "Confirm popup" for a in page["actions"])
        assert next(a for a in page["actions"] if a["label"] == "Popup search")["context"]["heading"] == "Related popup"
        assert "Visible popup outside overflow ancestor" in page["text"]
        assert browser.evaluate("document.querySelector('.escaped-popup').getBoundingClientRect().top > "
                                "document.querySelector('.overflow-shell').getBoundingClientRect().bottom")
        assert "amount 20" not in page["text"]
        passed.append("Absolute popup escaping overflow is observed; truly clipped list options remain excluded")

        amount = next(a for a in page["actions"] if a["kind"] == "scroll" and
                      a["context"]["heading"] == "Amount options")
        assert not any(a["label"] == "amount 20" for a in page["actions"])
        before = browser.evaluate("JSON.stringify(window.fixtureEvents)")
        assert browser.fresh(page, amount)
        browser.observe(screenshot=False)
        assert browser.evaluate("JSON.stringify(window.fixtureEvents)") == before
        passed.append("Read-only observations and freshness do not scroll or click")

        browser.act(amount, page)
        page = browser.observe(screenshot=False)
        actual = browser.evaluate("({amount:document.getElementById('amount').scrollTop,"
                                  "distance:document.getElementById('distance').scrollTop,events:window.fixtureEvents})")
        assert actual["amount"] > 0 and actual["distance"] == 0 and actual["events"]["wheel"] == 1
        passed.append("One observed region scroll sends one wheel and leaves its sibling unchanged")

        old = page
        amount = next(a for a in page["actions"] if a["kind"] == "scroll" and a["direction"] == "down" and
                      a["context"]["heading"] == "Amount options")
        browser.evaluate("document.getElementById('amount').scrollTop+=10")
        assert not browser.fresh(old, amount)
        try:
            browser.act(amount, old)
            raise AssertionError("A stale region scroll must not execute")
        except StalePage:
            pass
        assert browser.evaluate("window.fixtureEvents.wheel") == 1
        passed.append("Changed region position invalidates a pending scroll before input")

        for _ in range(10):
            page = browser.observe(screenshot=False)
            target = next((a for a in page["actions"] if a["label"] == "amount 20"), None)
            if target:
                browser.act(target, page)
                page = browser.observe(screenshot=False)
                break
            action = next(a for a in page["actions"] if a["kind"] == "scroll" and a["direction"] == "down" and
                          a["context"]["heading"] == "Amount options")
            browser.act(action, page)
        else:
            raise AssertionError("Could not discover a lower option")
        assert browser.evaluate("window.fixtureEvents.amount") == 1
        passed.append("Repeated explicitly selected scrolls discover a previously clipped option")

        load = next(a for a in page["actions"] if a["label"] == "Load delayed content")
        browser.act(load, page)
        page = browser.observe(screenshot=False)
        assert "Delayed content arrived" in page["text"]
        assert page["readiness"]["status"] == "ready" and page["readiness"]["reads"] > 1
        passed.append("Post-action loading observations wait for delayed content without repeating click")

        details = page["inventory"]["coverage"]["unsupported_context_details"]
        hidden = next(item for item in details if item["title"] == "Hidden integration host")
        assert not hidden["css_visible"] and not hidden["in_viewport"] and not hidden["reachable_sample"]
        assert hidden["rect"]["w"] == hidden["rect"]["h"] == 0
        assert not page["inventory"]["coverage"]["extraction_complete"]
        assert page["inventory"]["coverage"]["unsupported_context_details_complete"]
        browser.evaluate("document.getElementById('visible-frame').style.display='block'")
        page = browser.observe(screenshot=False)
        visible = next(item for item in page["inventory"]["coverage"]["unsupported_context_details"]
                       if item["title"] == "Visible opaque panel")
        assert visible["css_visible"] and visible["in_viewport"] and visible["reachable_sample"]
        assert not any(a["label"] == "Load delayed content" for a in page["actions"])
        assert not page["inventory"]["coverage"]["extraction_complete"]
        passed.append("Opaque host evidence distinguishes hidden 0x0 frames from a visible occluding frame")

        browser.candidate_limits = replace(browser.candidate_limits, max_regions=1)
        page = browser.observe(screenshot=False)
        coverage = page["inventory"]["coverage"]
        assert len(coverage["unsupported_context_details"]) == coverage["unsupported_context_details_limit"] == 1
        assert coverage["unsupported_hosts_observed"] == 2
        assert not coverage["unsupported_context_details_complete"] and not coverage["extraction_complete"]
        assert set(coverage["unsupported_context_details"][0]) == {
            "kind", "title", "label", "css_visible", "in_viewport", "reachable_sample", "rect",
        }
        passed.append("Opaque host descriptions obey the configured bound without claiming complete coverage")
        print(json.dumps({"passed": passed, "count": len(passed), "model_calls": 0}, indent=2))
    finally:
        browser.close()


if __name__ == "__main__":
    main()
