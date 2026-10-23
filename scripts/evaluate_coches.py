"""Explicit live acceptance run on coches.net; importing this module performs no I/O.

Run manually: uv run python scripts/evaluate_coches.py
The agent uses this evaluator's filter-and-first-ad goal, never the contact loop
in test.py. A separate DOM reader records factual
evidence without clicking, typing, scrolling or using the agent's DOM inventory.
Unknown evidence never counts as a successful acceptance result.
"""

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from AgentBrowser import Agent, model  # noqa: E402
from AgentBrowser.demo import load_environment  # noqa: E402

GOAL = (
    "Accept cookies if present. "
    "Select BMW Z4 and press Aceptar in the model panel. "
    "Select Madrid and press Aceptar in the provincias panel. "
    "Set the maximum price to 20.000 €. "
    "Show the results, wait for the results panel and open the first advertisement."
)
ORIGINAL_GOAL = (
    "Accept cookies if present. Select BMW Z4 and Madrid. "
    "Set the maximum price to 20.000 € and maximum mileage to 150.000 km. "
    "Confirm all filters and show the results."
)

READ_DOM = r"""(() => {
  const visible = e => {
    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden');
  };
  const text = e => (e?.innerText||e?.textContent||'').replace(/\s+/g,' ').trim();
  const inPanel = e => {
    for(let p=e;p&&p!==document.body;p=p.parentElement) {
      if(p.matches('[role="dialog"],[aria-modal="true"]') ||
         /(?:Modal|Drawer|Popover|Dropdown|FilterPanel|FilterBasicLegacy)/.test(p.className||'')) return true;
    }
    return false;
  };
  const controls=[...document.querySelectorAll('input,select,button,[role="checkbox"],[role="radio"],[role="option"]')]
    .filter(e=>visible(e)||e.matches('input:checked')).slice(0,1000).map(e=>{
      const row=e.closest('li,label,[role="option"],[role="listitem"]');
      const label=e.getAttribute('aria-label')||text(e.labels?.[0])||text(row)||text(e);
      return {tag:e.tagName.toLowerCase(),type:e.type||'',role:e.getAttribute('role')||'',
        label:label.slice(0,500),id:e.id||'',name:e.name||'',value:e.value??'',
        checked:!!e.checked,pressed:e.getAttribute('aria-pressed'),selected:e.getAttribute('aria-selected'),
        in_panel:inPanel(e),
        related_ids:row?[...row.querySelectorAll('input,select')].map(x=>({id:x.id,name:x.name,value:x.value})):[],
        options:e.tagName==='SELECT'?[...e.options].map(x=>({label:x.label,value:x.value,selected:x.selected})):[]};
    });
  const summarySelector='[class*="FilterTag"],[class*="FilterChip"],[class*="SelectedFilter"],'+
    '[class*="AppliedFilter"],[class*="FilterPill"],[class*="FilterBadge"],[data-testid*="active-filter"]';
  const filter_summaries=[...document.querySelectorAll(summarySelector)].filter(e=>visible(e)&&!inPanel(e))
    .map(e=>({text:text(e).slice(0,1000),class_name:String(e.className),source:'applied_filter'}));
  const headings=[...document.querySelectorAll('h1')].filter(e=>visible(e)&&!e.closest('article')&&!inPanel(e))
    .map(e=>({text:text(e).slice(0,1000),source:'search_heading'}));
  const anchors=[...document.querySelectorAll('a[href]')].filter(visible).slice(0,1500).map((e,index)=>({
    href:e.href,text:text(e).slice(0,500),index,
    result_card:!!e.closest('article,[class*="CardAd"],[class*="ListAds"],[data-testid*="ad-card"]'),
    in_panel:inPanel(e)
  }));
  return {url:location.href,title:document.title,ready_state:document.readyState,
    text:(document.body?.innerText||'').slice(0,50000),controls,filter_summaries,headings,anchors};
})()"""


def normalized(value):
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    return " ".join("".join(char for char in value if not unicodedata.combining(char)).split())


def detail_id(url):
    """Recognized public advertisement URL shape; unrecognized shapes stay unknown."""
    parsed = urlparse(url)
    if parsed.hostname not in {"www.coches.net", "coches.net"}:
        return None
    path = parsed.path
    match = re.search(r"(?:-|/)(\d+)-covo\.aspx$", path, re.I)
    return match.group(1) if match else None


def search_page(observation):
    parsed = urlparse(observation.get("url", ""))
    return parsed.hostname in {"www.coches.net", "coches.net"} and bool(
        re.match(r"^/search(?:/|$)", parsed.path, re.I)
    )


