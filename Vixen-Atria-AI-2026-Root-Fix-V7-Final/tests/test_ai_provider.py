import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from bot.services.ai_provider import AIProvider


class FakeContent:
    def __init__(self, body):
        self._body = body

    async def read(self, *_):
        return self._body


class FakeResponse:
    def __init__(self, status=200, payload=None, content_type="application/json"):
        self.status = status
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
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class AIProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_is_not_configured(self):
        provider = AIProvider()
        with patch.dict(os.environ, {"AI_API_KEY": "", "ATRIA_API_KEY": ""}, clear=False):
            result = await provider.test_connection()

        self.assertFalse(result["configured"])
        self.assertEqual(result["status"], "Not Configured")
        self.assertIsNone(result["latency_ms"])
        self.assertIsNotNone(result["last_request"])
        self.assertEqual(result["provider"], "Atria")

    async def test_uses_direct_atria_endpoint_and_model(self):
        provider = AIProvider()
        fake_session = FakeSession(
            FakeResponse(
                payload={
                    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                }
            )
        )
        provider._session = fake_session
        provider._last_call = 0.0

        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test", "AI_API_KEY": "legacy-test"}, clear=False):
            result = await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8)

        self.assertEqual(result, "OK")
        url, kwargs = fake_session.calls[0]
        self.assertEqual(url, "https://api.atria-asi.ai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer atria-test")
        self.assertEqual(kwargs["json"]["model"], "Atria-Dawn-Preview")

    async def test_api_key_whitespace_is_normalized(self):
        provider = AIProvider()
        fake_session = FakeSession(
            FakeResponse(payload={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
        )
        provider._session = fake_session

        with patch.dict(
            os.environ,
            {"ATRIA_API_KEY": "  atria- test\nkey  ", "AI_API_KEY": "legacy-test"},
            clear=False,
        ):
            result = await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8)

        self.assertEqual(result, "OK")
        self.assertEqual(
            fake_session.calls[0][1]["headers"]["Authorization"],
            "Bearer atria-testkey",
        )

    async def test_runtime_base_url_and_model_are_trimmed(self):
        provider = AIProvider()
        fake_session = FakeSession(
            FakeResponse(payload={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
        )
        provider._session = fake_session

        with patch.dict(
            os.environ,
            {
                "ATRIA_API_KEY": "atria-test",
                "ATRIA_API_BASE_URL": " https://example.test/v1/ ",
                "ATRIA_MODEL": " Atria-Dawn-Preview ",
            },
            clear=False,
        ):
            await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8)

        url, kwargs = fake_session.calls[0]
        self.assertEqual(url, "https://example.test/v1/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "Atria-Dawn-Preview")

    async def test_legacy_ai_key_still_works(self):
        provider = AIProvider()
        fake_session = FakeSession(
            FakeResponse(payload={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
        )
        provider._session = fake_session

        with patch.dict(os.environ, {"ATRIA_API_KEY": "", "AI_API_KEY": "legacy-test"}, clear=False):
            result = await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8)

        self.assertEqual(result, "OK")
        self.assertEqual(fake_session.calls[0][1]["headers"]["Authorization"], "Bearer legacy-test")

    async def test_403_exposes_safe_provider_diagnostic(self):
        provider = AIProvider()
        fake_session = FakeSession(
            FakeResponse(status=403, payload={"error": {"message": "Access denied"}})
        )
        provider._session = fake_session

        with patch.dict(os.environ, {"ATRIA_API_KEY": "atria-test", "AI_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8)
            status = provider.status()
        self.assertEqual(status["status"], "Error")
        self.assertIn("HTTP 403", status["error"])
        self.assertIn("Access denied", status["error"])
        self.assertNotIn("atria-test", status["error"])


if __name__ == "__main__":
    unittest.main()
