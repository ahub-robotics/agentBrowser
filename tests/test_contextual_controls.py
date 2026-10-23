"""Contextual button names and toggle state reach the native policy unchanged."""

import json
from unittest.mock import Mock

import pytest

from AgentBrowser import model


@pytest.mark.parametrize("pressed", ["false", "true", "mixed"])
def test_contextual_toggle_payload_and_choice_keep_observed_button_identity(monkeypatch, pressed):
    # The DOM reader excludes the hidden native input; the observed, visible
    # buttons retain their own node identities even when their text is a sibling.
    page = {
        "url": "https://example.test/filters", "title": "Models", "text": "Z3 Z4",
        "inventory": {
            "version": 1,
            "nodes": [
                {"node": 41, "region": "row1", "role": "button", "label": "Z3",
                 "pressed": "false", "value": "", "operations": ["click"]},
                {"node": 42, "region": "row2", "role": "button", "label": "Z4",
                 "pressed": pressed, "value": "", "operations": ["click"]},
            ],
            "globals": [{"id": "wait", "kind": "wait", "label": "Wait"}],
            "coverage": {"extraction_complete": True},
        },
    }

    def response_for_request(_url, _key, body):
        def answer(question, selected):
            return {"choice": selected, "confidence": 1,
                    "probabilities": {key: float(key == selected) for key in question["criteria"]}}

        return {"model": "offline", "answers": {
            "operation": answer(body["questions"]["operation"], "CLICK"),
            "click_target": answer(body["questions"]["click_target"], "2"),
        }}

    post = Mock(side_effect=response_for_request)
    network = Mock(side_effect=AssertionError("Paid API calls are forbidden in tests"))
    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setattr(model.CLIENT, "post", network)
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline")

    decision = model.choose(page, "Select Z4", [])

    post.assert_called_once()
    network.assert_not_called()
    body = json.loads(post.call_args.args[2].content)
    assert body["state"]["elements"] == [
        {"index": "1", "role": "button", "label": "Z3", "pressed": "false",
         "value": "", "operations": ["CLICK"]},
        {"index": "2", "role": "button", "label": "Z4", "pressed": pressed,
         "value": "", "operations": ["CLICK"]},
    ]
    assert body["questions"]["click_target"]["criteria"]["2"] == {
        "element": "[2] Z4", "current_value": "", "role": "button", "pressed": pressed,
    }
    assert set(body["questions"]["click_target"]["criteria"]) == {"1", "2"}
    assert decision["operation"] == "CLICK"
    assert decision["target"] == "2"
    assert decision["choice"] == "e42_click"
    assert decision["probabilities"] == {"e41_click": 0.0, "e42_click": 1.0}