def query_values(observation):
    values = parse_qs(urlparse(observation.get("url", "")).query)
    result = {}
    for key, entries in values.items():
        result.setdefault(re.sub(r"[^a-z]", "", normalized(key)), []).extend(entries)
    return result


def selected(control):
    return control.get("checked") is True or control.get("pressed") == "true" or control.get("selected") == "true"


def learned_ids(observations, label):
    """Learn mappings only from observed selected controls, never guessed province IDs."""
    result = set()
    for observation in observations:
        for control in observation.get("controls", []):
            if normalized(control.get("label", "")) != normalized(label) or not selected(control):
                continue
            candidates = [control, *control.get("related_ids", [])]
            for candidate in candidates:
                for key in ("id", "name", "value"):
                    value = str(candidate.get(key, ""))
                    if re.fullmatch(r"\d+", value):
                        result.add(value)
    return result


def criterion(status, **evidence):
    return {"status": status, "evidence": evidence}


def filter_outcomes(observation, preceding, *, kilometers=False):
    """Certify applied search scope, not matching ad text or pending panel choices."""
    url = observation.get("url", "")
    result = {key: criterion("unknown", reason="No applied filter evidence")
              for key in ("bmw_z4", "madrid", "price_max")}
    if kilometers:
        result["kilometers_max"] = criterion("unknown", reason="No applied kilometer limit evidence")
    if not search_page(observation):
        return result
    scoped = [*observation.get("filter_summaries", []), *observation.get("headings", [])]
    for item in scoped:
        text = normalized(item.get("text", ""))
        if re.search(r"\bbmw\s+z4\b", text):
            result["bmw_z4"] = criterion("verified", source=item.get("source"), value=item["text"], url=url)
        if re.search(r"\bmadrid\b", text):
            result["madrid"] = criterion("verified", source=item.get("source"), value=item["text"], url=url)
        if re.search(r"(?:hasta|max(?:imo)?\.?|menos de|<)\s*20[.\s]?000\s*(?:€|eur)", text):
            result["price_max"] = criterion("verified", source=item.get("source"), value=item["text"], url=url)
        if kilometers and re.search(r"(?:hasta|max(?:imo)?\.?|menos de|<)\s*150[.\s]?000\s*km", text):
            result["kilometers_max"] = criterion("verified", source=item.get("source"), value=item["text"], url=url)
    params = query_values(observation)
    mappings = {
        "madrid": (learned_ids(preceding, "Madrid"), {"provinceid", "provinceids", "regionid", "regionids"}),
        "bmw_z4": (learned_ids(preceding, "Z4"), {"modelid", "modelids"}),
    }
    for key, (ids, names) in mappings.items():
        matching = {name: values for name, values in params.items() if name in names and values and set(values) <= ids}
        if matching:
            # A model ID proves Z4 only with an independently observed BMW search scope or make mapping.
            bmw_ids = learned_ids(preceding, "BMW")
            make_proof = any(name in {"makeid", "makeids"} and set(values) & bmw_ids
                             for name, values in params.items())
            bmw_scope = any(re.search(r"\bbmw\b", normalized(item.get("text", ""))) for item in scoped)
            if key != "bmw_z4" or make_proof or bmw_scope:
                result[key] = criterion(
                    "verified", source="URL and observed selected control", values=matching, url=url,
                )
    numeric = [("price_max", {"maxprice", "maximumprice", "priceto", "priceuntil"}, 20000)]
    if kilometers:
        numeric.append(("kilometers_max", {"maxkm", "maxkilometers", "kilometersto", "kmto"}, 150000))
    for key, names, expected in numeric:
        for name, values in params.items():
            if name not in names or len(values) != 1:
                continue
            try:
                actual = float(values[0])
            except ValueError:
                continue
            result[key] = criterion("verified" if actual == expected else "failed",
                                    source="explicit URL upper bound", parameter=name, value=values[0], url=url)
    return result


