"""LangChain adapters for Jev's native decision API and the optional text helper."""

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableLambda

from . import model


class JevPolicy(RunnableLambda[dict[str, Any], dict[str, Any]]):
    """Choose an operation and its observed target in one native TypeSafe request.

    The input contains ``page``, ``goal`` and ``history``. LangChain's inherited
    invoke/ainvoke methods handle RunnableConfig, callbacks and composition;
    the provider continues to receive its original structured inputs.
    """

    def __init__(self, chooser: Callable[..., dict[str, Any]] | None = None):
        provider = model.choose if chooser is None else chooser

        def choose_action(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            return provider(payload["page"], payload["goal"], payload["history"], **kwargs)

        super().__init__(choose_action, name="JevPolicy")


class TextValue(RunnableLambda[dict[str, Any], tuple[str, dict[str, Any]]]):
    """Generate a field value from the complete field_context payload.

    The helper preserves the existing validated value and usage metadata. It
    does not choose browser actions or retry their execution.
    """

    def __init__(self, generator: Callable[..., tuple[str, dict[str, Any]]] | None = None):
        provider = model.field_text if generator is None else generator

        def generate_value(context: dict[str, Any], **kwargs: Any) -> tuple[str, dict[str, Any]]:
            return provider(context, **kwargs)

        super().__init__(generate_value, name="TextValue")
