import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from bot.utils.ai_store import AIStore
from bot.web import server


class AIChatLogEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AIStore(Path(self.tempdir.name) / "chat.sqlite3")
        self.store.add_chat_message(1, 10, 20, "user", "allowed conversation")
        self.store.add_chat_message(1, 11, 21, "user", "private conversation")
        guild = SimpleNamespace(
            id=1,
            get_channel=lambda channel_id: SimpleNamespace(id=channel_id) if channel_id in {10, 11} else None,
        )
        self.client = TestClient(TestServer(server.create_web_app(SimpleNamespace(guilds=[guild]))))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tempdir.cleanup()

    async def test_transcript_requires_auth_and_allowed_channel(self):
        unauthorized = await self.client.get("/api/ai/chat/logs?channel_id=10")
        self.assertEqual(unauthorized.status, 401)
        with patch.object(server, "require_permission", return_value={"user_id": "9"}), \
             patch.object(server, "load_settings", return_value={"ai": {"chat": {"allowed_channels": ["10"]}}}), \
             patch.object(server, "ai_store", self.store):
            headers = {"Authorization": "Bearer test"}
            allowed = await self.client.get("/api/ai/chat/logs?channel_id=10", headers=headers)
            denied = await self.client.get("/api/ai/chat/logs?channel_id=11", headers=headers)

        self.assertEqual(len((await allowed.json())["messages"]), 1)
        self.assertEqual(denied.status, 403)


if __name__ == "__main__":
    unittest.main()