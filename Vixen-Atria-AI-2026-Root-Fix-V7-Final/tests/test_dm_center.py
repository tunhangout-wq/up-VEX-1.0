import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer

from bot.utils.dm_store import DMStore
from bot.utils.ai_store import AIStore
from bot.web import server


class DMCenterEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = DMStore(Path(self.tempdir.name) / "dm.sqlite3")
        self.ai_store = AIStore(Path(self.tempdir.name) / "ai.sqlite3")
        self.member = SimpleNamespace(
            id=22,
            name="mira",
            display_name="Mira",
            mention="<@22>",
            send=AsyncMock(),
        )
        self.guild = SimpleNamespace(
            id=11,
            name="Tun Hangout",
            member_count=30,
            icon=None,
            primary_color=None,
            get_member=lambda user_id: self.member if user_id == 22 else None,
        )
        bot = SimpleNamespace(guilds=[self.guild], user=None)
        self.client = TestClient(TestServer(server.create_web_app(bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tempdir.cleanup()

    async def test_confirmed_dm_uses_one_time_preview_and_persists_history(self):
        design = {"title": "Welcome {username}", "description": "Welcome to {server}: {reason}"}
        with patch.object(server, "require_permission", return_value={"user_id": "9", "name": "Moderator"}), \
             patch.object(server, "dm_store", self.store), \
               patch.object(server, "ai_store", self.ai_store), \
             patch.object(server.ranks, "log_action"):
            headers = {"Authorization": "Bearer test"}
            created = await self.client.post(
                "/api/dm/templates",
                headers=headers,
                json={"guild_id": "11", "action": "save", "name": "Welcome", "embed": design},
            )
            template_id = (await created.json())["template_id"]
            preview_response = await self.client.post(
                "/api/dm/preview",
                headers=headers,
                json={"guild_id": "11", "member_id": "22", "template_id": template_id, "embed": design, "reason": "Joined"},
            )
            preview = await preview_response.json()
            self.assertEqual(preview["embed"]["title"], "Welcome mira")
            payload = {
                "guild_id": "11",
                "member_id": "22",
                "confirm": True,
                "preview_token": preview["preview_token"],
            }
            sent = await self.client.post("/api/dm/send", headers=headers, json=payload)
            self.assertEqual(sent.status, 200)
            self.member.send.assert_awaited_once()
            replay = await self.client.post("/api/dm/send", headers=headers, json=payload)
            self.assertEqual(replay.status, 409)
            history = await self.client.get("/api/dm/history?guild_id=11", headers=headers)
            self.assertEqual((await history.json())["history"][0]["status"], "success")

    async def test_preview_binding_mismatch_does_not_burn_token(self):
        design = {"title": "Notice", "description": "Hello"}
        with patch.object(server, "require_permission", return_value={"user_id": "9", "name": "Moderator"}), \
             patch.object(server, "dm_store", self.store), \
             patch.object(server.ranks, "log_action"):
            headers = {"Authorization": "Bearer test"}
            preview = await self.client.post(
                "/api/dm/preview", headers=headers,
                json={"guild_id": "11", "member_id": "22", "embed": design},
            )
            token = (await preview.json())["preview_token"]
            with patch.object(server, "require_permission", return_value={"user_id": "10", "name": "Other Moderator"}):
                rejected = await self.client.post(
                    "/api/dm/send", headers=headers,
                    json={"guild_id": "11", "member_id": "22", "confirm": True, "preview_token": token},
                )
            self.assertEqual(rejected.status, 403)

            restored = await self.client.post(
                "/api/dm/send", headers=headers,
                json={"guild_id": "11", "member_id": "22", "confirm": True, "preview_token": token},
            )
            self.assertEqual(restored.status, 200)
            self.member.send.assert_awaited_once()

    async def test_concurrent_dm_confirmation_can_spend_token_only_once(self):
        design = {"title": "Notice", "description": "Hello"}
        gate = asyncio.Event()
        async def gated_send(*args, **kwargs):
            await gate.wait()

        self.member.send = AsyncMock(side_effect=gated_send)
        with patch.object(server, "require_permission", return_value={"user_id": "9", "name": "Moderator"}), \
             patch.object(server, "dm_store", self.store), \
             patch.object(server.ranks, "log_action"):
            headers = {"Authorization": "Bearer test"}
            preview = await self.client.post(
                "/api/dm/preview", headers=headers,
                json={"guild_id": "11", "member_id": "22", "embed": design},
            )
            token = (await preview.json())["preview_token"]
            payload = {"guild_id": "11", "member_id": "22", "confirm": True, "preview_token": token}
            first = asyncio.create_task(self.client.post("/api/dm/send", headers=headers, json=payload))
            await asyncio.sleep(0.01)
            second = await self.client.post("/api/dm/send", headers=headers, json=payload)
            self.assertEqual(second.status, 409)
            gate.set()
            self.assertEqual((await first).status, 200)
            self.member.send.assert_awaited_once()

    async def test_failed_discord_dm_is_logged_as_failed(self):
        design = {"title": "Notice", "description": "Hello"}
        response = Mock(status=403, headers={}, reason="Forbidden")
        self.member.send.side_effect = discord.Forbidden(response, "DM closed")
        with patch.object(server, "require_permission", return_value={"user_id": "9", "name": "Moderator"}), \
             patch.object(server, "dm_store", self.store), \
               patch.object(server, "ai_store", self.ai_store), \
             patch.object(server.ranks, "log_action"):
            headers = {"Authorization": "Bearer test"}
            preview = await self.client.post(
                "/api/dm/preview",
                headers=headers,
                json={"guild_id": "11", "member_id": "22", "embed": design},
            )
            token = (await preview.json())["preview_token"]
            sent = await self.client.post(
                "/api/dm/send",
                headers=headers,
                json={"guild_id": "11", "member_id": "22", "confirm": True, "preview_token": token},
            )
            self.assertEqual(sent.status, 502)
            history = await self.client.get("/api/dm/history?guild_id=11", headers=headers)
            self.assertEqual((await history.json())["history"][0]["status"], "failed")

    async def test_starter_templates_install_once_and_are_editable(self):
        with patch.object(server, "require_permission", return_value={"user_id": "9", "name": "Moderator"}), \
             patch.object(server, "dm_store", self.store), \
             patch.object(server.ranks, "log_action"):
            headers = {"Authorization": "Bearer test"}
            first = await self.client.post(
                "/api/dm/templates",
                headers=headers,
                json={"guild_id": "11", "action": "install_defaults"},
            )
            second = await self.client.post(
                "/api/dm/templates",
                headers=headers,
                json={"guild_id": "11", "action": "install_defaults"},
            )

        self.assertEqual((await first.json())["created"], 12)
        self.assertEqual((await second.json())["created"], 0)
        self.assertEqual(len(self.store.list_templates(11)), 12)

    async def test_template_create_update_duplicate_delete_routes(self):
        with patch.object(server, "require_permission", return_value={"user_id": "9", "name": "Moderator"}), \
             patch.object(server, "dm_store", self.store), \
             patch.object(server.ranks, "log_action"):
            headers = {"Authorization": "Bearer test"}
            created = await self.client.post(
                "/api/dm/templates",
                headers=headers,
                json={"guild_id": "11", "action": "save", "name": "Notice", "embed": {"title": "First"}},
            )
            template_id = (await created.json())["template_id"]
            updated = await self.client.post(
                "/api/dm/templates",
                headers=headers,
                json={"guild_id": "11", "action": "save", "template_id": template_id, "name": "Notice", "embed": {"title": "Updated"}},
            )
            duplicate = await self.client.post(
                "/api/dm/templates",
                headers=headers,
                json={"guild_id": "11", "action": "duplicate", "template_id": template_id, "name": "Copy"},
            )
            duplicate_id = (await duplicate.json())["template_id"]
            removed = await self.client.post(
                "/api/dm/templates",
                headers=headers,
                json={"guild_id": "11", "action": "delete", "template_id": duplicate_id},
            )

        self.assertEqual(updated.status, 200)
        self.assertEqual(removed.status, 200)
        self.assertEqual(self.store.get_template(11, template_id)["embed"]["title"], "Updated")
        self.assertEqual(len(self.store.list_templates(11)), 1)


if __name__ == "__main__":
    unittest.main()