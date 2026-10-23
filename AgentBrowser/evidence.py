"""Factual, bounded observations and effects; no goal inference or browser input."""

import hashlib
import json

CONTROL_FIELDS = ("value", "current_value", "checked", "selected", "pressed", "expanded",
                  "required", "invalid", "validation_message", "position", "href", "href_truncated", "submits_form")
MAX_CONTROLS = 80
MAX_CONTEXTS = 32
MAX_CHANGES = 40
MAX_EXECUTIONS = 60


def bounded_context(context):
    """Expose observed semantic grouping only, never selectors or executable data."""
    if not isinstance(context, dict):
        return {}
    return {key: str(context[key])[:limit] for key, limit in (("id", 80), ("kind", 40), ("heading", 160))
            if context.get(key) is not None}


def execution_history(history):
    """Compact the execution log, never the proposals, without inferring outcomes."""
    records = []
    offset = max(0, len(history) - MAX_EXECUTIONS)
    for index, entry in enumerate(history[offset:], start=offset + 1):
        record = {"step": entry.get("step", index)}
        trimmed = []
        for key, limit in (("operation", 40), ("kind", 40), ("action", 240), ("text", 512)):
            if entry.get(key) is not None:
                value = str(entry[key])
                record[key] = value[:limit]
                if len(value) > limit:
                    trimmed.append(key)
        if isinstance(entry.get("context"), dict):
            record["context"] = bounded_context(entry["context"])
            trimmed.extend("context." + key for key, value in record["context"].items()
                           if value != str(entry["context"][key]))
        if trimmed:
            record["truncated_fields"] = trimmed
        records.append(record)
    if offset and records:
        records[0]["earlier_executions_omitted"] = offset
    return records


def _controls(page):
    inventory = page.get("inventory")
    source = (inventory.get("nodes", []) + inventory.get("globals", [])
              if inventory is not None else page.get("actions", []))
    controls, filled_nodes = {}, set()
    for item in source:
        node = item.get("node")
        if node is None:
            continue
        control = controls.setdefault(str(node), {"node": node, "observed_order": len(controls) + 1})
        # Fill/click pairs and scroll directions share one factual control.
        label = item.get("label", "")
        if not control.get("label") or item.get("kind") == "fill":
            control["label"] = str(label).split(" → ")[0][:240]
        if "role" in item:
            control["role"] = str(item["role"])[:40]
        if item.get("context"):
            control["context"] = bounded_context(item["context"])
        href = item.get("href")
        truncated_href = (bool(control.get("href_truncated")) or bool(item.get("href_truncated"))
                          or isinstance(href, str) and len(href) > 2000)
        for key in CONTROL_FIELDS:
            if key in item:
                if key in {"value", "current_value"} and str(node) in filled_nodes and item.get("kind") != "fill":
                    continue
                value = item[key]
                # An option action's value is an alternative, not the field value.
                if key == "value" and item.get("kind") == "select":
                    value = item.get("current_value", "")
                control[key] = value[:2000] if isinstance(value, str) else value
        if truncated_href:
            control["href_truncated"] = True
        if item.get("kind") == "fill":
            filled_nodes.add(str(node))
    return controls


def _contexts(page, controls):
    inventory = page.get("inventory", {})
    contexts = {}
    for context in [*page.get("contexts", inventory.get("contexts", [])),
                    *(control["context"] for control in controls.values() if "context" in control)]:
        context = bounded_context(context)
        if context.get("id"):
            contexts.setdefault(context["id"], {}).update(context)
    return contexts


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def semantic_digest(page):
    """Track local observable state, not timestamps, geometry or DOM fingerprints."""
    controls = _controls(page)
    return _digest({
        "url": page.get("url", ""), "title": page.get("title", ""), "text": page.get("text", "")[:6000],
        "controls": controls, "contexts": _contexts(page, controls),
    })


