import asyncio
import json
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.services.ai_provider import AIProvider


class StreamContent:
    def __init__(self, events):
        self.events = list(events)
        self._iter = iter(self.events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = next(self._iter, None)
        if event is None:
            raise StopAsyncIteration
        if isinstance(event, BaseException):
            raise event
        return event

    async def read(self, *_):
        return b""


class StreamResponse:
    status = 200
    content_type = "text/event-stream"
    headers = {}

    def __init__(self, events):
        self.content = StreamContent(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class CompleteResponse:
    status = 200
    content_type = "application/json"
    headers = {}

    def __init__(self, content, finish_reason="stop"):
        payload = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
        self.content = types.SimpleNamespace(read=AsyncMock(return_value=json.dumps(payload).encode()))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_sse_transport_cut_after_partial_uses_continuation_without_replay(monkeypatch):
    initial = StreamResponse([
        b'data: {"choices":[{"delta":{"content":"Hello from the "}}]}\n',
        b'data: {"choices":[{"delta":{"content":"partial stream"}}]}\n',
        __import__("aiohttp").ClientPayloadError("cut"),
    ])
    continuation = CompleteResponse("continuation only", "stop")
    provider = AIProvider()
    provider._session = SequenceSession([initial, continuation])
    monkeypatch.setenv("ATRIA_API_KEY", "test")

    chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], fast=True)]
    result = "".join(chunks)
    assert result == "Hello from the partial stream continuation only"
    assert len(provider._session.calls) == 2
    assert provider._session.calls[1][1]["json"]["messages"][-2]["role"] == "assistant"


@pytest.mark.asyncio
async def test_stream_continues_through_two_length_stops(monkeypatch):
    initial = StreamResponse([
        b'data: {"choices":[{"delta":{"content":"First section. "}}]}\n',
        b'data: {"choices":[{"delta":{"content":"Second section."},"finish_reason":"length"}]}\n',
    ])
    c1 = CompleteResponse("Second section. Third section.", "length")
    c2 = CompleteResponse("Third section. Final line.", "stop")
    provider = AIProvider()
    provider._session = SequenceSession([initial, c1, c2])
    monkeypatch.setenv("ATRIA_API_KEY", "test")

    chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], fast=True)]
    assert "".join(chunks) == "First section. Second section. Third section. Final line."
    assert len(provider._session.calls) == 3


@pytest.mark.asyncio
async def test_stream_without_done_or_finish_reason_is_recovered(monkeypatch):
    initial = StreamResponse([
        b'data: {"choices":[{"delta":{"content":"An answer without a terminal marker"}}]}\n',
    ])
    continuation = CompleteResponse(" finishes cleanly.", "stop")
    provider = AIProvider()
    provider._session = SequenceSession([initial, continuation])
    monkeypatch.setenv("ATRIA_API_KEY", "test")

    chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], fast=True)]
    assert "".join(chunks) == "An answer without a terminal marker finishes cleanly."


def test_clean_answer_preserves_python_code_fence():
    discord = types.ModuleType("discord")
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.Forbidden = type("Forbidden", (Exception,), {})
    discord.NotFound = type("NotFound", (Exception,), {})
    discord.utils = types.SimpleNamespace(utcnow=lambda: None)
    previous = sys.modules.get("discord")
    sys.modules["discord"] = discord
    try:
        from bot.services.ai_chat import AIChatService
        answer = "```python\nprint('ok')\n```"
        assert AIChatService._clean_answer(answer) == answer
    finally:
        if previous is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = previous


def test_split_discord_preserves_all_plain_text_without_loss():
    from bot.services.ai_chat import AIChatService

    source = "A" * 6000
    chunks = AIChatService._split_discord(source, limit=1900)
    assert chunks
    assert all(len(chunk) <= 1900 for chunk in chunks)
    assert "".join(chunks) == source


def test_technical_short_question_is_not_marked_simple_overlong():
    from bot.services.ai_chat import AIChatService
    from bot.services.ai_intent import ai_intent_router
    route = ai_intent_router.route("كيفاش نعمل timeout في aiohttp؟", {})
    assert route.intent in {"question", "code_or_complex"}
    issues = AIChatService._quality_issues("X" * 800, "كيفاش نعمل timeout في aiohttp؟", "User", [])
    assert "simple_prompt_overlong" not in issues


def test_quality_repair_rejects_shortened_rewrite():
    discord = types.ModuleType("discord")
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.Forbidden = type("Forbidden", (Exception,), {})
    discord.NotFound = type("NotFound", (Exception,), {})
    discord.utils = types.SimpleNamespace(utcnow=lambda: None)
    previous = sys.modules.get("discord")
    sys.modules["discord"] = discord
    try:
        from bot.services.ai_chat import AIChatService
        from bot.services.ai_quality import QualityReport

        service = AIChatService(provider=SimpleNamespace(complete=AsyncMock(return_value="short")))
        long_answer = "word " * 300
        route = SimpleNamespace(intent="question")
        with patch("bot.services.ai_chat.ai_quality_evaluator.evaluate", return_value=QualityReport(0.5, ("repeated_block",))):
            result = asyncio.run(
                service._quality_repair(
                    long_answer,
                    bot=SimpleNamespace(),
                    guild=SimpleNamespace(id=1, name="g"),
                    channel=SimpleNamespace(name="c"),
                    author=SimpleNamespace(id=2, display_name="u"),
                    content="explain this",
                    config={},
                    context_entries=[],
                    route=route,
                )
            )
        assert result == long_answer.strip()
    finally:
        if previous is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = previous
