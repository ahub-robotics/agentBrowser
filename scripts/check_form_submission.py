"""Native submission metadata and freshness in local Chrome; no model calls."""

import json
from pathlib import Path
from unittest.mock import patch

from AgentBrowser import model
from AgentBrowser.browser import Browser

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "form_submit_controls.html"


def main():
    browser = Browser(FIXTURE.as_uri())
    passed = []

    def action(page, label):
        return next(item for item in page["actions"] if item["label"] == label)

    try:
        page = browser.observe(screenshot=False)
        expected = {
            "Implicit submit": True, "Open panel": False, "Reset form": False,
            "Input submit": True, "Image submit": True, "Missing owner": False,
            "External submit": True, "Standalone button": False, "Standalone input": False,
        }
        nodes = {item["label"]: item for item in page["inventory"]["nodes"]}
        assert {label: nodes[label]["submits_form"] for label in expected} == expected
        assert {label: action(page, label)["submits_form"] for label in expected} == expected
        assert "submits_form" not in nodes["Custom disclosure"]
        assert nodes["Implicit submit"]["expanded"] == "false"
        passed.append("native default/type/form ownership produces factual booleans in nodes and actions")

        browser.act(action(page, "Implicit submit"), page)
        page = browser.observe(screenshot=False)
        assert browser.evaluate("window.submissions") == ["first"]
        browser.act(action(page, "Open panel"), page)
        page = browser.observe(screenshot=False)
        assert browser.evaluate("window.submissions") == ["first"]
        passed.append("expanded=false default button submits once; explicit type=button does not submit")

        previous = page
        opener = action(previous, "Open panel")
        browser.evaluate("document.getElementById('opener').type='submit'")
        assert not browser.fresh(previous, opener)
        page = browser.observe(screenshot=False)
        assert action(page, "Open panel")["submits_form"] is True
        passed.append("changing type=button to submit invalidates the prior action and metadata")

        previous = page
        external = action(previous, "External submit")
        browser.evaluate("document.getElementById('external').setAttribute('form','second')")
        assert not browser.fresh(previous, external)
        page = browser.observe(screenshot=False)
        assert action(page, "External submit")["submits_form"] is True
        browser.act(action(page, "External submit"), page)
        page = browser.observe(screenshot=False)
        assert browser.evaluate("window.submissions") == ["first", "second"]
        passed.append("external form reassociation invalidates while submits_form remains true")

        previous = page
        image = action(previous, "Image submit")
        browser.evaluate("document.getElementById('image').type='submit'")
        assert not browser.fresh(previous, image)
        page = browser.observe(screenshot=False)
        assert action(page, "Image submit")["submits_form"] is True
        passed.append("changing image to submit invalidates even though both types submit")

        previous = page
        external = action(previous, "External submit")
        browser.evaluate("document.getElementById('external').setAttribute('form','missing')")
        assert not browser.fresh(previous, external)
        page = browser.observe(screenshot=False)
        assert action(page, "External submit")["submits_form"] is False
        passed.append("removing a resolved form owner invalidates and reports submits_form=false")
    finally:
        browser.close()
    print("\n".join(passed))
    print("EVIDENCE: " + json.dumps({"checks": len(passed), "model_calls": 0, "submissions": ["first", "second"]}))
    print(f"PASS: {len(passed)} native submission checks; no model calls")


if __name__ == "__main__":
    with patch.object(model.CLIENT, "post", side_effect=AssertionError("Model network calls are forbidden")):
        main()
