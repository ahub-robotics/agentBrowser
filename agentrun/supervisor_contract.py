"""Versioned plans and atomic review deltas for the deterministic coordinator.

These validators have no model, browser or store dependencies. They establish
identity, provenance and structural invariants; positive references alone never
establish the semantic truth of a requirement.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=160, pattern=r"\S")]
Reference = Annotated[str, StringConstraints(min_length=1, max_length=1024, pattern=r"\S")]
Instruction = Annotated[str, StringConstraints(min_length=1, max_length=4000, pattern=r"\S")]
Criterion = Annotated[str, StringConstraints(min_length=1, max_length=2000, pattern=r"\S")]
GoalQuote = Annotated[
    str, StringConstraints(min_length=1, max_length=2400, pattern=r"\S"),
    Field(description=(
        "Copy one exact, contiguous substring of the original goal, including its punctuation and whitespace. "
        "Do not paraphrase, concatenate separate fragments, insert ellipses or add text. This applies to every "
        "requirement, task, loop, discovery template and item step, including audit replacements. A broader "
        "contiguous quote may span several conditions; keep the instruction and success criteria specific."
    )),
]
Status = Literal["satisfied", "unsatisfied", "unknown"]
Phase = Literal["initialize", "audit", "review"]
NextAction = Literal["continue", "inspect", "observe", "repair", "feedback", "stop"]
FeedbackReason = Literal["missing_user_information", "uncertain_effect", "external_intervention"]


class ContractError(ValueError):
    """A bounded local explanation safe to return for one model correction."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Requirement(StrictModel):
    id: Identifier
    goal_quote: GoalQuote
    description: Criterion
    status: Status
    evidence_refs: list[Reference] = Field(default_factory=list, max_length=64)


class TaskTemplate(StrictModel):
    id: Identifier
    instruction: Instruction
    success_criteria: list[Criterion] = Field(min_length=1, max_length=16)
    requirement_ids: list[Identifier] = Field(min_length=1, max_length=32)
    goal_quote: GoalQuote
    verification_scope: Literal["destination", "current"] = "current"


class PlanTask(TaskTemplate):
    depends_on: list[Identifier] = Field(default_factory=list, max_length=48)


class LoopPlan(StrictModel):
    id: Identifier
    goal_quote: GoalQuote
    description: Criterion
    requirement_ids: list[Identifier] = Field(min_length=1, max_length=32)
    depends_on: list[Identifier] = Field(default_factory=list, max_length=48)
    discovery: TaskTemplate
    steps: list[TaskTemplate] = Field(min_length=1, max_length=16)
    limit: int | None = Field(default=None, ge=1, le=100000)
    order: Literal["observed"] = "observed"


class PlanContract(StrictModel):
    version: Literal[2] = 2
    requirements: list[Requirement] = Field(min_length=1, max_length=32)
    tasks: list[PlanTask] = Field(default_factory=list, max_length=40)
    loops: list[LoopPlan] = Field(default_factory=list, max_length=16)
    execution_constraints: list[Criterion] = Field(default_factory=list, max_length=32)


class InitializePlan(StrictModel):
    contract: PlanContract


class AuditPlan(StrictModel):
    approved: bool
    problems: list[Criterion] = Field(default_factory=list, max_length=32)
    replacement_contract: PlanContract | None = None

    @model_validator(mode="after")
    def consistent_verdict(self):
        if self.approved and (self.problems or self.replacement_contract is not None):
            raise ValueError("An approved plan cannot have problems or a replacement")
        if not self.approved and not self.problems:
            raise ValueError("A rejected plan requires concrete problems")
        return self


class RequirementUpdate(StrictModel):
    id: Identifier
    status: Status
    evidence_refs: list[Reference] = Field(default_factory=list, max_length=64)


class SubtaskReview(StrictModel):
    delegation_id: Identifier
    status: Status
    evidence_refs: list[Reference] = Field(default_factory=list, max_length=64)
    destination_ref: Reference | None = Field(
        default=None,
        description="Only cite a redirect destination listed in the active item's navigation_destinations; "
        "leave null when no redirect was observed.",
    )


class DiscoveredItems(StrictModel):
    loop_id: Identifier
    item_refs: list[Reference] = Field(min_length=1, max_length=160)


class LoopReview(StrictModel):
    loop_id: Identifier
    exhausted: bool = Field(description=(
        "Discovery coverage is complete for the requested scope. Registered items may still need processing; "
        "this does not mark their steps or the whole loop's outcomes complete."
    ))
    evidence_refs: list[Reference] = Field(default_factory=list, max_length=64)


class AttemptReview(StrictModel):
    attempt_id: Identifier = Field(
        description="Must exactly match an id in unresolved_attempts. Never cite an observed or rejected attempt; "
        "when unresolved_attempts is empty, return no reconciliation entries.",
    )
    evidence_refs: list[Reference] = Field(min_length=1, max_length=64)


