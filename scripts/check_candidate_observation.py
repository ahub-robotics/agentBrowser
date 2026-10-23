"""Real local DOM and workflow checks for CS01; Jev is simulated and model network calls are forbidden."""

import json
import os
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.parse import quote

from AgentBrowser import agent as loop
from AgentBrowser import model
from AgentBrowser.browser import Browser, fingerprint
from AgentBrowser.candidates import DEFAULT_LIMITS


def check_real_workflow():
    """Exercise real DOM, extraction, request heads and LangGraph with only Jev simulated."""
    calls = []
    helper = Mock(side_effect=AssertionError("The text helper must not run for candidate consultation or SELECT"))

    def answer(criteria, selected):
        assert selected in criteria
        return {"choice": selected, "confidence": 1, "probabilities": {
            candidate: float(candidate == selected) for candidate in criteria
        }}

    def simulated_jev(_url, _key, body):
        assert len(calls) < 4, "The fixture must finish within four logical Jev calls"
        body.http_attempts += 1
        questions = body["questions"]
        selected_index = agent.browser.evaluate("document.querySelector('select').selectedIndex")
        target = next((option["index"] for element in body["state"]["elements"]
                       for option in element.get("options", []) if option["value"] == "value36"), None)
        operation = "DONE" if selected_index == 36 else "SELECT" if target is not None else "MORE_CANDIDATES"
        answers = {
            "operation": answer(questions["operation"]["criteria"], operation),
            "select_target": {"choice": "unobserved-unused-head"},
            "click_target": {"choice": "unobserved-unused-head"},
        }
        if operation == "SELECT":
            answers["select_target"] = answer(questions["select_target"]["criteria"], target)
        calls.append({"operation": operation, "window_id": body["state"]["candidate_coverage"]["window_id"]})
        return {"model": "simulated-local-jev", "answers": answers}

    with (
        patch.dict(os.environ, {"TYPESAFE_API_KEY": "offline-local-fixture"}),
        patch.object(model, "post_json", simulated_jev),
        patch.object(loop, "field_text", helper),
        patch.object(model.CLIENT, "post", side_effect=AssertionError("Model network calls are forbidden")),
        loop.Agent("about:blank", "Select Option 36 as the destination.",
                   candidate_limits=replace(DEFAULT_LIMITS, max_queries=4, max_windows=4),
                   verification=False, close_tab_on_exit=True) as agent,
    ):
        agent.browser.evaluate("""(() => {
          document.title='Candidate workflow fixture';
          document.body.innerHTML='<select aria-label="Destination"></select>';
          const select=document.querySelector('select');
          select.innerHTML=Array.from({length:37},(_,i)=>'<option value="value'+i+'">Option '+i+'</option>').join('');
          window.fixtureEvents={input:0,change:0};
          for (const event of ['input','change']) select.addEventListener(event,()=>window.fixtureEvents[event]++);
        })()""")
        agent._install_page(agent.browser.observe(screenshot=False))
        states = list(agent.run())
        observed = agent.browser.evaluate("({selectedIndex:document.querySelector('select').selectedIndex,"
                                          "value:document.querySelector('select').value,events:window.fixtureEvents})")
        assert [entry["operation"] for entry in calls] == ["MORE_CANDIDATES", "MORE_CANDIDATES", "SELECT", "DONE"]
        assert [state["status"] for state in states] == ["ready", "ready", "ready", "done"]
        assert observed == {"selectedIndex": 36, "value": "value36", "events": {"input": 1, "change": 1}}
        assert agent.state["model_calls"] == agent.state["model_http_attempts"] == len(calls) == 4
        assert len(agent.state["history"]) == 1 and agent.state["history"][0]["kind"] == "select"
        assert len(agent.state["candidate_controls"]) == 2
        assert agent.state["text_calls"] == []
        helper.assert_not_called()
        return {"calls": calls, "model_calls": len(calls), "browser_actions": len(agent.state["history"]),
                "candidate_controls": len(agent.state["candidate_controls"]), "verified_dom": observed}


