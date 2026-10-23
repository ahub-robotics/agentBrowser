"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from .candidates import DEFAULT_LIMITS, preview_actions

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"


def read_state_expression(limits=DEFAULT_LIMITS):
    """Use the same extraction limits for observations and read-only freshness checks."""
    return READ_STATE.replace("/*__JEV_LIMITS__*/", json.dumps(limits.snapshot_limits()))


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class MutationUncertain(RuntimeError):
    """Input may have reached Chrome; reconciliation is required, never replay."""


def _back_action(history):
    """Expose only the immediately preceding entry actually observed in CDP."""
    index, entries = history.get("currentIndex"), history.get("entries", [])
    if type(index) is not int or not 0 < index < len(entries):
        return None
    previous, current = entries[index - 1], entries[index]
    if (type(previous.get("id")) is not int or type(current.get("id")) is not int
            or not isinstance(previous.get("url"), str) or not previous["url"]):
        return None
    # Initial blank documents do not represent a useful return destination.
    if previous["url"] == "about:blank":
        return None
    return {
        "id": "back", "kind": "back", "label": "Back to " + previous["url"],
        "history_entry": previous["id"], "current_history_entry": current["id"],
        "href": previous["url"], "history_current_url": current.get("url", ""),
    }


class Browser:
    def __init__(self, url, *, candidate_limits=None, enable_history=False):
        self.candidate_limits = candidate_limits or DEFAULT_LIMITS
        self.enable_history = enable_history
        self.observation_timeout = 2.0
        self.observation_deadline = None
        self.session_recoveries = 0
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self._configure_observation()
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    @classmethod
    def attach(cls, target_id, *, candidate_limits=None, enable_history=True):
        """Reconnect only an existing owned page, without creation or navigation."""
        if not isinstance(target_id, str) or not target_id:
            raise ValueError("Supply the original browser target ID")
        ensure_daemon()
        targets = cdp("Target.getTargets").get("targetInfos", [])
        if sum(item.get("targetId") == target_id and item.get("type") == "page" for item in targets) != 1:
            raise RuntimeError("Owned browser target is unavailable; refusing to switch or create a tab")
        browser = cls.__new__(cls)
        browser.candidate_limits = candidate_limits or DEFAULT_LIMITS
        browser.enable_history = enable_history
        browser.observation_timeout = 2.0
        browser.observation_deadline = None
        browser.session_recoveries = 0
        browser.target = target_id
        browser.session = cdp("Target.attachToTarget", targetId=target_id, flatten=True).get("sessionId")
        if not isinstance(browser.session, str) or not browser.session:
            raise RuntimeError("Could not reconnect to the owned browser target")
        try:
            browser._configure_observation()
        except Exception:
            browser.disconnect()
            raise
        return browser

    def call(self, method, **params):
        self._require_session()
        return cdp(method, session_id=self.session, **params)

    def _require_session(self):
        if not self.session:
            raise RuntimeError("Browser session is disconnected; the tab has been left open.")

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def _configure_observation(self):
        """Restore owned-tab rendering settings; this never navigates or dispatches page input."""
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)

    def _recover_read_session(self, error, *, deadline=None):
        """Reconnect only our existing target after an exact lost-session read failure."""
        if "Session with given id not found" not in str(error):
            raise error
        global_deadline = getattr(self, "observation_deadline", None)
        deadlines = [value for value in (deadline, global_deadline) if isinstance(value, (int, float))]
        if deadlines and time.monotonic() >= min(deadlines):
            raise RuntimeError("Observation deadline expired before lost-session recovery") from error
        recovered = getattr(self, "session_recoveries", 0)
        if recovered >= 2:
            raise RuntimeError("Browser session recovery limit reached (2)") from error
        self.session_recoveries = recovered + 1
        target = getattr(self, "target", None)
        targets = cdp("Target.getTargets").get("targetInfos", [])
        owned = [item for item in targets if item.get("targetId") == target and item.get("type") == "page"]
        if not target or len(owned) != 1:
            raise RuntimeError("Owned browser target is unavailable; refusing to switch or create a tab") from error
        # The lookup may consume the remaining budget. Never attach after that deadline.
        if deadlines and time.monotonic() >= min(deadlines):
            raise RuntimeError("Observation deadline expired before lost-session recovery") from error
        session = cdp("Target.attachToTarget", targetId=target, flatten=True).get("sessionId")
        if not isinstance(session, str) or not session:
            raise RuntimeError("Could not reconnect to the owned browser target") from error
        self.session = session
        if deadlines and time.monotonic() >= min(deadlines):
            raise RuntimeError("Observation deadline expired after lost-session recovery") from error
        # Chrome clears device metrics when a session detaches. Preserve the observation viewport.
        self._configure_observation()

    def observe(self, screenshot=True):
        self._require_session()
        started = time.monotonic()
        deadline = started + getattr(self, "observation_timeout", 2.0)
        global_deadline = getattr(self, "observation_deadline", None)
        if isinstance(global_deadline, (int, float)):
            deadline = min(deadline, global_deadline)
        after_input = getattr(self, "after_input", None)
        if after_input and time.monotonic() < deadline:
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,Math.min(action.observation_ms,autocomplete ? 200 : 50));
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps({**action, "observation_ms": max(0, (deadline-time.monotonic())*1000)}) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        previous_marker, latest, attempts = None, None, 0
        while True:
            try:
                latest = browser_operation(
                    {
                        "operation": "observe", "session": self.session, "screenshot": False,
                        "candidate_limits": getattr(self, "candidate_limits", DEFAULT_LIMITS),
                    }
                )
                attempts += 1
                readiness = latest.get("readiness")
                # Legacy providers have no readiness metadata. Do not change their observation contract.
                useful = not readiness or readiness.get("status") == "ready"
                stable = not after_input or not readiness or latest["marker"] == previous_marker
                if useful and stable or time.monotonic() >= deadline:
                    if getattr(self, "enable_history", False):
                        history = self.call("Page.getNavigationHistory")
                        action = _back_action(history)
                        latest["navigation_history"] = {
                            "current_entry": (
                                history.get("entries", [])[history["currentIndex"]].get("id")
                                if type(history.get("currentIndex")) is int
                                and 0 <= history["currentIndex"] < len(history.get("entries", [])) else None
                            ),
                            "back": action,
                        }
                        if action:
                            latest["actions"] = [*latest["actions"], action]
                            if "inventory" in latest:
                                latest["inventory"] = {
                                    **latest["inventory"],
                                    "globals": [*latest["inventory"].get("globals", []), action],
                                }
                        latest["fingerprint"] = fingerprint(latest)
                    if screenshot:
                        latest["screenshot"] = self.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
                    break
                previous_marker = latest.get("marker")
            except StalePage:
                attempts += 1
                if time.monotonic() >= deadline:
                    raise
            except RuntimeError as error:
                self._recover_read_session(error, deadline=deadline)
                # A replacement session requires a complete new observation, never replayed input.
                attempts += 1
                previous_marker, latest = None, None
            time.sleep(min(0.05, max(0, deadline-time.monotonic())))
        if latest.get("readiness"):
            latest["readiness"] = {
                **latest["readiness"], "reads": attempts,
                "waited_ms": round((time.monotonic()-started)*1000),
                "settled": useful and stable,
                "timed_out": not (useful and stable),
            }
        return latest

    def fresh(self, page, action=None):
        try:
            return self._fresh(page, action)
        except RuntimeError as error:
            self._recover_read_session(error)
            # Even an unchanged DOM cannot authorize the decision made under the lost session.
            return False

    def _fresh(self, page, action=None):
        if action is not None and action["kind"] == "reload":
            history = self.call("Page.getNavigationHistory")
            index, entries = history.get("currentIndex"), history.get("entries", [])
            return (type(index) is int and 0 <= index < len(entries)
                    and entries[index].get("url") == page.get("url"))
        if action is not None and action["kind"] == "back":
            if not getattr(self, "enable_history", False):
                return False
            if _back_action(self.call("Page.getNavigationHistory")) != action:
                return False
        expression = read_state_expression(getattr(self, "candidate_limits", DEFAULT_LIMITS))
        if "inventory" in page:
            if action is not None and (action["kind"] in {"click", "select"} or
                                       action["kind"] == "scroll" and "node" in action):
                node = action["node"]
                if type(node) is not int:
                    return False
                current = self.evaluate(
                    f"(() => {{ const state={expression}; "
                    f"return state ? [state.inventory_marker,state.guards[{node}]] : null; }})()"
                )
                return current == [page["inventory_marker"], page["guards"].get(str(node))]
            return self.evaluate(
                f"(() => {{ const state={expression}; return state?.marker ?? null; }})()"
            ) == page["marker"]
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        self._require_session()
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def disconnect(self):
        """Release the owned CDP session while preserving its tab and target ID."""
        if self.session:
            session, self.session = self.session, None
            try:
                cdp("Target.detachFromTarget", sessionId=session)
            except RuntimeError as error:
                # A session already lost/closed has nothing left to detach.
                if "Session with given id not found" not in str(error):
                    raise

    def close(self):
        """Explicitly close only the owned tab, including after disconnect()."""
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None
            self.session = None


