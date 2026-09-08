from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class GenerateResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    meta: dict = field(default_factory=dict)


class LLMProvider(ABC):
    """`system` is a list of system blocks; providers join them with blank lines."""

    name: str = "base"

    @abstractmethod
    async def generate(self, system: list[str], messages: list[Message], *, model: str | None = None,
                       max_tokens: int = 600, temperature: float = 0.7) -> GenerateResult: ...

    async def stream(self, system: list[str], messages: list[Message], *, model: str | None = None,
                     max_tokens: int = 600, temperature: float = 0.7) -> AsyncIterator[str]:
        """Default streaming: yield the whole completion as one chunk. Real providers override."""
        result = await self.generate(system, messages, model=model, max_tokens=max_tokens, temperature=temperature)
        yield result.text

    @staticmethod
    def _join_system(system: list[str]) -> str:
        return "\n\n".join(s.strip() for s in system if s and s.strip())

    @staticmethod
    def _timer():
        start = time.perf_counter()
        return lambda: int((time.perf_counter() - start) * 1000)
