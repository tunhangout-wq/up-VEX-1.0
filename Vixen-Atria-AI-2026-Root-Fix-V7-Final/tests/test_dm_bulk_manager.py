import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services.dm_bulk import DMBulkManager
from bot.utils.dm_store import DMStore


class Member(SimpleNamespace):
    pass


class Role(SimpleNamespace):
    def is_default(self):
        return False


class DMBulkManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DMStore(Path(self.tmp.name) / 'dm.sqlite3')
        self.member = Member(id=22, name='mira', display_name='Mira', mention='<@22>', bot=False, roles=[Role(id=1, name='Member')], send=AsyncMock())
        self.guild = SimpleNamespace(id=11, name='Guild', member_count=1, members=[self.member], me=None, icon=None,
                                     get_member=lambda user_id: self.member if user_id == 22 else None,
                                     fetch_member=AsyncMock(return_value=self.member))
        self.bot = SimpleNamespace(guilds=[self.guild], user=None)
        self.manager = DMBulkManager(self.store, max_jobs=1)
        self.manager.bind_bot(self.bot)
        await self.manager.start()

    async def asyncTearDown(self):
        await self.manager.stop()
        self.tmp.cleanup()

    async def test_campaign_delivers_and_persists_result(self):
        job_id = self.manager.create_and_start(
            guild_id=11, sender_id=9, sender_name='Moderator', template_id=None, reason='',
            content='Hello {username}', embed={}, options={'messages_per_minute': 60}, recipient_ids=[22],
        )
        for _ in range(50):
            job = self.store.get_bulk_job(job_id)
            if job['status'] == 'completed':
                break
            await asyncio.sleep(0.02)
        job = self.store.get_bulk_job(job_id)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['success'], 1)
        self.member.send.assert_awaited_once()
        self.assertEqual(self.member.send.await_args.kwargs['content'], 'Hello mira')

    async def test_stop_finalizes_inflight_send_as_uncertain(self):
        gate = asyncio.Event()
        async def blocked_send(*args, **kwargs):
            await gate.wait()
        self.member.send.side_effect = blocked_send
        job_id = self.manager.create_and_start(
            guild_id=11, sender_id=9, sender_name='Moderator', template_id=None, reason='',
            content='Hello', embed={}, options={'messages_per_minute': 60}, recipient_ids=[22],
        )
        for _ in range(50):
            item = self.store.bulk_items(job_id)[0]
            if item['status'] == 'sending':
                break
            await asyncio.sleep(0.01)
        await self.manager.stop()
        job = self.store.get_bulk_job(job_id)
        self.assertEqual(job['status'], 'interrupted')
        self.assertEqual(job['uncertain'], 1)
        self.assertEqual(self.store.bulk_items(job_id)[0]['status'], 'uncertain')