def main():
    browser = Browser("data:text/html," + quote("<!doctype html><title>Candidate observation checks</title>"))
    passed = []

    def install(html, setup=""):
        browser.evaluate("document.body.innerHTML=" + json.dumps(html) + ";" + setup + ";void 0")
        return browser.observe(screenshot=False)

    try:
        options = "".join(f'<option value="v{i}">Option {i}</option>' for i in range(1000))
        page = install(f'<select aria-label="Choice">{options}</select><button>Continue</button>')
        assert len(page["inventory"]["nodes"]) == 2
        assert len(page["inventory"]["nodes"][0]["options"]) == 999
        assert sum(action["kind"] == "select" for action in page["actions"]) == 12
        assert any(action["label"] == "Continue" for action in page["actions"])
        assert page["inventory"]["coverage"]["options_inspected"] == 1000
        assert page["inventory"]["coverage"]["extraction_complete"]
        passed.append("1000-option select preserves later button and exact inventory counts")

        before = json.dumps(page["inventory"], sort_keys=True)
        assert browser.fresh(page)
        assert browser.fresh(page)
        same = browser.observe(screenshot=False)
        assert before == json.dumps(same["inventory"], sort_keys=True)
        assert page["actions"] == same["actions"]
        passed.append("freshness reads preserve inventory, option identities and preview")

        button = next(action for action in page["actions"] if action["label"] == "Continue")
        browser.evaluate("document.querySelector('select').options[900].text='Changed pending option'")
        assert not browser.fresh(page)
        assert not browser.fresh(page, button)
        changed = browser.observe(screenshot=False)
        assert page["actions"] == changed["actions"]
        assert fingerprint(page) != fingerprint(changed)
        passed.append("pending option changes invalidate DOM and action freshness outside preview")

        page = changed
        browser.evaluate("const option=document.querySelector('select').options[900]; "
                         "option.replaceWith(option.cloneNode(true))")
        assert not browser.fresh(page)
        passed.append("identical pending option replacement invalidates its DOM identity")

        page = install('<select aria-label="Duplicates"><option>Initial</option>'
                       '<option value="same">Same</option><option value="same">Same</option></select>')
        options = [action for action in page["actions"] if action["kind"] == "select"]
        assert len(options) == 2 and options[0]["option"] != options[1]["option"]
        browser.act(options[1], page)
        assert browser.evaluate("document.querySelector('select').selectedIndex") == 2
        passed.append("duplicate labels and values select the exact second observed option")

        page = install('<select aria-label="Large"></select><button>After</button>',
                       "document.querySelector('select').innerHTML="
                       "Array.from({length:6001},(_,i)=>'<option>'+i+'</option>').join('')")
        coverage = page["inventory"]["coverage"]
        assert coverage["options_inspected"] == 5000 and coverage["controls_inspected"] == 2
        assert not coverage["options_total_known"] and not coverage["extraction_complete"]
        assert any(action["label"] == "After" for action in page["actions"])
        passed.append("option inspection stops at 5000 and declares unknown total")

        page = install('<style>body{margin:0;display:grid;grid-template-columns:repeat(100,8px)}'
                       'button{width:8px;height:8px;padding:0;border:0;font-size:0}</style>',
                       "document.body.insertAdjacentHTML('beforeend',"
                       "Array.from({length:5001},(_,i)=>'<button>Button '+i+'</button>').join(''))")
        coverage = page["inventory"]["coverage"]
        assert coverage["controls_inspected"] == 5000
        assert not coverage["controls_total_known"] and not coverage["extraction_complete"]
        assert len(page["inventory"]["nodes"]) == 5000
        passed.append("control inspection stops at 5000 and declares unknown total")

        page = install('<style>body{display:grid;grid-template-columns:repeat(10,80px);gap:2px}'
                       'button{width:80px;height:30px;padding:0}</style>' +
                       "".join(f'<form><button>Region {i}</button></form>' for i in range(40)))
        regions = {node["region"] for node in page["inventory"]["nodes"]}
        assert len(regions) == 33 and "residual" in regions
        assert page["inventory"]["coverage"]["region_overflow"]
        passed.append("structural region count is bounded to 32 plus residual")

        browser.candidate_limits = replace(DEFAULT_LIMITS, max_controls_inspected=2, max_options_inspected=2)
        page = install('<select aria-label="Custom"><option>Initial</option><option>A</option>'
                       '<option>B</option></select><button>Second</button><button>Third</button>')
        coverage = page["inventory"]["coverage"]
        assert coverage["controls_inspected"] == coverage["options_inspected"] == 2
        assert not coverage["controls_total_known"] and not coverage["options_total_known"]
        assert browser.fresh(page)
        passed.append("custom extraction limits are identical during observations and freshness")

        browser.candidate_limits = DEFAULT_LIMITS
        page = install('<iframe src="about:blank"></iframe><button>Outside frame</button>')
        assert "iframes" in page["inventory"]["coverage"]["unsupported_contexts"]
        assert not page["inventory"]["coverage"]["extraction_complete"]
        passed.append("unsupported frames are declared instead of claiming complete coverage")
    finally:
        browser.close()
    workflow = check_real_workflow()
    passed.append("real Agent/LangGraph reaches option 36 after two consultations; one SELECT and four simulated calls")
    print("\n".join(passed))
    print("WORKFLOW_EVIDENCE: " + json.dumps(workflow, sort_keys=True))
    print(f"PASS: {len(passed)} candidate observation checks; no paid model calls (4 simulated Jev calls)")


if __name__ == "__main__":
    main()