def evaluate_outcomes(observations, *, original_prompt=False):
    """Acceptance oracle for the search goal and historical kilometer variant."""
    searches = [(index, observation) for index, observation in enumerate(observations) if search_page(observation)]
    index, search = searches[-1] if searches else (-1, {})
    criteria = filter_outcomes(search, observations[:index + 1], kilometers=original_prompt)
    if not original_prompt:
        criteria["first_result"] = criterion("unknown", reason="No ordered result list and final detail evidence")
        links = [item for item in search.get("anchors", [])
                 if item.get("result_card") and not item.get("in_panel") and detail_id(item.get("href", ""))]
        # Repeated title/image links belong to the same first card; DOM order is preserved.
        first = links[0] if links else None
        final_url = observations[-1].get("url", "") if observations else ""
        final_detail = detail_id(final_url)
        if first and final_detail:
            matches = detail_id(first["href"]) == final_detail
            criteria["first_result"] = criterion(
                "verified" if matches else "failed", source="ordered listing link and final document URL",
                expected_url=first["href"], final_url=final_url, label=first.get("text", ""),
            )
        elif first and search_page(observations[-1]):
            criteria["first_result"] = criterion("failed", reason="Final document remains on the result list",
                                                  expected_url=first["href"], final_url=final_url)
    states = {item["status"] for item in criteria.values()}
    return {"status": "verified" if states == {"verified"} else "failed" if "failed" in states else "unknown",
            "criteria": criteria, "search_observation_index": index}


def counters(state):
    return {
        "decisions": state.get("model_calls", 0), "decision_http_attempts": state.get("model_http_attempts", 0),
        "text_calls": len(state.get("text_calls", [])), "verification_calls": state.get("verification_calls", 0),
        "verification_http_attempts": state.get("verification_http_attempts", 0),
        "browser_actions": len(state.get("history", [])), "candidate_windows": state.get("candidate_windows", 0),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-prompt", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--close-tab", action="store_true", help="Close the working tab when the run ends.")
    args = parser.parse_args(argv)
    goal = ORIGINAL_GOAL if args.original_prompt else GOAL
    output = args.output or ROOT / "artifacts" / ("coches-validation-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    output.mkdir(parents=True, exist_ok=True)
    observations, agent, failure = [], None, None
    trace_path = output / "trace.jsonl"

    with trace_path.open("w", encoding="utf-8") as trace:
        def record(event, **data):
            trace.write(json.dumps({"event": event, **data}, ensure_ascii=False, default=str) + "\n")
            trace.flush()

        original_post = model.post_json

        def traced_post(url, key, body):
            result = original_post(url, key, body)
            if body.get("response_format", {}).get("json_schema", {}).get("name") == "browser_outcome_review":
                record("verification_transport", request=dict(body), response=result)
            return result

        model.post_json = traced_post

        class TracedAgent(Agent):
            def _choose(self, config):
                try:
                    super()._choose(config)
                finally:
                    record("choice", request=self.state["page"].get("_candidate_request"),
                           decision=self.state.get("decision"), actions=self.state["page"].get("actions", []))

            def _execute(self, body, config):
                try:
                    return super()._execute(body, config)
                except Exception as error:
                    record("execution_error", error_type=type(error).__name__, message=str(error),
                           browser_actions=len(self.state["history"]))
                    raise

        def independent_read():
            try:
                observed = agent.browser.evaluate(READ_DOM)
                if not isinstance(observed, dict):
                    raise ValueError("Independent DOM reader returned no document")
                observed["after_action"] = len(agent.state["history"])
                observations.append(observed)
                record("independent_dom", observation_index=len(observations) - 1, observed=observed)
            except Exception as error:
                record("independent_read_error", error_type=type(error).__name__, message=str(error))

        record("start", goal=goal, original_prompt=args.original_prompt, url="https://www.coches.net")
        try:
            load_environment()
            options = {"close_tab_on_exit": True} if args.close_tab else {}
            agent = TracedAgent("https://www.coches.net", goal, **options)
            independent_read()
            for state in agent.run():
                record("state", snapshot=state)
                independent_read()
            record("final_state", snapshot=agent.snapshot())
            independent_read()
        except Exception as error:
            failure = {"type": type(error).__name__, "message": str(error)}
            record("error", **failure)
            if agent is not None:
                independent_read()
        finally:
            model.post_json = original_post
            if agent is not None:
                try:
                    if args.close_tab:
                        agent.close()
                    else:
                        agent.disconnect()
                except Exception as error:
                    failure = failure or {"type": type(error).__name__, "message": str(error)}
                    record("close_error" if args.close_tab else "disconnect_error",
                           error_type=type(error).__name__, message=str(error))
        state = agent.state if agent is not None else {}
        report = {
            "goal": goal, "agent_status": state.get("status", "error"), "stop_reason": state.get("stop_reason"),
            "outcome": evaluate_outcomes(observations, original_prompt=args.original_prompt),
            "counts": counters(state), "elapsed_ms": state.get("elapsed_ms", 0), "error": failure,
            "trace": str(trace_path), "observations": len(observations),
        }
        record("report", report=report)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"agent_status": report["agent_status"], "outcome": report["outcome"],
                      "counts": report["counts"], "report": str(output / "report.json")}, ensure_ascii=False))
    return 0 if not failure and report["outcome"]["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
