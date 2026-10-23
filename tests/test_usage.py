from types import SimpleNamespace

from agentrun.supervised import SupervisedAgent
from AgentBrowser.usage import summarize_model_usage


def test_model_usage_summary_counts_calls_tokens_and_reported_costs():
    summary = summarize_model_usage({
        "model_calls": 2,
        "decisions": [
            {"model": "vendor/choice", "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                                    "total_tokens": 12, "cost": 0.001}},
            {"model": "vendor/choice", "usage": {"prompt_tokens": 8, "completion_tokens": 3,
                                                    "total_tokens": 11, "cost": 0.002}},
        ],
        "text_model_calls": 1,
        "text_calls": [{"model": "vendor/text", "usage": {"input_tokens": 5, "output_tokens": 1}}],
    })

    assert summary["calls"] == 3
    assert summary["known_cost_usd"] == 0.003
    assert summary["cost_usd"] is None
    assert summary["missing_cost_calls"] == 1
    choice = next(model for model in summary["models"] if model["component"] == "Jev")
    assert (choice["calls"], choice["input_tokens"], choice["output_tokens"], choice["cost_usd"]) == (
        2, 18, 5, 0.003,
    )


def test_model_usage_summary_counts_failed_calls_without_inventing_model_or_price():
    summary = summarize_model_usage({"model_calls": 2, "decisions": []})
    assert summary["calls"] == 2
    assert summary["cost_usd"] is None
    assert summary["missing_cost_calls"] == 2
    assert summary["models"][0]["model"] == "sin uso devuelto"


def test_supervisor_usage_is_split_by_phase():
    summary = summarize_model_usage({
        "supervisor_calls": 2,
        "supervisor_usage": [
            {"model": "vendor/supervisor", "phase": "initialize", "usage": {
                "prompt_tokens": 100, "completion_tokens": 30, "cost": 0.001,
            }},
            {"model": "vendor/supervisor", "phase": "review", "usage": {
                "prompt_tokens": 400, "completion_tokens": 20, "cost": 0.002,
            }},
        ],
    })

    phases = {model["phase"]: model for model in summary["models"]}
    assert phases["initialize"]["input_tokens"] == 100
    assert phases["review"]["input_tokens"] == 400
    assert summary["calls"] == 2
    assert summary["cost_usd"] == 0.003


def test_jev_cost_is_calculated_from_published_input_token_rate():
    inputs = [16_396] * 26 + [16_395] * 57
    outputs = [283] * 77 + [282] * 6
    summary = summarize_model_usage({
        "model_calls": 83,
        "decisions": [{
            "model": "jev-1.13.0",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        } for input_tokens, output_tokens in zip(inputs, outputs)],
    })

    jev = summary["models"][0]
    assert jev["model"] == "jev-1.13.0"
    assert jev["calls"] == 83
    assert jev["input_tokens"] == 1_360_811
    assert jev["output_tokens"] == 23_483
    assert summary["cost_usd"] == round(1_360_811 * 0.042 / 1_000_000, 8)
    assert summary["cost_basis"] == "published_rate"
    assert summary["missing_cost_calls"] == 0


def test_supervised_worker_compaction_preserves_jev_model_name():
    owner = SimpleNamespace(
        worker=SimpleNamespace(
            state={"decisions": [{"model": "jev-1.13.0", "usage": {"input_tokens": 10},
                                  "request": {"large": "omitted"}}]},
            _semantic_visits=[],
        ),
        state={},
    )

    SupervisedAgent._capture_worker(owner)

    decision = owner.state["worker"]["decisions"][0]
    assert decision["model"] == "jev-1.13.0"
    assert "request" not in decision
