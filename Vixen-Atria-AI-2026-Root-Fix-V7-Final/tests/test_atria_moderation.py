import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.cogs.atria import Atria


class AtriaBanPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def run_ban_verdict(self, allow_ai_ban=False):
        author = SimpleNamespace(bot=False, id=123, mention="@member", top_role=1)
        guild = SimpleNamespace(
            id=456,
            owner_id=999,
            me=SimpleNamespace(
                top_role=10,
                guild_permissions=SimpleNamespace(ban_members=True, moderate_members=True),
            ),
            ban=AsyncMock(),
        )
        message = SimpleNamespace(
            author=author,
            guild=guild,
            content="test message",
            channel=SimpleNamespace(
                id=789,
                send=AsyncMock(),
                permissions_for=lambda member: SimpleNamespace(send_messages=True),
            ),
        )
        cog = Atria(SimpleNamespace(user=SimpleNamespace(id=789)))
        settings = {
            "atria": {
                "moderation_enabled": True,
                "allow_ai_ban": allow_ai_ban,
            }
        }

        with patch("bot.cogs.atria.load_settings", return_value=settings), patch(
            "bot.cogs.atria.atria_chat",
            new=AsyncMock(return_value=json.dumps({"action": "ban", "classification": "toxicity", "confidence": 0.99, "reason": "test"})),
        ), patch(
            "bot.services.moderation_tools.moderation_tools.store.reserve_moderation_action",
            return_value=True,
        ), patch(
            "bot.services.moderation_tools.ranks.log_action",
        ), patch(
            "bot.cogs.atria.ai_store.log_moderation",
        ):
            await cog.on_message(message)

        return guild.ban

    async def test_ai_ban_is_blocked_without_explicit_opt_in(self):
        ban = await self.run_ban_verdict()

        ban.assert_not_awaited()

    async def test_ai_ban_runs_after_explicit_opt_in(self):
        ban = await self.run_ban_verdict(allow_ai_ban=True)

        ban.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()