class TaskRepair(StrictModel):
    instruction: Instruction
    success_criteria: list[Criterion] = Field(min_length=1, max_length=16)


class UserFeedback(StrictModel):
    question: Criterion
    feedback_reason: FeedbackReason
    missing_information: str = Field(default="", max_length=2000)
    blocked_requirement_ids: list[Identifier] = Field(min_length=1, max_length=32)
    user_intervention: Criterion

    @model_validator(mode="after")
    def missing_fact(self):
        if self.feedback_reason == "missing_user_information" and not self.missing_information.strip():
            raise ValueError("Missing user information requires a concrete missing fact")
        return self


class ProgressReview(StrictModel):
    requirement_updates: list[RequirementUpdate] = Field(default_factory=list, max_length=32)
    subtask_review: SubtaskReview | None = None
    discovered: list[DiscoveredItems] = Field(default_factory=list, max_length=16)
    loop_reviews: list[LoopReview] = Field(default_factory=list, max_length=16)
    reconcile_attempts: list[AttemptReview] = Field(default_factory=list, max_length=16)
    next_action: NextAction
    repair: TaskRepair | None = None
    feedback: UserFeedback | None = None
    stop_reason: Criterion | None = None

    @model_validator(mode="after")
    def matching_intent(self):
        for action, field in (("repair", "repair"), ("feedback", "feedback"), ("stop", "stop_reason")):
            if (self.next_action == action) != (getattr(self, field) is not None):
                raise ValueError(f"{field} is required only for next_action={action}")
        return self


PHASE_SCHEMAS = {"initialize": InitializePlan, "audit": AuditPlan, "review": ProgressReview}
PHASE_TOOLS = {"initialize": "initialize_plan", "audit": "audit_plan", "review": "review_progress"}


def validate_response(raw, phase):
    """Validate one phase response, accepting either tool arguments or its envelope."""
    if phase not in PHASE_SCHEMAS:
        raise ContractError("Unknown coordinator phase")
    if not isinstance(raw, Mapping):
        raise ContractError("A coordinator response must be an object")
    fields = dict(raw)
    if "contract_version" in fields and (
        type(fields["contract_version"]) is not int or fields["contract_version"] != 2
    ):
        raise ContractError("A v2 coordinator response requires contract_version=2")
    if fields.get("phase", phase) != phase:
        raise ContractError("The response does not match the current coordinator phase")
    fields.pop("contract_version", None)
    fields.pop("phase", None)
    result = PHASE_SCHEMAS[phase].model_validate(fields).model_dump()
    return {"contract_version": 2, "phase": phase, **result}


def _unique(values, label):
    if len(values) != len(set(values)):
        raise ContractError(f"Duplicate {label}")


def _facts(evidence, links=()):
    values = evidence.values() if isinstance(evidence, Mapping) else evidence or []
    return {fact["ref"]: fact for fact in [*values, *links] if isinstance(fact, dict) and "ref" in fact}


def _references(refs, facts, sent_refs, *, required=False, after=None):
    _unique(refs, "evidence references")
    if required and not refs:
        raise ContractError("Positive observed evidence is required")
    if any(ref not in facts or ref not in sent_refs for ref in refs):
        raise ContractError("Evidence reference was not supplied")
    if after is not None and not any(facts[ref].get("observed_at", -1) > after for ref in refs):
        raise ContractError("New postcondition evidence is required")


def goal_coverage(goal, requirements):
    """Report lexical omissions; semantic plan completeness belongs to the audit."""
    covered = [not char.isalnum() for char in goal]
    for requirement in requirements:
        quote = requirement["goal_quote"]
        start = goal.find(quote)
        while start >= 0:
            covered[start:start + len(quote)] = [True] * len(quote)
            start = goal.find(quote, start + len(quote))
    missing, start = [], None
    for index, matched in enumerate([*covered, True]):
        if not matched and start is None:
            start = index
        if matched and start is not None:
            missing.append(goal[start:index])
            start = None
    return {"complete": all(covered), "uncovered_words": missing[:200], "omitted_words": max(0, len(missing) - 200)}


def _quoted_entries(contract):
    for collection in ("requirements", "tasks", "loops"):
        for index, entry in enumerate(contract[collection]):
            yield f"{collection}[{index}].goal_quote", entry
    for index, loop in enumerate(contract["loops"]):
        yield f"loops[{index}].discovery.goal_quote", loop["discovery"]
        for step_index, step in enumerate(loop["steps"]):
            yield f"loops[{index}].steps[{step_index}].goal_quote", step


