"""Bounded, deterministic windows over an observed inventory; no browser input or inference."""

import hashlib
import json
from collections import Counter, deque
from dataclasses import asdict, dataclass


class CandidateBudgetExceeded(ValueError):
    """The complete request cannot fit without silently changing the user's goal."""

    reason = "budget_insufficient"


class CandidateTraversalExhausted(ValueError):
    """There is no usable next window in this traversal."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Candidate traversal stopped: {reason}")


@dataclass(frozen=True)
class CandidateLimits:
    """One authority for extraction, selection, transport and traversal limits."""

    max_nodes: int = 80
    max_options_per_select: int = 12
    max_actions: int = 250
    max_request_bytes: int = 96 * 1024
    max_controls_inspected: int = 5000
    max_options_inspected: int = 5000
    max_regions: int = 32
    max_windows: int = 120
    max_queries: int = 120
    max_seconds: float = 300

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "max_seconds":
                if type(value) not in (int, float) or not 0 < value < float("inf"):
                    raise ValueError(f"{name} must be finite and positive")
            elif type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def snapshot_limits(self):
        return {name: getattr(self, name) for name in (
            "max_controls_inspected", "max_options_inspected", "max_regions",
        )}


DEFAULT_LIMITS = CandidateLimits()
ELEMENT_KINDS = {"click", "fill", "select", "scroll"}


def element_action(action):
    """Scoped scrolls consume node quotas; legacy document scrolls remain globals."""
    return action["kind"] in ELEMENT_KINDS and action.get("node") is not None


def serialize_json(body):
    """The only wire serialization, also used to enforce the exact UTF-8 budget."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class SerializedBody(dict):
    """A mapping for existing provider adapters, with the already measured wire body."""

    def __init__(self, body):
        super().__init__(body)
        self.content = serialize_json(body)
        self.http_attempts = 0


def action_key(action):
    """Observed node, operation and option identity, never a display index or label."""
    if not element_action(action):
        return ("global", action["kind"], action["id"])
    option = action.get("option", action.get("option_id", action["id"])) if action["kind"] == "select" else None
    if action["kind"] == "scroll":
        option = action.get("direction", "down" if action.get("delta", 0) > 0 else "up")
    return (action["node"], action["kind"], option)


@dataclass(frozen=True, slots=True)
class _ObservedAction:
    """A small reference to one operation; shared node context is not expanded."""

    node: dict
    kind: str
    option: dict | None = None

    def __getitem__(self, key):
        if key == "kind":
            return self.kind
        if key == "id":
            suffix = f"_{self.option['option']}" if self.option is not None else ""
            return f"e{self.node['node']}_{self.kind}{suffix}"
        if key == "option" and self.option is not None:
            return self.option["option"]
        return self.node[key]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def materialize(self):
        base = {key: value for key, value in self.node.items() if key not in {"operations", "options"}}
        action = {**base, "kind": self.kind, "id": self["id"]}
        if self.option is not None:
            action.update(
                option=self.option["option"], value=self.option["value"],
                current_value=self.node.get("current_value", self.node.get("value", "")),
                label=f"{self.node['label']} → {self.option['label']}",
            )
        elif self.kind == "click" and "fill" in self.node["operations"]:
            action["label"] = "Open " + self.node["label"]
        return action


def _materialize(actions):
    return [action.materialize() if isinstance(action, _ObservedAction) else action for action in actions]


def _inventory_actions(inventory):
    actions = []
    for node in inventory.get("nodes", []):
        for kind in node.get("operations", []):
            if kind not in ELEMENT_KINDS:
                raise ValueError("Unsupported observed operation")
            if kind == "select":
                for option in node.get("options", []):
                    actions.append(_ObservedAction(node, kind, option))
            else:
                actions.append(_ObservedAction(node, kind))
    return actions + [dict(action) for action in inventory.get("globals", [])]


