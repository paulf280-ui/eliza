"""Minimal OpenAI model provider plugin for the Python AgentRuntime."""

from __future__ import annotations

import os
from typing import Any

from elizaos.plugin import Plugin
from elizaos.types.model import ModelType


async def _openai_text_handler(runtime: Any, params: dict[str, Any]) -> str:
    """Call the OpenAI chat completions API."""
    import openai

    api_key = os.environ.get("OPENAI_API_KEY") or (
        runtime.get_setting("OPENAI_API_KEY") if hasattr(runtime, "get_setting") else None
    )
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    model_name = params.get("model") or os.environ.get("OPENAI_LARGE_MODEL", "gpt-4o")
    temperature = params.get("temperature", 0.7)
    max_tokens = params.get("maxTokens", params.get("max_tokens", 2000))

    messages: list[dict[str, str]] = []
    if params.get("system"):
        messages.append({"role": "system", "content": params["system"]})
    else:
        # Default system prompt to encourage structured output compliance
        messages.append({
            "role": "system",
            "content": (
                "You are a helpful AI assistant. When the prompt asks you to respond "
                "in a specific format (XML, JSON), you MUST respond ONLY with that "
                "format. Do not include any preamble, explanation, or text outside "
                "the requested format block. Start your response immediately with "
                "the opening tag or brace."
            ),
        })
    prompt = params.get("prompt", "")
    if prompt:
        messages.append({"role": "user", "content": prompt})

    client = openai.AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


async def _openai_small_handler(runtime: Any, params: dict[str, Any]) -> str:
    """TEXT_SMALL handler uses the small model."""
    params = dict(params)
    params.setdefault("model", os.environ.get("OPENAI_SMALL_MODEL", "gpt-4o-mini"))
    return await _openai_text_handler(runtime, params)


async def _openai_embedding_handler(runtime: Any, params: dict[str, Any]) -> list[float]:
    """Generate text embeddings via OpenAI."""
    import openai

    api_key = os.environ.get("OPENAI_API_KEY") or (
        runtime.get_setting("OPENAI_API_KEY") if hasattr(runtime, "get_setting") else None
    )
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    model_name = params.get("model") or os.environ.get(
        "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
    )
    text = params.get("text", params.get("prompt", ""))

    client = openai.AsyncOpenAI(api_key=api_key)
    response = await client.embeddings.create(model=model_name, input=text)
    return response.data[0].embedding


def create_openai_plugin() -> Plugin:
    """Create an OpenAI plugin that registers LLM model handlers."""
    return Plugin(
        name="openai",
        description="OpenAI model provider for text generation and embeddings",
        models={
            ModelType.TEXT_LARGE: _openai_text_handler,
            ModelType.TEXT_SMALL: _openai_small_handler,
            ModelType.TEXT_EMBEDDING: _openai_embedding_handler,
        },
    )
