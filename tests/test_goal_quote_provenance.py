"""Literal goal provenance stays strict and diagnosable at every plan scope."""

from copy import deepcopy

import pytest
from test_supervisor_contract import GOAL, contract
from test_supervisor_policy import ToolModel
from test_supervisor_policy_v2 import payload, tool_call

from agentrun.supervisor_contract import ContractError, validate_contract
from agentrun.supervisor_policy import SupervisorPolicy

SCOPES = [
    (("requirements", 0), "requirements[0].goal_quote"),
    (("tasks", 0), "tasks[0].goal_quote"),
    (("loops", 0), "loops[0].goal_quote"),
    (("loops", 0, "discovery"), "loops[0].discovery.goal_quote"),
    (("loops", 0, "steps", 0), "loops[0].steps[0].goal_quote"),
]


def entry_at(plan, path):
    for key in path:
        plan = plan[key]
    return plan


@pytest.mark.parametrize(("path", "field"), SCOPES)
@pytest.mark.parametrize("quote", [
    "Find books. return to the list.",  # Both fragments occur, but not contiguously.
    "Find literature and examine the results.",  # A paraphrase is not provenance.
])
def test_invalid_quote_names_only_its_structural_field_without_rewriting_input(path, field, quote):
    raw = contract()
    entry = entry_at(raw, path)
    entry.update(goal_quote=quote, id="private-identifier-do-not-log")
    before = deepcopy(raw)

    with pytest.raises(ContractError) as caught:
        validate_contract(raw, GOAL)

    detail = str(caught.value)
    assert detail.startswith(field + " ")
    assert "quote the original goal" in detail
    assert quote not in detail and entry["id"] not in detail
    assert raw == before


@pytest.mark.parametrize(("path", "_field"), SCOPES)
def test_broader_literal_quote_does_not_expand_or_rewrite_the_work(path, _field):
    raw = contract()
    entry_at(raw, path)["goal_quote"] = GOAL
    before = deepcopy(raw)

    result = validate_contract(raw, GOAL)

    assert result == before and raw == before


@pytest.mark.parametrize("quote", [GOAL.replace(" ", "\u00a0"), GOAL.rstrip(".") + "!"])
def test_quote_whitespace_and_punctuation_are_not_silently_normalized(quote):
    raw = contract()
    raw["tasks"][0]["goal_quote"] = quote
    with pytest.raises(ContractError, match=r"tasks\[0\]\.goal_quote"):
        validate_contract(raw, GOAL)


def resolve(schema, value):
    while "$ref" in value:
        value = schema["$defs"][value["$ref"].rsplit("/", 1)[1]]
    return value


@pytest.mark.parametrize("phase", ["initialize", "audit"])
def test_model_tool_schema_explains_literal_provenance_for_every_nested_quote(phase):
    fields = ({"contract": contract()} if phase == "initialize" else {
        "approved": False, "problems": ["Revise the requested outcome."], "replacement_contract": contract(),
    })
    model = ToolModel(responses=[tool_call(f"{phase}_plan", fields)])
    SupervisorPolicy(model=model).invoke(payload(phase))
    schema = model.tools_seen[0].args_schema.model_json_schema()
    plan_field = schema["properties"]["contract" if phase == "initialize" else "replacement_contract"]
    if "anyOf" in plan_field:
        plan_field = next(value for value in plan_field["anyOf"] if "$ref" in value)
    plan_schema = resolve(schema, plan_field)
    for path, _field in SCOPES:
        entry_schema = plan_schema
        for key in path:
            entry_schema = resolve(schema, entry_schema["items"] if isinstance(key, int)
                                   else entry_schema["properties"][key])
        description = entry_schema["properties"]["goal_quote"]["description"]
        assert "exact, contiguous substring of the original goal" in description
        assert "Do not paraphrase, concatenate separate fragments" in description
    assert model.calls == 1
