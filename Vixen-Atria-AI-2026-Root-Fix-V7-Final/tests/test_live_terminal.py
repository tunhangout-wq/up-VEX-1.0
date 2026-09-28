import asyncio
import json
import logging
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from bot.services.live_terminal import install_live_terminal, live_terminal
from bot.web import server


class LiveTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        install_live_terminal()
        self.client = TestClient(TestServer(server.create_web_app(None)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for queue in tuple(live_terminal.queues):
            live_terminal.unsubscribe(queue)

    async def test_stream_requires_auth_and_delivers_real_log_records(self):
        denied = await self.client.get("/api/terminal/stream")
        self.assertEqual(denied.status, 401)

        with patch.object(server, "require_permission", return_value={"user_id": "7"}):
            response = await self.client.get(
                "/api/terminal/stream",
                headers={"Authorization": "Bearer test"},
            )
            logging.getLogger("vixen.terminal.test").warning("terminal-regression-event")
            found = False
            for _ in range(200):
                line = await asyncio.wait_for(response.content.readline(), timeout=2)
                if not line.startswith(b"data: "):
                    continue
                event = json.loads(line[6:])
                if event["message"] == "terminal-regression-event":
                    found = True
                    break
            response.close()

        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()