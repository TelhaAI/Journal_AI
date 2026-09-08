"""Provider-agnostic LLM adapter (plan §2.1). Model choice is config, not code."""
from __future__ import annotations

from .base import GenerateResult, LLMProvider, Message
from .providers import ScriptedProvider, SilentProvider, get_provider, set_provider

__all__ = [
    "GenerateResult",
    "LLMProvider",
    "Message",
    "ScriptedProvider",
    "SilentProvider",
    "get_provider",
    "set_provider",
]
