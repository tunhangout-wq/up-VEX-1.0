import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from bot.web import server


class BotProfileAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = SimpleNamespace(
            guilds=[],
            cogs={},
            user=None,
            latency=float("nan"),
            _vixen_started_at=0,
            is_ready=lambda: False,
        )
        self.client = TestClient(TestServer(server.create_web_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_profile_is_protected_and_presence_is_persisted_before_gateway(self):
        unauthorized = await self.client.get("/api/bot/profile")
        self.assertEqual(unauthorized.status, 401)
        with patch.object(server, "require_permission", return_value={"user_id": "4", "name": "Admin"}), \
             patch.object(server, "load_settings", return_value={"bot": {}}), \
             patch.object(server, "atomic_update_settings") as atomic_update, \
             patch.object(server.ranks, "log_action"):
            headers = {"Authorization": "Bearer test"}
            response = await self.client.get("/api/bot/profile", headers=headers)
            profile = await response.json()
            self.assertFalse(profile["connected"])
            self.assertIsNone(profile["latency_ms"])
            self.assertEqual(profile["commands"], 0)

            presence = await self.client.post(
                "/api/bot/presence",
                headers=headers,
                json={"type": "listening", "name": "Vixen radio"},
            )
            result = await presence.json()
            self.assertFalse(result["applied"])
            self.assertTrue(atomic_update.called)
            updater = atomic_update.call_args.args[0]
            settings = {"bot": {}}
            self.assertTrue(updater(settings))
            self.assertEqual(settings["bot"]["presence"], {"type": "listening", "name": "Vixen radio"})


if __name__ == "__main__":
    unittest.main()