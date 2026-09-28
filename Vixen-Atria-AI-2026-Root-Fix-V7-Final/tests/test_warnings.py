import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.warnings import issue_warning
from bot.utils.ai_store import AIStore


class WarningEscalationTests(unittest.IsolatedAsyncioTestCase):
    async def test_threshold_applies_one_deterministic_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AIStore(Path(directory) / "warnings.sqlite3")
            member = SimpleNamespace(
                id=20,
                top_role=1,
                send=AsyncMock(),
                timeout=AsyncMock(),
            )
            guild = SimpleNamespace(
                id=10,
                name="Test guild",
                owner_id=99,
                me=SimpleNamespace(
                    id=30,
                    top_role=10,
                    guild_permissions=SimpleNamespace(moderate_members=True),
                ),
            )
            with patch("bot.services.warnings.ai_store", store), patch(
                "bot.services.warnings.ranks.log_action"
            ):
                for _ in range(9):
                    result = await issue_warning(guild, member, 40, "Moderator", "spam", threshold=10)
                    self.assertEqual(result["escalation"], "not_due")
                result = await issue_warning(guild, member, 40, "Moderator", "spam", threshold=10)

        self.assertEqual(result["warning_count"], 10)
        self.assertEqual(result["escalation"], "timeout_applied")
        member.timeout.assert_awaited_once()
        self.assertEqual(member.send.await_count, 10)

    async def test_threshold_escalation_respects_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AIStore(Path(directory) / "warnings.sqlite3")
            member = SimpleNamespace(id=20, top_role=1, send=AsyncMock(), timeout=AsyncMock())
            guild = SimpleNamespace(
                id=10,
                name="Test guild",
                owner_id=99,
                me=SimpleNamespace(
                    id=30,
                    top_role=10,
                    guild_permissions=SimpleNamespace(moderate_members=True),
                ),
            )
            reserve = AsyncMock(return_value=False)
            with patch("bot.services.warnings.ai_store", store), patch(
                "bot.services.warnings.ranks.log_action"
            ):
                result = await issue_warning(
                    guild,
                    member,
                    40,
                    "Vixen AI",
                    "spam",
                    threshold=1,
                    reserve_escalation=reserve,
                )

        self.assertEqual(result["escalation"], "blocked_by_rate_limit")
        reserve.assert_awaited_once()
        member.timeout.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()