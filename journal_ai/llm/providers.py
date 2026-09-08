from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator, Callable

from ..config import get_settings
from .base import GenerateResult, LLMProvider, Message

ScriptFn = Callable[[list[str], list[Message]], str]


class ScriptedProvider(LLMProvider):
    """Deterministic provider for tests and offline development.

    Responses come from, in order of precedence: a `script` callable, a FIFO queue
    (`push`), or a default echo. Every call is recorded in `calls` so tests can
    assert on prompt assembly (which addenda were present, which model was named).
    """

    name = "scripted"

    def __init__(self, script: ScriptFn | None = None, default: str = "I read that.", model: str = "scripted-1"):
        self.script = script
        self.default = default
        self.model = model
        self.queue: deque[str] = deque()
        self.calls: list[dict] = []

    def push(self, *responses: str) -> "ScriptedProvider":
        self.queue.extend(responses)
        return self

    def reset(self) -> None:
        self.queue.clear()
        self.calls.clear()

    async def generate(self, system, messages, *, model=None, max_tokens=600, temperature=0.7) -> GenerateResult:
        done = self._timer()
        if self.script is not None:
            text = self.script(system, messages)
        elif self.queue:
            text = self.queue.popleft()
        else:
            text = self.default
        self.calls.append({"system": list(system), "messages": [(m.role, m.content) for m in messages],
                           "model": model or self.model, "response": text})
        joined = self._join_system(system) + "".join(m.content for m in messages)
        return GenerateResult(text=text, model=model or self.model, input_tokens=len(joined) // 4,
                              output_tokens=len(text) // 4, latency_ms=done())

    async def stream(self, system, messages, *, model=None, max_tokens=600, temperature=0.7) -> AsyncIterator[str]:
        result = await self.generate(system, messages, model=model, max_tokens=max_tokens, temperature=temperature)
        for word in result.text.split(" "):
            yield word + " "
            await asyncio.sleep(0)


class SilentProvider(LLMProvider):
    name = "silent"

    async def generate(self, system, messages, *, model=None, max_tokens=600, temperature=0.7) -> GenerateResult:
        return GenerateResult(text="", model=model or "silent")


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.AsyncAnthropic()
        self.model = model

    async def generate(self, system, messages, *, model=None, max_tokens=600, temperature=0.7) -> GenerateResult:
        done = self._timer()
        resp = await self.client.messages.create(
            model=model or self.model, max_tokens=max_tokens, system=self._join_system(system),
            messages=[{"role": m.role, "content": m.content} for m in messages],
            extra_body={"temperature": temperature},  # SDK ≥1.x moved sampling params; the API still accepts it
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return GenerateResult(text=text, model=resp.model, input_tokens=resp.usage.input_tokens,
                              output_tokens=resp.usage.output_tokens, latency_ms=done())

    async def stream(self, system, messages, *, model=None, max_tokens=600, temperature=0.7) -> AsyncIterator[str]:
        async with self.client.messages.stream(
            model=model or self.model, max_tokens=max_tokens, system=self._join_system(system),
            messages=[{"role": m.role, "content": m.content} for m in messages],
            extra_body={"temperature": temperature},
        ) as s:
            async for chunk in s.text_stream:
                yield chunk


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str):
        import openai

        self.client = openai.AsyncOpenAI()
        self.model = model

    def _msgs(self, system, messages):
        return [{"role": "system", "content": self._join_system(system)}] + [
            {"role": m.role, "content": m.content} for m in messages
        ]

    async def generate(self, system, messages, *, model=None, max_tokens=600, temperature=0.7) -> GenerateResult:
        done = self._timer()
        resp = await self.client.chat.completions.create(
            model=model or self.model, max_tokens=max_tokens, temperature=temperature,
            messages=self._msgs(system, messages),
        )
        usage = resp.usage
        return GenerateResult(text=resp.choices[0].message.content or "", model=resp.model,
                              input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                              output_tokens=getattr(usage, "completion_tokens", 0) or 0, latency_ms=done())

    async def stream(self, system, messages, *, model=None, max_tokens=600, temperature=0.7) -> AsyncIterator[str]:
        resp = await self.client.chat.completions.create(
            model=model or self.model, max_tokens=max_tokens, temperature=temperature,
            messages=self._msgs(system, messages), stream=True,
        )
        async for chunk in resp:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta


_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        if s.llm_provider == "anthropic":
            _provider = AnthropicProvider(s.llm_model)
        elif s.llm_provider == "openai":
            _provider = OpenAIProvider(s.llm_model)
        elif s.llm_provider == "silent":
            _provider = SilentProvider()
        else:
            _provider = ScriptedProvider()
    return _provider


def set_provider(p: LLMProvider | None) -> None:
    global _provider
    _provider = p