def progress_digest(page):
    """Recognize revisited observable states despite DOM identity churn.

    This is only a cycle-detection hint, never evidence identity, freshness or
    proof of completion. Keep control order, duplicate controls, grouping,
    values and scroll positions, but omit DOM IDs and layout geometry.
    """
    inventory = page.get("inventory")
    source = (inventory.get("nodes", []) + inventory.get("globals", [])
              if inventory is not None else page.get("actions", []))
    contexts, context_indexes = [], {}

    def context_index(context):
        if not isinstance(context, dict) or not context:
            return None
        semantic = {key: context[key] for key in ("kind", "heading") if key in context}
        identity = context.get("id")
        if identity is not None and identity in context_indexes:
            index = context_indexes[identity]
            contexts[index].update(semantic)
            return index
        index = len(contexts)
        contexts.append(semantic)
        if identity is not None:
            context_indexes[identity] = index
        return index

    for context in page.get("contexts", (inventory or {}).get("contexts", [])):
        context_index(context)
    controls, filled_nodes = {}, set()
    fields = tuple(key for key in CONTROL_FIELDS if key != "position") + ("readonly", "disabled")
    for position, item in enumerate(source):
        if item.get("kind") == "wait":
            continue
        # Identity groups fill/click alternatives, but never enters the digest.
        identity = ("node", str(item["node"])) if item.get("node") is not None else ("entry", position)
        control = controls.setdefault(identity, {})
        label = str(item.get("label", ""))
        if item.get("kind") == "scroll":
            label = label.removesuffix(" — scroll down").removesuffix(" — scroll up")
        elif item.get("kind") == "select":
            label = label.split(" → ")[0]
        if "label" not in control or item.get("kind") == "fill":
            control["label"] = label
        if "role" in item:
            control["role"] = item["role"]
        if item.get("context"):
            control["context"] = context_index(item["context"])
        for key in fields:
            if key not in item:
                continue
            if key in {"value", "current_value"} and identity in filled_nodes and item.get("kind") != "fill":
                continue
            control[key] = (item.get("current_value", "")
                            if key == "value" and item.get("kind") == "select" else item[key])
        if item.get("kind") == "fill":
            filled_nodes.add(identity)
        if inventory is None and item.get("kind"):
            control["operations"] = sorted(set(control.get("operations", [])) | {item["kind"]})
        elif "operations" in item:
            control["operations"] = item["operations"]
        for key in ("options_complete", "options_inspected"):
            if key in item:
                control[key] = item[key]
        if "options" in item:
            control["options"] = [
                {key: option[key] for key in ("label", "value", "selected", "disabled") if key in option}
                for option in item["options"]
            ]
        elif item.get("kind") == "select":
            control.setdefault("options", []).append({
                "label": str(item.get("label", "")).split(" → ")[-1], "value": item.get("value", ""),
            })
        if item.get("kind") == "scroll":
            scroll = item.get("scroll", {})
            control["scroll"] = {key: scroll[key] for key in ("x", "y") if key in scroll}
            if "y" not in control["scroll"] and "position" in item:
                control["scroll"]["y"] = item["position"]
            directions = control.setdefault("directions", [])
            if item.get("direction") not in directions:
                directions.append(item.get("direction"))
    page_key = page.get("page_key", [])
    scroll = page.get("scroll", {})
    return _digest({
        "url": page.get("url", ""), "title": page.get("title", ""), "text": page.get("text", ""),
        "controls": list(controls.values()), "contexts": contexts,
        "scroll": {"x": scroll.get("x", page_key[2] if len(page_key) > 2 else 0),
                   "y": scroll.get("y", page_key[3] if len(page_key) > 3 else 0)},
    })


def _scope(page):
    # timeOrigin in page_key identifies this document, not elapsed run time.
    # It scopes references across reloads, but is excluded from cycle detection.
    page_key = page.get("page_key")
    epoch = page_key[0] if isinstance(page_key, (tuple, list)) and page_key else None
    return _digest({"semantic": semantic_digest(page), "document_epoch": epoch})[:16]


def evidence_view(page):
    """Current observable facts with references tied to this document and state.

    Truncation is explicit. Missing facts and disappearing controls never prove
    that a goal was met or that their former values were applied.
    """
    scope = _scope(page)
    facts = [{"ref": f"{scope}:page:{key}", "kind": "page", key: str(page.get(key, ""))[:limit]}
             for key, limit in (("url", 2000), ("title", 500), ("text", 6000))]
    controls = _controls(page)
    contexts = _contexts(page, controls)
    for context in list(contexts.values())[:MAX_CONTEXTS]:
        facts.append({"ref": f"{scope}:context:{context['id']}", "kind": "context", **context})
    for node, control in list(controls.items())[:MAX_CONTROLS]:
        facts.append({"ref": f"{scope}:node:{node}", "kind": "control", **control})
    if len(controls) > MAX_CONTROLS or len(contexts) > MAX_CONTEXTS:
        facts.append({"ref": f"{scope}:coverage", "kind": "coverage",
                      "controls_omitted": max(0, len(controls) - MAX_CONTROLS),
                      "contexts_omitted": max(0, len(contexts) - MAX_CONTEXTS)})
    return facts


