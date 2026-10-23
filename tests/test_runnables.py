"""Offline LangChain contracts: keep Jev native and preserve invocation context."""

from unittest.mock import Mock

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableLambda

from AgentBrowser import model
from AgentBrowser.runnables import JevPolicy, TextValue


class Events(BaseCallbackHandler):
    def __init__(self):
        self.starts = []
        self.ends = []
        self.errors = []

    def on_chain_start(self, serialized, inputs, **kwargs):
        self.starts.append((inputs, kwargs))

    def on_chain_end(self, outputs, **kwargs):
        self.ends.append(outputs)

    def on_chain_error(self, error, **kwargs):
        self.errors.append(error)


def test_policy_maps_inputs_without_changing_provider_output():
    page, history = {"actions": []}, [{"action": "wait"}]
    result = {"choice": "DONE", "operation": "DONE", "raw_answers": {"operation": {}}}
    chooser = Mock(return_value=result)

    output = JevPolicy(chooser=chooser).invoke({"page": page, "goal": "Find a book", "history": history})

    assert output is result
    chooser.assert_called_once_with(page, "Find a book", history)
    assert chooser.call_args.args[0] is page
    assert chooser.call_args.args[2] is history


def test_text_adapter_preserves_complete_context_and_usage():
    context = {
        "goal": "Find a book",
        "field": {"label": "Search", "role": "textbox", "value": ""},
        "page": {"title": "Library", "text": "Search books"},
        "recent_actions": [{"action": "click"}],
    }
    result = ("a book", {"model": "text-test", "usage": {"input_tokens": 17}})
    generator = Mock(return_value=result)

    assert TextValue(generator=generator).invoke(context) is result
    generator.assert_called_once_with(context)
    assert generator.call_args.args[0] is context


@pytest.mark.parametrize("adapter", [JevPolicy, TextValue])
def test_callbacks_and_config_reach_nested_runnables(adapter):
    events = Events()
    nested = RunnableLambda(lambda _input, config: config["configurable"]["trace_label"], name="nested")
    runnable = adapter(lambda *args: nested.invoke("provider input"))
    payload = {"page": {}, "goal": "Find a book", "history": []}

    output = runnable.invoke(payload, config={
        "callbacks": [events],
        "tags": ["offline"],
        "metadata": {"task": "book search"},
        "run_name": "configured adapter",
        "configurable": {"trace_label": "propagated"},
    })

    assert output == "propagated"
    assert [kwargs["name"] for _, kwargs in events.starts] == ["configured adapter", "nested"]
    for _, kwargs in events.starts:
        assert "offline" in kwargs["tags"]
        assert kwargs["metadata"]["task"] == "book search"
    assert events.starts[1][1]["parent_run_id"] == events.starts[0][1]["run_id"]
    assert events.ends == ["propagated", "propagated"]
    assert events.errors == []


@pytest.mark.parametrize("adapter", [JevPolicy, TextValue])
def test_provider_error_propagates_once_and_is_reported_to_callbacks(adapter):
    failure = RuntimeError("Provider unavailable")
    provider = Mock(side_effect=failure)
    events = Events()
    payload = {"page": {}, "goal": "Find a book", "history": []}

    with pytest.raises(RuntimeError) as caught:
        adapter(provider).invoke(payload, config={"callbacks": [events]})

    assert caught.value is failure
    assert provider.call_count == 1
    assert events.errors == [failure]
    assert events.ends == []


def test_default_policy_uses_jev_native_multihead_api(monkeypatch):
    calls = []

    def answer(ids, selected):
        return {"choice": selected, "confidence": 1.0, "probabilities": {key: float(key == selected) for key in ids}}

    def post(url, key, body):
        calls.append((url, key, body))
        return {"model": "jev-latest", "answers": {
            "operation": answer(body["questions"]["operation"]["criteria"], "CLICK"),
            "click_target": answer(["1"], "1"),
            "type_text_target": {"choice": "invalid unused target"},
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-placeholder")
    monkeypatch.delenv("TYPESAFE_MODEL", raising=False)
    monkeypatch.setattr(model, "post_json", post)
    page = {"url": "https://example.test", "title": "Search", "text": "Search", "actions": [
        {"id": "click-search", "kind": "click", "node": 10, "label": "Search", "role": "textbox"},
        {"id": "fill-search", "kind": "fill", "node": 10, "label": "Search", "role": "textbox"},
    ]}

    result = JevPolicy().invoke({"page": page, "goal": "Find a book", "history": []})

    assert len(calls) == 1
    url, key, body = calls[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert key == "offline-placeholder"
    assert body["model"] == "jev-latest"
    assert set(body["questions"]) == {"operation", "click_target", "type_text_target"}
    assert set(body) == {"model", "state", "questions"}
    assert result["choice"] == "click-search"
    assert result["operation"] == "CLICK"
    assert result["model"] == "jev-latest"


def test_default_text_adapter_retains_existing_validation(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "offline-placeholder")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text": "", "unexpected": true}'}}]})
    monkeypatch.setattr(model, "post_json", post)

    with pytest.raises(ValueError, match="no valid field value"):
        TextValue().invoke({"goal": "Find a book"})

    assert post.call_count == 1
