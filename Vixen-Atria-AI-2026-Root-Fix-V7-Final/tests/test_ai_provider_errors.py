import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from bot.services.ai_provider import AIProvider, AIProviderError


class FakeContent:
    def __init__(self, body):
        self._body = body

    async def read(self, *_):
        return self._body


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, content_type="application/json"):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self.content = FakeContent(json.dumps(payload or {}).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def post(self, url, **kwargs):
        return self.response


class AIProviderErrorTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, status, payload, headers=None):
        provider = AIProvider()
        provider._session = FakeSession(FakeResponse(status=status, payload=payload, headers=headers))
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test", "AI_API_KEY": ""}, clear=False):
            with patch.object(provider, "_wait_for_provider_backoff", new=AsyncMock()):
                with self.assertRaises(AIProviderError) as ctx:
                    await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8, fast=True)
            status = provider.status()
        return ctx.exception, status

    async def test_401_has_clear_safe_message(self):
        exc, status = await self._run(401, {"error": {"message": "Invalid API key"}})
        self.assertEqual(exc.status_code, 401)
        self.assertIn("مفتاح Atria", exc.safe_message)
        self.assertIn("HTTP 401", status["error"])
        self.assertNotIn("atria-test", status["error"])

    async def test_429_preserves_retry_after(self):
        exc, status = await self._run(429, {"error": {"message": "rate limit"}}, {"Retry-After": "7"})
        self.assertEqual(exc.status_code, 429)
        self.assertEqual(exc.retry_after, 7.0)
        self.assertNotIn("rate limit", status["error"].lower())
        self.assertNotIn("HTTP 429", status["error"])

    async def test_provider_does_not_add_client_side_rate_limit(self):
        provider = AIProvider()
        response = FakeResponse(
            payload={"choices": [{"message": {"content": "OK"}}]},
            headers={"x-rpm-limit": "60", "x-rpm-remaining": "59"},
        )
        provider._session = FakeSession(response)
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test", "AI_API_KEY": ""}, clear=False):
            result = await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8, fast=True)
        self.assertEqual(result, "OK")
        self.assertFalse(provider.status()["client_rate_limit"])

    async def test_complete_429_recovers_transparently(self):
        class SequenceSession:
            def __init__(self):
                self.responses = [
                    FakeResponse(status=429, payload={"error": {"message": "rate limit"}}, headers={"Retry-After": "0.01"}),
                    FakeResponse(payload={"choices": [{"message": {"content": "Recovered"}}]}),
                ]
                self.calls = []
                self.closed = False
            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.responses.pop(0)
        provider = AIProvider()
        provider._session = SequenceSession()
        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test"}, clear=False), patch("bot.services.ai_provider.asyncio.sleep", new=AsyncMock()):
            result = await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8, fast=True)
        self.assertEqual(result, "Recovered")
        self.assertEqual(len(provider._session.calls), 2)
        self.assertFalse(provider.status()["client_rate_limit"])

if __name__ == "__main__":
    unittest.main()
