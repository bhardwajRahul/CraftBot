# -*- coding: utf-8 -*-
"""Model registry mapping providers to default models."""

from agent_core.core.models.types import InterfaceType

MODEL_REGISTRY = {
    "openai": {
        InterfaceType.LLM: "gpt-5.2-2025-12-11",
        InterfaceType.VLM: "gpt-5.2-2025-12-11",
        InterfaceType.EMBEDDING: "text-embedding-3-small",
    },
    "gemini": {
        InterfaceType.LLM: "gemini-2.5-pro",
        InterfaceType.VLM: "gemini-2.5-pro",
        InterfaceType.EMBEDDING: "text-embedding-004",
    },
    "anthropic": {
        InterfaceType.LLM: "claude-sonnet-4-5-20250929",
        InterfaceType.VLM: "claude-sonnet-4-5-20250929",
        InterfaceType.EMBEDDING: None,  # Anthropic does not provide native embedding models
    },
    "byteplus": {
        InterfaceType.LLM: "seed-2-0-pro-260328",
        InterfaceType.VLM: "seed-2-0-pro-260328",
        InterfaceType.EMBEDDING: "skylark-embedding-vision-250615",
    },
    "remote": {
        InterfaceType.LLM: "llama3.2:3b",
        InterfaceType.VLM: "llava:7b",
        InterfaceType.EMBEDDING: "nomic-embed-text",
    },
    "minimax": {
        InterfaceType.LLM: "MiniMax-Text-01",
        InterfaceType.VLM: "MiniMax-VL-01",
        InterfaceType.EMBEDDING: None,
    },
    "deepseek": {
        InterfaceType.LLM: "deepseek-chat",
        InterfaceType.VLM: None,
        InterfaceType.EMBEDDING: None,
    },
    "moonshot": {
        InterfaceType.LLM: "kimi-k2.5",
        InterfaceType.VLM: "moonshot-v1-8k-vision-preview",
        InterfaceType.EMBEDDING: None,
    },
    "grok": {
        InterfaceType.LLM: "grok-3",
        InterfaceType.VLM: "grok-4-0709",
        InterfaceType.EMBEDDING: None,
    },
    "openrouter": {
        # OpenRouter slugs follow `<provider>/<model>` format. Default to a Claude
        # model so KV caching exercises the cache_control path on first use.
        InterfaceType.LLM: "anthropic/claude-sonnet-4.5",
        InterfaceType.VLM: "anthropic/claude-sonnet-4.5",
        InterfaceType.EMBEDDING: None,
    },
}
