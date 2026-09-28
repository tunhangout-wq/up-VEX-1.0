import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.cogs.atria import Atria


class FastDiscordAITests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_and_moderation_run_concurrently(self):
        started = []
        gate = asyncio.Event()

        async def chat(*args, **kwargs):
            started.append("chat")
            await gate.wait()
            return "tawa labes"

        async def moderation(*args, **kwargs):
            started.append("moderation")
            await gate.wait()
            return json.dumps({"action": "allow", "classification": "toxicity", "confidence": 0.99, "reason": "safe"})

        bot = SimpleNamespace(user=SimpleNamespace(id=900))
        cog = Atria(bot)
        cog._ai_runtime = {
            "chat": {"enabled": True, "allowed_channels": ["123"], "response_mode": "every_message",
                     "user_cooldown_seconds": 0, "channel_cooldown_seconds": 0, "global_cooldown_seconds": 0,
                     "streaming": False},
            "moderation": {"enabled": True, "channels": ["123"], "detection_types": ["toxicity"],
                           "automatic_actions": ["timeout"], "sensitivity": "high", "allow_ai_ban": False}
        }
        channel = SimpleNamespace(id=123, send=AsyncMock(), permissions_for=lambda member: SimpleNamespace(send_messages=True))
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False, id=55, display_name="twix", mention="@twix", top_role=1),
            guild=SimpleNamespace(id=1, owner_id=2, me=SimpleNamespace(top_role=10, guild_permissions=SimpleNamespace(moderate_members=True, ban_members=True))),
            channel=channel,
            content="slim normal text",
            mentions=[],
            reply=AsyncMock(),
        )
        with patch("bot.cogs.atria.load_settings", return_value={"atria": {}}), \
             patch("bot.cogs.atria.ai_chat_service.handle_message", side_effect=chat), \
             patch("bot.cogs.atria.atria_chat", side_effect=moderation):
            task = asyncio.create_task(cog.on_message(message))
            await asyncio.sleep(0.05)
            self.assertCountEqual(started, ["chat", "moderation"])
            gate.set()
            await task


if __name__ == "__main__":
    unittest.main()