def repair_goal_quote_punctuation(raw, goal):
    """Remove one invented trailing punctuation mark at an unambiguous word boundary.

    Only provenance fields change, on an independent copy. The remaining text
    must already occur exactly once in the goal, start after whitespace (or at
    the start), end with a letter and precede whitespace (or the end). This does
    not repair words, numbers, internal punctuation, whitespace or joined quotes.
    The caller must still validate the entire contract and audit its semantics.
    """
    if not isinstance(goal, str) or not goal.strip():
        raise ContractError("Supply one natural-language goal")
    contract = PlanContract.model_validate(raw).model_dump()
    repairs = []
    for path, entry in _quoted_entries(contract):
        quote = entry["goal_quote"]
        if quote in goal or len(quote) < 2 or quote[-1] not in ",.;:!?" or not quote[-2].isalpha():
            continue
        candidate = quote[:-1]
        start = goal.find(candidate)
        end = start + len(candidate)
        if (start < 0 or goal.find(candidate, start + 1) >= 0
                or (start > 0 and not goal[start - 1].isspace())
                or (end < len(goal) and not goal[end].isspace())):
            continue
        entry["goal_quote"] = goal[start:end]
        repairs.append({"field": path, "original": quote, "replacement": entry["goal_quote"]})
    return contract, repairs


