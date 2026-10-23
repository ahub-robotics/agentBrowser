"""Check custom toggle labels in real Chrome against a local fixture; no model calls."""

import json
from pathlib import Path
from unittest.mock import patch

from AgentBrowser import browser as browser_module
from AgentBrowser import model
from AgentBrowser.browser import Browser

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "contextual_labels.html"


def main():
    passed = []
    browser = Browser(FIXTURE.as_uri())

    def buttons(page):
        return [node for node in page["inventory"]["nodes"] if node["role"] == "button"]

    def install(html):
        browser.evaluate("document.body.innerHTML=" + json.dumps(
            '<style>button{width:28px;height:28px}</style>' + html) + ";void 0")
        return browser.observe(screenshot=False)

    try:
        page = browser.observe(screenshot=False)
        # Comparison isolates the former label expression; it does not revert other reader fixes.
        former_labels = browser_module.read_state_expression().replace(
            "label:label(e)||rname", "label:name(e)||rname")
        comparison = browser.evaluate(former_labels)
        assert [node["label"] for node in buttons(comparison)] == ["button", "button"]
        assert [node["label"] for node in buttons(page)] == ["Z4", "X3"]
        assert all(node["role"] != "checkbox" for node in page["inventory"]["nodes"])
        assert [node["pressed"] for node in buttons(page)] == ["false", "false"]
        passed.append("exact user markup: Z4 and adjacent X3 have separate names; hidden inputs are excluded")

        prepared = model.prepare_candidates(page, "Select Z4.", [])
        body = prepared["_candidate_request"]
        element = next(element for element in body["state"]["elements"] if element["label"] == "Z4")
        criterion = body["questions"]["click_target"]["criteria"][element["index"]]
        assert element["role"] == "button" and element["pressed"] == "false"
        assert criterion["pressed"] == "false" and "Z4" in criterion["element"]
        passed.append("prepared model state and click criteria contain Z4 with pressed=false")

        action = next(action for action in page["actions"] if action["label"] == "Z4")
        browser.act(action, page)
        after = browser.observe(screenshot=False)
        outcome = browser.evaluate("({pressed:document.querySelector('button').getAttribute('aria-pressed'),"
                                   "checked:document.getElementById('291').checked,clicks:window.fixtureClicks})")
        assert outcome == {"pressed": "true", "checked": True, "clicks": 1}
        assert next(node for node in buttons(after) if node["label"] == "Z4")["pressed"] == "true"
        assert next(node for node in buttons(after) if node["label"] == "X3")["pressed"] == "false"
        assert after["fingerprint"] != page["fingerprint"]
        passed.append("one observed Z4 click toggles only Z4; the following snapshot reports pressed=true")

        action = next(action for action in after["actions"] if action["label"] == "Z4")
        browser.evaluate("document.querySelector('p').textContent='Z5'")
        assert not browser.fresh(after, action)
        renamed = browser.observe(screenshot=False)
        assert buttons(renamed)[0]["label"] == "Z5"
        passed.append("changing sibling label invalidates the previous action and refreshes its name")

        action = next(action for action in renamed["actions"] if action["label"] == "Z5")
        browser.evaluate("document.querySelector('button').setAttribute('aria-pressed','false')")
        assert not browser.fresh(renamed, action)
        assert buttons(browser.observe(screenshot=False))[0]["pressed"] == "false"
        passed.append("aria-pressed change alone invalidates freshness and appears in a new snapshot")

        page = install('<ul><li>Row name<button aria-label="Explicit name"></button></li>'
                       '<li>Other row<button title="Explicit title"></button></li>'
                       '<li>More context<button>Own text</button></li></ul>')
        assert [node["label"] for node in buttons(page)] == ["Explicit name", "Explicit title", "Own text"]
        passed.append("explicit aria-label, title and button text take precedence over row context")

        page = install('<ul><li>Ambiguous<button></button><button></button></li>'
                       '<li>Later row<button></button></li></ul>')
        assert [node["label"] for node in buttons(page)] == ["button", "button", "Later row"]
        passed.append("two buttons in one row stay unnamed while a later unambiguous row is observed")

        page = install('<ul><li><span hidden>Hidden</span><span style="display:none">CSS hidden</span>'
                       '<span style="visibility:hidden">Invisible</span><span aria-hidden="true">ARIA hidden</span>'
                       '<script type="application/json">"Script text"</script>Visible label<button></button></li></ul>')
        assert buttons(page)[0]["label"] == "Visible label"
        passed.append("context labels omit hidden, invisible, aria-hidden and script text")

        page = install('<ul><li>Parent text<ul><li>Child label<button></button></li></ul>'
                       '<button></button></li></ul>')
        assert [node["label"] for node in buttons(page)] == ["Child label", "button"]
        passed.append("nested row names do not leak into an outer unnamed button")

        page = install('<div role="listitem">List item<button></button></div>'
                       '<table><tr><td>Table row</td><td><button></button></td></tr></table>'
                       '<div role="row">ARIA row<button></button></div>'
                       '<div role="option">Option row<button></button></div>'
                       '<label>Label wrapper<button></button></label>')
        assert [node["label"] for node in buttons(page)] == [
            "List item", "Table row", "ARIA row", "Option row", "Label wrapper"]
        passed.append("semantic listitem, table row, ARIA row, option and label scopes work generically")

        page = install('<ul><li>' + "x" * 241 + '<button></button></li><li>' +
                       '<span> </span>' * 201 + '<button></button></li>'
                       '<li>After oversized rows<button></button></li></ul>')
        assert [node["label"] for node in buttons(page)] == ["button", "button", "After oversized rows"]
        passed.append("context length and scan limits fall back safely and retain later controls")
    finally:
        browser.close()

    print("\n".join(passed))
    print("EVIDENCE: " + json.dumps({"former_label_expression": ["button", "button"],
                                    "current_labels": ["Z4", "X3"], "verified_dom": outcome,
                                    "model_calls": 0}, sort_keys=True))
    print(f"PASS: {len(passed)} contextual label checks; no model calls")


if __name__ == "__main__":
    with patch.object(model.CLIENT, "post", side_effect=AssertionError("Model network calls are forbidden")):
        main()