def _selection(actions, presented, limits, *, force_rounds=False):
    globals_ = [action for action in actions if not element_action(action)]
    if len(globals_) > limits.max_actions:
        raise CandidateBudgetExceeded("Available global controls exceed the action budget")
    pending = [action for action in actions if element_action(action) and action_key(action) not in presented]
    nodes = {action["node"] for action in pending}
    options = Counter(action["node"] for action in pending if action["kind"] == "select")
    if (not force_rounds and len(nodes) <= limits.max_nodes and len(pending) + len(globals_) <= limits.max_actions
            and all(count <= limits.max_options_per_select for count in options.values())):
        # Keep original fixture indices and observed order when allocation is unnecessary.
        return [action for action in actions
                if not element_action(action) or action_key(action) not in presented]

    by_node, regions = {}, {}
    for action in pending:
        node = action["node"]
        if node not in by_node:
            raw_region = str(action.get("region", "root"))
            if raw_region not in regions and len(regions) >= limits.max_regions:
                raw_region = "residual"
            regions.setdefault(raw_region, [])
            regions[raw_region].append(node)
            by_node[node] = []
        by_node[node].append(action)

    # Alternate structural regions and operation families before expanding any node.
    queues = {}
    for region, node_ids in regions.items():
        queues[region] = {}
        for node in node_ids:
            first = by_node[node][0]
            queues[region].setdefault(first["kind"], deque()).append(node)
    families = {
        kind: deque(groups[kind] for groups in queues.values() if kind in groups)
        for kind in ("fill", "click", "select", "scroll")
    }
    order = []
    while any(families.values()):
        for region_queues in families.values():
            if region_queues:
                queue = region_queues.popleft()
                order.append(queue.popleft())
                if queue:
                    region_queues.append(queue)

    selected, retained_nodes, retained_options = [], set(), Counter()
    max_elements = limits.max_actions - len(globals_)
    depth = 0
    while len(selected) < max_elements:
        progress = False
        for node in order:
            if depth >= len(by_node[node]):
                continue
            if node not in retained_nodes and len(retained_nodes) >= limits.max_nodes:
                continue
            action = by_node[node][depth]
            if action["kind"] == "select" and retained_options[node] >= limits.max_options_per_select:
                continue
            if len(selected) >= max_elements:
                break
            selected.append(action)
            retained_nodes.add(node)
            retained_options[node] += action["kind"] == "select"
            progress = True
        if not progress:
            break
        depth += 1
    return selected + globals_


def preview_actions(inventory, limits=DEFAULT_LIMITS):
    """Structural-only preview for browser callers; model requests also enforce bytes."""
    return _materialize(_selection(_inventory_actions(inventory), set(), limits))


@dataclass
class PackedWindow:
    actions: list
    coverage: dict
    digest: str
    body: SerializedBody
    request_bytes: int
    keys: frozenset
    window_id: int