def fingerprint(state):
    # A window is a selection over the DOM, not a new DOM state.
    keys = ("url", "text", "inventory_marker", "scroll") if "inventory_marker" in state else (
        "url", "text", "actions", "scroll",
    )
    content = {k: state[k] for k in keys}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    input_started = False

    def call(method, **params):
        nonlocal input_started
        if method.startswith("Input.") or method in {"Page.navigateToHistoryEntry", "Page.reload"}:
            input_started = True
        try:
            return cdp(method, session_id=session, **params)
        except Exception as error:
            if input_started:
                raise MutationUncertain("Browser input acknowledgement was lost; inspect before retrying.") from error
            raise

    def evaluate(expression):
        nonlocal input_started
        if operation == "act" and request["action"]["kind"] == "select":
            # Selection and its events happen inside this evaluation; losing its
            # acknowledgement cannot prove that the mutation was not dispatched.
            input_started = True
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise MutationUncertain("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "reload":
            call("Page.reload", ignoreCache=False)
        elif kind == "back":
            current = _back_action(call("Page.getNavigationHistory"))
            if current != action:
                raise StalePage("Navigation history changed. Observe again.")
            call("Page.navigateToHistoryEntry", entryId=action["history_entry"])
        elif kind == "scroll":
            if "node" in action:
                if type(action["node"]) is not int or action.get("direction") not in {"up", "down"}:
                    raise ValueError("Invalid observed scroll region")
                target = evaluate("""(action => {
                  const c=window.__jevFast,e=c?.nodes.get(action.node);
                  if (!e?.isConnected || !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ||
                      e.closest('[inert],[aria-hidden="true"]')) return null;
                  const root=e===document.scrollingElement,y=root ? scrollY : e.scrollTop;
                  const height=root ? innerHeight : e.clientHeight;
                  if ((action.direction==='up' && y<=0) ||
                      (action.direction==='down' && y+height>=e.scrollHeight-2)) return null;
                  if (!root && !/auto|scroll/.test(getComputedStyle(e).overflowY)) return null;
                  return c.scrollPoint(e);
                })(""" + json.dumps(action) + ")")
                if target is None:
                    raise StalePage("Scroll region changed, reached its boundary or is covered. Observe again.")
            else:
                target = {"x": 550, "y": 650}  # Compatibility with legacy observed page scroll controls.
            call("Input.dispatchMouseEvent", type="mouseWheel", **target, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                // Values and labels need not be unique. Select the exact observed option node.
                const option=Number.isInteger(action.option) ? window.__jevFast.nodes.get(action.option) : null;
                if (e.tagName!=='SELECT' || !option?.isConnected || option.closest('select')!==e ||
                    option.disabled || option.closest('optgroup[disabled]') || option.value!==action.value) return null;
                e.selectedIndex=option.index;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise MutationUncertain("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    limits = request.get("candidate_limits", DEFAULT_LIMITS)
    info = evaluate(read_state_expression(limits))
    if info is None:
        raise StalePage("Document is navigating")
    if "inventory" in info:
        info["actions"] = preview_actions(info["inventory"], limits)
        eligible = sum(
            len(node.get("options", [])) if "select" in node["operations"] else len(node["operations"])
            for node in info["inventory"]["nodes"]
        )
        info["omitted_actions"] = max(0, eligible - sum("node" in action for action in info["actions"]))
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
