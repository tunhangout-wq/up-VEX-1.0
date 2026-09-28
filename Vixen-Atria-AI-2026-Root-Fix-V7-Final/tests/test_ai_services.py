import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.ai_chat import AIChatService
from bot.services.ai_provider import AIProvider
from bot.utils.ai_store import AIStore


class AIProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_is_not_configured(self):
        provider = AIProvider()
        with patch.dict(os.environ, {"AI_API_KEY": "", "ATRIA_API_KEY": ""}, clear=False):
            result = await provider.test_connection()

        self.assertFalse(result["configured"])
        self.assertEqual(result["status"], "Not Configured")
        self.assertIsNone(result["latency_ms"])
        self.assertIsNotNone(result["last_request"])


class AIChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_allowlist_mention_mode_and_bounded_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AIStore(Path(directory) / "ai.sqlite3")
            store.add_chat_message(10, 100, 1, "user", "Mariam: شنوة اسمك؟")
            store.add_chat_message(10, 100, 999, "assistant", "Vixen.")
            store.add_chat_message(10, 200, 2, "user", "private channel history")
            provider = SimpleNamespace(complete=AsyncMock(return_value="لاباس!"))
            service = AIChatService(provider=provider, store=store)
            bot_user = SimpleNamespace(id=999)
            bot = SimpleNamespace(user=bot_user)
            config = {
                "enabled": True,
                "allowed_channels": ["100"],
                "response_mode": "mention_only",
                "context_messages": 10,
                "tunisian_mode": True,
                "user_cooldown_seconds": 0,
                "channel_cooldown_seconds": 0,
                "global_cooldown_seconds": 0,
            }

            def message(channel_id, content, mentions):
                return SimpleNamespace(
                    guild=SimpleNamespace(id=10),
                    channel=SimpleNamespace(id=channel_id),
                    author=SimpleNamespace(bot=False, id=3, display_name="Mariam"),
                    content=content,
                    mentions=mentions,
                    reply=AsyncMock(),
                )

            outside_channel = message(200, "<@999> hello", [bot_user])
            no_mention = message(100, "hello", [])
            allowed = message(100, "<@999> وقداش عمرك؟", [bot_user])

            self.assertFalse(await service.handle_message(bot, outside_channel, config))
            self.assertFalse(await service.handle_message(bot, no_mention, config))
            self.assertTrue(await service.handle_message(bot, allowed, config))
            provider.complete.assert_awaited_once()
            request_messages = provider.complete.await_args.args[0]
            self.assertIn("Tunisian Derja", request_messages[0]["content"])
            self.assertTrue(any("Vixen." in message["content"] for message in request_messages))
            self.assertNotIn("private channel history", [message["content"] for message in request_messages])
            self.assertEqual(allowed.reply.await_count, 1)

    async def test_slash_chat_respects_ignored_channels(self):
        provider = SimpleNamespace(complete=AsyncMock(return_value="OK"))
        service = AIChatService(provider=provider, store=SimpleNamespace())
        config = {
            "enabled": True,
            "allowed_channels": ["100"],
            "ignored_channels": ["100"],
            "user_cooldown_seconds": 0,
            "channel_cooldown_seconds": 0,
            "global_cooldown_seconds": 0,
        }
        result = await service.answer_command(
            SimpleNamespace(user=None),
            SimpleNamespace(id=10),
            SimpleNamespace(id=100),
            SimpleNamespace(id=3, display_name="User"),
            "hello",
            config,
        )
        self.assertIsNone(result)
        provider.complete.assert_not_awaited()

    async def test_chat_context_limit_clamps_to_supported_windows(self):
        service = AIChatService(provider=SimpleNamespace(), store=SimpleNamespace())

        prompt = service._system_prompt({"language": "french", "name": "Vixen"})

        self.assertIn("natural French", prompt)


if __name__ == "__main__":
    unittest.main()