def navigation_source(page, action, step):
    """Retain at most 16 observed source facts, without inferring result ranking.

    This packet must only be published as history after a different URL is
    observed. The selected control's identity is an association, not proof that
    its destination loaded or any goal was satisfied.
    """
    scope = _scope(page)
    controls = _controls(page)
    contexts = _contexts(page, controls)
    selected = controls.get(str(action.get("node")))
    facts = []
    if selected:
        facts.append({"ref": f"{scope}:node:{selected['node']}", "kind": "control",
                      **selected, "selected_control": True})
        context = contexts.get(selected.get("context", {}).get("id"))
        if context:
            facts.append({"ref": f"{scope}:context:{context['id']}", "kind": "context", **context})
    for key, limit in (("url", 2000), ("title", 500), ("text", 6000)):
        facts.append({"ref": f"{scope}:page:{key}", "kind": "page",
                      key: str(page.get(key, ""))[:limit]})
    links = [control for control in controls.values() if control.get("role") == "link"]
    position = selected.get("observed_order", 1) if selected else 1
    neighbors = sorted(links, key=lambda control: abs(control["observed_order"] - position))
    # Reserve one fact to make the bounded order and omitted controls explicit.
    admitted = {fact["ref"] for fact in facts}
    for control in neighbors:
        ref = f"{scope}:node:{control['node']}"
        if ref not in admitted and len(facts) < 15:
            facts.append({"ref": ref, "kind": "control", **control})
            admitted.add(ref)
    represented = sum(fact["kind"] == "control" for fact in facts)
    facts.append({"ref": f"{scope}:navigation_source_coverage", "kind": "coverage",
                  "order_scope": "DOM order of observed controls only; not global result ranking",
                  "controls_observed": len(controls), "controls_retained": represented,
                  "controls_omitted": len(controls) - represented,
                  "extraction_complete": page.get("inventory", {}).get("coverage", {}).get("extraction_complete")})
    return [{**fact, "historical": True, "navigation_source": True, "observed_before_step": step}
            for fact in facts]


def compare_pages(before, after, action):
    """Return bounded factual effects; never treat a closed panel as success."""
    changes = []
    old, new = _controls(before), _controls(after)
    old_contexts, new_contexts = _contexts(before, old), _contexts(after, new)
    scope = _scope(after)
    if before.get("url") != after.get("url"):
        changes.append({"kind": "navigated", "ref": f"{scope}:page:url",
                        "before": before.get("url"), "after": after.get("url")})
    # Read the acted-on control first so unrelated list updates cannot hide its effect.
    acted_node = str(action.get("node"))
    nodes = sorted(new.keys() & old.keys(), key=lambda node: (node != acted_node, node))
    for node in nodes:
        for key in ("label", *CONTROL_FIELDS):
            if key in old[node] and key in new[node] and old[node][key] != new[node][key]:
                changes.append({"kind": key, "ref": f"{scope}:node:{node}", "node": new[node]["node"],
                                "label": new[node].get("label", ""),
                                "before": old[node][key], "after": new[node][key]})
    for context in sorted(new_contexts.keys() - old_contexts.keys()):
        changes.append({"kind": "opened", "ref": f"{scope}:context:{context}",
                        "context": new_contexts[context], "observed_only": True,
                        "description": "Context became observed; this does not prove a panel opened."})
    for context in sorted(old_contexts.keys() - new_contexts.keys()):
        # No current ref exists for a disappeared context. This is historical evidence.
        changes.append({"kind": "closed", "context": old_contexts[context], "observed_only": True,
                        "description": "Context became unobserved; this does not prove a panel closed or committed."})
    if before.get("text", "") != after.get("text", ""):
        changes.append({"kind": "visible_text_changed", "ref": f"{scope}:page:text",
                        "before": str(before.get("text", ""))[:320], "after": str(after.get("text", ""))[:320]})
    for node in sorted(new.keys() - old.keys()):
        changes.append({"kind": "control_observed", "ref": f"{scope}:node:{node}",
                        "node": new[node]["node"], "label": new[node].get("label", "")})
    for node in sorted(old.keys() - new.keys()):
        changes.append({"kind": "control_no_longer_observed", "node": old[node]["node"],
                        "label": old[node].get("label", "")})
    if not changes and semantic_digest(before) == semantic_digest(after):
        changes.append({"kind": "no_observed_change"})
    if len(changes) > MAX_CHANGES:
        return changes[:MAX_CHANGES] + [{"kind": "effects_truncated", "omitted": len(changes) - MAX_CHANGES}]
    return changes
