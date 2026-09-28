import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services.ai_chat import AIChatService
from bot.utils.ai_store import AIStore
from bot.cogs.atria import Atria


class AIUltimateRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_context_cache_does_not_hide_durable_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AIStore(Path(directory) / "ai.sqlite3")
            store.add_chat_message(1, 10, 22, "user", "Mariam: شنوة اسمك؟")
            store.add_chat_message(1, 10, 900, "assistant", "Vixen.")
            provider = SimpleNamespace(complete=AsyncMock(return_value="لاباس"))
            service = AIChatService(provider=provider, store=store)
            bot = SimpleNamespace(user=SimpleNamespace(id=900))
            message = SimpleNamespace(
                id=100, guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=10, permissions_for=lambda _: SimpleNamespace(send_messages=True)),
                author=SimpleNamespace(id=22, bot=False, display_name="Mariam"), content="شنوة أخبارك؟", mentions=[],
                reply=AsyncMock(),
            )
            config = {"enabled": True, "allowed_channels": ["10"], "response_mode": "every_message", "streaming": False,
                      "tunisian_mode": True, "context_messages": 20, "user_cooldown_seconds": 0,
                      "channel_cooldown_seconds": 0, "global_cooldown_seconds": 0}
            self.assertTrue(await service.handle_message(bot, message, config))
            request_messages = provider.complete.await_args.args[0]
            contents = [entry["content"] for entry in request_messages]
            self.assertTrue(any("Vixen." in content for content in contents))
            self.assertTrue(contents[-1].endswith("شنوة أخبارك؟"))

    async def test_legacy_ai_ban_opt_in_is_preserved(self):
        bot = SimpleNamespace(user=SimpleNamespace(id=900))
        cog = Atria(bot)
        from unittest.mock import patch
        with patch("bot.cogs.atria.load_settings", return_value={"ai": {}, "atria": {"moderation_enabled": True, "allow_ai_ban": True}}):
            runtime = cog.refresh_ai_config()
        self.assertTrue(runtime["moderation"]["enabled"])
        self.assertTrue(runtime["moderation"]["allow_ai_ban"])
        self.assertIn("ban", runtime["moderation"]["automatic_actions"])

    def test_stream_path_uses_single_orchestrated_plan(self):
        source = Path("bot/services/ai_chat.py").read_text()
        self.assertGreaterEqual(source.count("ai_orchestrator.plan("), 2)
        self.assertIn("provider.stream(messages, max_tokens=max_tokens, fast=fast, auto_continue=plan.continuation)", source)
        self.assertNotIn("max_tokens = min(configured_tokens, route.max_output_tokens)", source)


if __name__ == "__main__":
    unittest.main()