class CandidateTraversal:
    def __init__(self, page, limits=DEFAULT_LIMITS):
        self.limits = limits
        inventory = page.get("inventory")
        self.actions = _inventory_actions(inventory) if inventory is not None else list(page.get("actions", []))
        self.extraction = dict(inventory.get("coverage", {})) if inventory is not None else {
            "scope": "legacy_observed_actions", "extraction_complete": not page.get("omitted_actions", 0),
            "controls_total_known": not page.get("omitted_actions", 0),
            "options_total_known": not page.get("omitted_actions", 0),
        }
        self.version = inventory.get("version", 1) if inventory is not None else 1
        self._selector_totals_known = {
            item["node"]: item.get("options_complete", self.extraction.get("options_total_known", True))
            for item in inventory.get("nodes", []) if "select" in item.get("operations", [])
        } if inventory is not None else {}
        semantics = (
            {**inventory, "nodes": [{key: value for key, value in node.items() if key != "rect"}
                                    for node in inventory.get("nodes", [])]}
            if inventory is not None else
            [{key: value for key, value in action.items() if key != "rect"} for action in self.actions]
        )
        self.inventory_id = hashlib.sha256(serialize_json({
            "url": page.get("url"), "marker": page.get("marker"), "actions": semantics,
            "coverage": self.extraction, "version": self.version,
        })).hexdigest()[:24]
        self.presented = set()
        self.window_id = 1
        self._window = None
        self._committed_window = None
        self._unrepresentable = set()
        self._all_keys = {action_key(action) for action in self.actions if element_action(action)}
        if len(self._all_keys) != sum(element_action(action) for action in self.actions):
            raise ValueError("Observed actions require unique node/operation/option identities")

    def _coverage(self, selected, digest, byte_trimmed=False, *, presented=None, window_id=None):
        presented = self.presented if presented is None else presented
        window_id = self.window_id if window_id is None else window_id
        element_actions = [action for action in selected if element_action(action)]
        selected_keys = {action_key(action) for action in element_actions}
        projected = presented | selected_keys
        remaining = self._all_keys - projected
        nodes = {action["node"] for action in self.actions if element_action(action)}
        selected_nodes = list(dict.fromkeys(action["node"] for action in element_actions))
        observed_options = Counter(action["node"] for action in self.actions if action["kind"] == "select")
        selected_options = Counter(action["node"] for action in selected if action["kind"] == "select")
        observed_operations = Counter(action["kind"] for action in self.actions)
        selected_operations = Counter(action["kind"] for action in selected)
        observed_regions = {str(action.get("region", "root")) for action in self.actions
                            if element_action(action)}
        selected_regions = {str(action.get("region", "root")) for action in element_actions}
        unsupported = self.extraction.get("unsupported_contexts", [])
        extraction_complete = bool(self.extraction.get("extraction_complete", True)) and not bool(unsupported)
        budget_complete = selected_keys == self._all_keys
        usable_remaining = remaining - self._unrepresentable
        more_available = bool(usable_remaining) and window_id < self.limits.max_windows
        reason = None
        if not more_available:
            reason = ("window_limit" if usable_remaining else "budget_insufficient" if remaining else
                      "inventory_exhausted" if extraction_complete else "extraction_incomplete")
        reasons = []
        if len(nodes) > self.limits.max_nodes:
            reasons.append("nodes")
        if any(count > self.limits.max_options_per_select for count in observed_options.values()):
            reasons.append("options_per_select")
        if len(self.actions) > self.limits.max_actions:
            reasons.append("actions")
        if byte_trimmed or self._unrepresentable:
            reasons.append("request_bytes")
        if self.presented:
            reasons.append("previous_windows")
        return {
            **self.extraction,
            "policy_version": "cs01-v1", "scope": self.extraction.get("scope", "observed_inventory"),
            "inventory_id": self.inventory_id, "version": self.version, "window_id": window_id,
            "selection_digest": digest,
            "extraction_complete": extraction_complete, "budget_complete": budget_complete,
            "complete": extraction_complete and budget_complete,
            "nodes_observed": len(nodes), "nodes_admitted": len(selected_nodes),
            "regions_observed": len(observed_regions), "regions_admitted": len(selected_regions),
            "operation_counts": {kind: {"observed": count, "admitted": selected_operations[kind]}
                                 for kind, count in observed_operations.items()},
            "actions_observed": len(self.actions), "actions_admitted": len(selected),
            "actions_total": len(self.actions) if extraction_complete else None,
            "options_admitted": sum(selected_options.values()),
            "options_omitted": sum(observed_options.values()) - sum(selected_options.values()),
            "selectors": [
                {"index": str(selected_nodes.index(node) + 1), "observed": observed_options[node],
                 "admitted": selected_options[node], "omitted": observed_options[node] - selected_options[node],
                 "total_known": self._selector_totals_known.get(node, True),
                 "total": observed_options[node] if self._selector_totals_known.get(node, True) else None}
                for node in selected_nodes if node in observed_options
            ],
            "presented_unique_actions": len(projected), "pending_known": len(remaining),
            "unrepresentable_actions": len(self._unrepresentable),
            "traversal_complete": not remaining, "more_available": more_available,
            "termination_reason": reason, "trim_reasons": reasons, "limits": asdict(self.limits),
        }

    def prepare(self, page, build_request):
        """Pack one window; preparation and freshness reads never move its cursor."""
        if self._window is not None:
            rebuilt = SerializedBody(build_request(page, self._window.actions, self._window.coverage))
            if rebuilt.content != self._window.body.content:
                raise ValueError("Candidate request context changed; start a new traversal")
            return self._window
        selected = _selection(self.actions, self.presented | self._unrepresentable, self.limits)
        globals_ = [action for action in selected if not element_action(action)]

        def pack(actions, byte_trimmed=False, *, presented=None, window_id=None):
            actions = _materialize(actions)
            keys = frozenset(action_key(action) for action in actions if element_action(action))
            digest = hashlib.sha256(serialize_json([action_key(action) for action in actions])).hexdigest()
            coverage = self._coverage(actions, digest, byte_trimmed, presented=presented, window_id=window_id)
            body = SerializedBody(build_request(page, actions, coverage))
            return PackedWindow(actions, coverage, digest, body, len(body.content), keys, self.window_id)

        def finish(window, byte_trimmed=False):
            if not window.keys and self._all_keys - self.presented - self._unrepresentable:
                raise CandidateBudgetExceeded("No node operation fits beside the reserved global controls")
            # Advertise MORE only after finding at least one usable next target.
            # This is local serialization, not a request or an extraction read.
            if window.coverage["more_available"]:
                pending = self._all_keys - self.presented - window.keys - self._unrepresentable
                for action in self.actions:
                    key = action_key(action)
                    if key not in pending:
                        continue
                    next_window = pack(
                        [action] + globals_, True,
                        presented=self.presented | window.keys, window_id=self.window_id + 1,
                    )
                    if next_window.request_bytes <= self.limits.max_request_bytes:
                        break
                    self._unrepresentable.add(key)
            actions = [action for action in window.actions if element_action(action)]
            final = pack(actions + globals_, byte_trimmed)
            while final.request_bytes > self.limits.max_request_bytes and actions:
                actions.pop()
                final = pack(actions + globals_, True)
            if final.request_bytes > self.limits.max_request_bytes:
                raise CandidateBudgetExceeded("Minimum request exceeds the candidate byte budget")
            if not final.keys and self._all_keys - self.presented - self._unrepresentable:
                raise CandidateBudgetExceeded("No pending candidate fits the request budget")
            self._window = final
            return final

        window = pack(selected)
        if window.request_bytes <= self.limits.max_request_bytes:
            # Preserve legacy action order when no global/control reorder is needed.
            if not window.coverage["more_available"]:
                if not window.keys and self._all_keys - self.presented - self._unrepresentable:
                    raise CandidateBudgetExceeded("No node operation fits beside the reserved global controls")
                self._window = window
                return window
            return finish(window)
        minimum = pack(globals_, True)
        if minimum.request_bytes > self.limits.max_request_bytes:
            raise CandidateBudgetExceeded("Goal, page context and minimum request exceed the candidate byte budget")

        # Byte pressure uses the same diversity order. A very large label must not
        # hide representable controls after it, nor cause identical MORE windows.
        while True:
            previously_impossible = len(self._unrepresentable)
            ordered = _selection(self.actions, self.presented | self._unrepresentable, self.limits, force_rounds=True)
            admitted = []
            for action in ordered:
                if not element_action(action):
                    continue
                trial = pack(admitted + [action] + globals_, True)
                if trial.request_bytes <= self.limits.max_request_bytes:
                    admitted.append(action)
                elif pack([action] + globals_, True).request_bytes > self.limits.max_request_bytes:
                    self._unrepresentable.add(action_key(action))
            # An impossible early node must release its quota for later nodes.
            if len(self._unrepresentable) == previously_impossible:
                return finish(pack(admitted + globals_, True), True)

    def commit_sent(self, window):
        """Only actions retained in an actually submitted request become presented."""
        if window is not self._window or window.window_id != self.window_id:
            raise ValueError("Cannot commit a stale candidate window")
        self.presented.update(window.keys)
        self._committed_window = self.window_id

    def advance(self):
        if self._window is None or self._committed_window != self.window_id:
            raise CandidateTraversalExhausted("no_progress")
        if not self._window.coverage["more_available"]:
            raise CandidateTraversalExhausted(self._window.coverage["termination_reason"] or "inventory_exhausted")
        if not self._window.keys:
            raise CandidateTraversalExhausted("no_progress")
        self.window_id += 1
        self._window = None