def validate_contract(raw, goal, *, evidence=None, sent_refs=None):
    """Return an independent validated plan without mutating the supplied state."""
    if not isinstance(goal, str) or not goal.strip():
        raise ContractError("Supply one natural-language goal")
    contract = PlanContract.model_validate(raw).model_dump()
    requirements = contract["requirements"]
    requirement_ids = [item["id"] for item in requirements]
    _unique(requirement_ids, "requirement IDs")
    facts = _facts(evidence)
    sent_refs = set(facts if sent_refs is None else sent_refs)
    for requirement in requirements:
        _references(requirement["evidence_refs"], facts, sent_refs, required=requirement["status"] == "satisfied")
    entries = [*contract["tasks"], *contract["loops"]]
    ids = [entry["id"] for entry in entries]
    _unique(ids, "task or loop IDs")
    templates = [template for loop in contract["loops"] for template in [loop["discovery"], *loop["steps"]]]
    for path, entry in _quoted_entries(contract):
        if entry["goal_quote"] not in goal:
            raise ContractError(f"{path} must quote the original goal as one exact, contiguous substring")
    if not goal_coverage(goal, requirements)["complete"]:
        raise ContractError("Requirements omit parts of the original goal")
    represented = {item["id"] for item in requirements if item["status"] == "satisfied"}
    for entry in [*entries, *templates]:
        _unique(entry["requirement_ids"], "task requirement IDs")
        if not set(entry["requirement_ids"]) <= set(requirement_ids):
            raise ContractError("Task references an unknown requirement")
        represented.update(entry["requirement_ids"])
    if represented != set(requirement_ids):
        raise ContractError("Every pending requirement needs planned work")
    for loop in contract["loops"]:
        _unique([loop["discovery"]["id"], *[step["id"] for step in loop["steps"]]], "loop template IDs")
        for template in [loop["discovery"], *loop["steps"]]:
            if not set(template["requirement_ids"]) <= set(loop["requirement_ids"]):
                raise ContractError("A loop template must reference its loop requirements")
    dependencies = {entry["id"]: entry["depends_on"] for entry in entries}
    for entry in entries:
        _unique(entry["depends_on"], "dependencies")
        if not set(entry["depends_on"]) <= set(ids):
            raise ContractError("Task references an unknown dependency")
    visited, visiting = set(), set()

    def visit(key):
        if key in visiting:
            raise ContractError("Plan dependencies contain a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in dependencies[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in ids:
        visit(key)
    return contract


def validate_progress_review(raw, payload):
    """Validate all review deltas before the coordinator commits any of them.

    ``evidence`` may be the complete local registry while ``sent_refs`` limits
    citations to facts actually presented. No result or item is marked complete
    here: the runtime also verifies its scheduled step and destination context.
    """
    review = validate_response(raw, "review")
    contract = payload.get("contract") or {}
    requirements = payload.get("requirements", contract.get("requirements", []))
    requirements = deepcopy({item["id"]: item for item in requirements})
    loops = payload.get("loops", contract.get("loops", []))
    loop_ids = set(loops) if isinstance(loops, Mapping) else {loop["id"] for loop in loops}
    facts = _facts(payload.get("evidence"), payload.get("observed_links", []))
    sent_refs = set(payload.get("sent_refs", facts))
    validation_errors = []
    for field, key in (("requirement_updates", "id"), ("discovered", "loop_id"),
                       ("loop_reviews", "loop_id"), ("reconcile_attempts", "attempt_id")):
        _unique([item[key] for item in review[field]], field)
    for update in review["requirement_updates"]:
        if update["id"] not in requirements:
            raise ContractError("Review references an unknown requirement")
        _references(update["evidence_refs"], facts, sent_refs, required=update["status"] == "satisfied")
        requirements[update["id"]].update(update)
    for discovery in review["discovered"]:
        if discovery["loop_id"] not in loop_ids:
            raise ContractError("Discovery references an unknown loop")
        _references(discovery["item_refs"], facts, sent_refs, required=True)
        for ref in discovery["item_refs"]:
            fact = facts[ref]
            href = fact.get("href")
            parsed = urlparse(href) if isinstance(href, str) else None
            if (fact.get("kind") != "control" or not parsed or parsed.scheme not in {"http", "https"}
                    or not parsed.netloc or len(href) > 2000 or fact.get("href_truncated")):
                raise ContractError("Discovered items require complete observed HTTP links")
    for loop in review["loop_reviews"]:
        if loop["loop_id"] not in loop_ids:
            raise ContractError("Review references an unknown loop")
        _references(loop["evidence_refs"], facts, sent_refs, required=loop["exhausted"])
    subtask = review["subtask_review"]
    last = payload.get("last_result") or {}
    if subtask:
        try:
            if subtask["delegation_id"] != last.get("delegation_id"):
                raise ContractError("Subtask review must identify the last delegation")
            satisfied = subtask["status"] == "satisfied"
            _references(subtask["evidence_refs"], facts, sent_refs, required=satisfied,
                        after=last.get("started_observation") if satisfied else None)
            destination_ref = subtask["destination_ref"]
            if destination_ref is not None:
                work = payload.get("active_work") or {}
                item_id = work.get("item_id")
                item = (payload.get("items") or {}).get(item_id)
                if work.get("kind") != "item" or item is None or item.get("started_observation") is None:
                    raise ContractError("A redirect review requires the active observed item")
                if last.get("item_id") is not None and last["item_id"] != item_id:
                    raise ContractError("The redirect belongs to a different item's delegation")
                _references([destination_ref], facts, sent_refs, required=True,
                            after=item["started_observation"])
                destinations = item.get("navigation_destinations", {})
                if destination_ref not in destinations:
                    raise ContractError("Redirect destination was not observed after opening this item")
                destination = destinations[destination_ref]
                parsed = urlparse(destination) if isinstance(destination, str) else None
                fact = facts[destination_ref]
                if (not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc
                        or len(destination) > 2000 or fact.get("kind") != "page"
                        or fact.get("url") != destination or fact.get("observed_url", destination) != destination):
                    raise ContractError("Redirect evidence must identify the complete observed destination URL")
        except ContractError as error:
            validation_errors.append(str(error))
    attempts = {attempt["id"]: attempt for attempt in payload.get("unresolved_attempts", [])}
    for attempt in review["reconcile_attempts"]:
        try:
            if attempt["attempt_id"] not in attempts:
                raise ContractError("Review references an unknown uncertain attempt")
            floor = payload.get("attempt_observations", {}).get(attempt["attempt_id"])
            _references(attempt["evidence_refs"], facts, sent_refs, required=True, after=floor)
        except ContractError as error:
            validation_errors.append(str(error))
    if validation_errors:
        unique_errors = list(dict.fromkeys(validation_errors))[:5]
        if len(unique_errors) == 1:
            raise ContractError(unique_errors[0])
        raise ContractError("Multiple review validation errors: " + "; ".join(unique_errors))
    action = review["next_action"]
    available = payload.get("available_actions")
    if available is not None and action not in available:
        raise ContractError("The requested next action is not available")
    if action == "inspect" and payload.get("next_evidence_offset") is None:
        raise ContractError("All retained evidence has been supplied; use observe for a fresh snapshot")
    if action == "repair":
        if not last.get("delegation_id") or not last.get("task"):
            raise ContractError("Repair requires the current executed task")
        unresolved = set(attempts) - {item["attempt_id"] for item in review["reconcile_attempts"]}
        if unresolved:
            raise ContractError("Reconcile uncertain effects before repairing a task")
        if subtask and subtask["status"] == "satisfied":
            raise ContractError("A satisfied subtask cannot be repaired for execution")
    if action == "feedback":
        feedback = review["feedback"]
        blocked = feedback["blocked_requirement_ids"]
        _unique(blocked, "blocked requirement IDs")
        if any(key not in requirements or requirements[key]["status"] == "satisfied" for key in blocked):
            raise ContractError("Feedback must concern known pending requirements")
        reasons = payload.get("feedback_reasons")
        if reasons is not None and feedback["feedback_reason"] not in reasons:
            raise ContractError("The feedback reason is not available")
        if feedback["feedback_reason"] == "uncertain_effect" and not attempts:
            raise ContractError("No uncertain browser attempt exists")
    return review
