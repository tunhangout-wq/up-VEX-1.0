import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from bot.services.ai_provider import AIProvider, AIProviderError


class StreamingContent:
    def __init__(self, lines=None, body=b""):
        self.lines = [line if isinstance(line, bytes) else str(line).encode() for line in (lines or [])]
        self.body = body

    async def read(self, *_):
        return self.body

    def __aiter__(self):
        self._iter = iter(self.lines)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class StreamingResponse:
    def __init__(self, status=200, lines=None, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = StreamingContent(lines, body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class StreamingSession:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class AIStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_deltas_are_joined(self):
        response = StreamingResponse(lines=[
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
            b'\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
            b'\n',
            b'data: [DONE]\n',
            b'\n',
        ])
        provider = AIProvider()
        provider._session = StreamingSession(response)
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False):
            chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}])]
        self.assertEqual("".join(chunks), "Hello")
        self.assertTrue(provider.status()["latency_ms"] is not None)

    async def test_stream_uses_max_65536_without_fast_cap(self):
        response = StreamingResponse(lines=[
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n',
            b'\n',
            b'data: [DONE]\n',
            b'\n',
        ])
        provider = AIProvider()
        provider._session = StreamingSession(response)
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False):
            _ = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], max_tokens=65536)]
        payload = provider._session.calls[0][1]["json"]
        self.assertEqual(payload["model"], "Atria-Dawn-Preview")
        self.assertEqual(payload["max_tokens"], 65536)
        self.assertTrue(payload["stream"])


    async def test_fast_stream_caps_output_at_chat_size(self):
        response = StreamingResponse(lines=[
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n',
            b'\n',
            b'data: [DONE]\n',
            b'\n',
        ])
        provider = AIProvider()
        provider._session = StreamingSession(response)
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False):
            _ = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], max_tokens=65536, fast=True)]
        payload = provider._session.calls[0][1]["json"]
        self.assertEqual(payload["max_tokens"], 8192)

    async def test_stream_429_recovers_without_exposing_rate_limit(self):
        payload = json.dumps({"error": {"message": "rate limit"}}).encode()
        class SequenceSession:
            def __init__(self):
                self.responses = [
                    StreamingResponse(status=429, body=payload, headers={"Retry-After": "0.01"}),
                    StreamingResponse(lines=[
                        b'data: {"choices":[{"delta":{"content":"Recovered"}}]}\n',
                        b'data: [DONE]\n',
                    ]),
                ]
                self.calls = []
                self.closed = False
            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.responses.pop(0)
        provider = AIProvider()
        provider._session = SequenceSession()
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False), patch("bot.services.ai_provider.asyncio.sleep", new=AsyncMock()):
            chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}])]
        self.assertEqual("".join(chunks), "Recovered")
        self.assertGreaterEqual(provider.status()["retries"], 1)
        self.assertFalse(provider.status()["client_rate_limit"])


if __name__ == "__main__":
    unittest.main()



class TimeoutAfterTextContent(StreamingContent):
    def __aiter__(self):
        self._state = 0
        return self

    async def __anext__(self):
        if self._state == 0:
            self._state = 1
            return b'data: {"choices":[{"delta":{"content":"Partial reply"}}]}\n'
        raise asyncio.TimeoutError()


class TimeoutAfterTextResponse(StreamingResponse):
    def __init__(self):
        self.status = 200
        self.headers = {}
        self.content = TimeoutAfterTextContent()


class TimeoutAfterTextSession:
    def __init__(self):
        self.closed = False
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return TimeoutAfterTextResponse()


class AIStreamingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_timeout_is_preserved_instead_of_timeout_error(self):
        provider = AIProvider()
        provider._session = TimeoutAfterTextSession()
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False):
            chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], fast=True, auto_continue=False)]
        self.assertEqual("".join(chunks), "Partial reply")
        self.assertEqual(provider.status()["recovery"]["type"], "partial_preserved")

class AIStreamingTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_stream_timeout_budget_is_not_too_aggressive(self):
        response = StreamingResponse(lines=[
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n',
            b'data: [DONE]\n',
        ])
        provider = AIProvider()
        provider._session = StreamingSession(response)
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False):
            _ = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], fast=True)]
        timeout = provider._session.calls[0][1]["timeout"]
        self.assertEqual(timeout.total, 150)
        self.assertEqual(timeout.sock_read, 45)


class ContinuationResponse:
    def __init__(self, content=" continued"):
        self.status = 200
        self.headers = {}
        self.content_type = "application/json"
        self.content = StreamingContent(body=json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class LengthThenCompleteSession:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.stream_response = StreamingResponse(lines=[
            b'data: {"choices":[{"delta":{"content":"Partial"},"finish_reason":null}]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n',
            b'data: [DONE]\n',
        ])

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if kwargs["json"].get("stream"):
            return self.stream_response
        return ContinuationResponse(" continuation")


class AIStreamingContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_length_finish_reason_auto_continues(self):
        provider = AIProvider()
        provider._session = LengthThenCompleteSession()
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False):
            chunks = [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}], fast=True)]
        self.assertEqual("".join(chunks), "Partial continuation")
        self.assertEqual(provider.status()["metrics"]["continuations"], 1)
        self.assertEqual(len(provider._session.calls), 2)
        continuation_payload = provider._session.calls[1][1]["json"]
        self.assertLessEqual(len(continuation_payload["messages"]), 100)



class GateResponse:
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return False


class GateSession:
    def __init__(self):
        self.closed = False
        self.active = 0
        self.maximum = 0

    def post(self, url, **kwargs):
        session = self
        class _Ctx(GateResponse):
            async def __aenter__(self):
                session.active += 1
                session.maximum = max(session.maximum, session.active)
                return self
            async def __aexit__(self, exc_type, exc, tb):
                session.active -= 1
                return False
        return _Ctx()


class AIProviderConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_are_bounded(self):
        class ProbeProvider(AIProvider):
            async def open(self):
                self._session = GateSession()

        provider = ProbeProvider()
        provider._session = GateSession()
        async def one():
            async with provider._gated_post("https://example.invalid"):
                await asyncio.sleep(0)
        await asyncio.gather(*[one() for _ in range(14)])
        self.assertEqual(provider._request_gate._value, 6)
        self.assertLessEqual(provider._session.maximum, 6)


class CompleteLengthResponse(StreamingResponse):
    def __init__(self, payload):
        super().__init__(body=json.dumps(payload).encode())
        self.content_type = "application/json"


class CompleteLengthSession:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.responses = [
            {"choices": [{"message": {"content": "First half"}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": "second half"}, "finish_reason": "stop"}]},
        ]

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = self.responses.pop(0)
        return CompleteLengthResponse(payload)


class AICompleteContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_length_finish_reason_auto_continues(self):
        provider = AIProvider()
        provider._session = CompleteLengthSession()
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False):
            answer = await provider.complete([{"role": "user", "content": "hi"}], max_tokens=1200, fast=True)
        self.assertEqual(answer, "First half second half")
        self.assertEqual(provider.status()["metrics"]["continuations"], 1)
        self.assertEqual(len(provider._session.calls), 2)
        self.assertFalse(provider._session.calls[1][1]["json"].get("stream", False))
