import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from bot.utils.dm_store import DMStore
from bot.web import server


class Role(SimpleNamespace):
    def is_default(self):
        return False


class FakeMember(SimpleNamespace):
    pass


def _virtual_session(code):
    return {'user_id': '0', 'name': 'Virtual Moderator', 'virtual': True, 'code': code}


class DMBulkAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = DMStore(Path(self.tempdir.name) / 'dm.sqlite3')
        self.members = [
            FakeMember(id=22, name='mira', display_name='Mira', mention='<@22>', bot=False, roles=[Role(id=1, name='Member')]),
            FakeMember(id=23, name='botty', display_name='Botty', mention='<@23>', bot=True, roles=[Role(id=1, name='Member')]),
            FakeMember(id=24, name='staff', display_name='Staff', mention='<@24>', bot=False, roles=[Role(id=2, name='Staff')]),
        ]
        self.guild = SimpleNamespace(
            id=11,
            name='Tun Hangout',
            member_count=3,
            members=self.members,
            icon=None,
            me=self.members[1],
            get_member=lambda user_id: next((m for m in self.members if m.id == user_id), None),
            fetch_member=AsyncMock(side_effect=lambda user_id: next((m for m in self.members if m.id == user_id), None)),
            chunk=AsyncMock(),
        )
        self.bot = SimpleNamespace(guilds=[self.guild], user=None)
        self.client = TestClient(TestServer(server.create_web_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tempdir.cleanup()

    async def test_role_mode_requires_include_role(self):
        with patch.object(server, 'require_permission', return_value={'user_id': '9', 'name': 'Moderator'}), \
             patch.object(server, 'dm_store', self.store):
            response = await self.client.post(
                '/api/dm/bulk/preview',
                headers={'Authorization': 'Bearer test'},
                json={'guild_id': '11', 'filters': {'mode': 'roles', 'role_ids': []}, 'content': 'Hi {username}'},
            )
        self.assertEqual(response.status, 400)
        self.assertIn('Role', (await response.json())['error'])

    async def test_bulk_preview_snapshots_filtered_audience(self):
        with patch.object(server, 'require_permission', return_value={'user_id': '9', 'name': 'Moderator'}), \
             patch.object(server, 'dm_store', self.store):
            response = await self.client.post(
                '/api/dm/bulk/preview',
                headers={'Authorization': 'Bearer test'},
                json={'guild_id': '11', 'filters': {'mode': 'all', 'exclude_bots': True, 'exclude_staff': False, 'exclude_self': False}, 'content': 'Hi {username}', 'messages_per_minute': 25},
            )
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload['count'], 2)
        self.assertEqual(payload['messages_per_minute'], 25)
        self.assertEqual(sorted(payload['sample']), ['22', '24'])
        self.assertTrue(payload['preview_token'])

    async def test_bulk_control_rejects_other_sender(self):
        self.store.create_bulk_job('job-auth', 11, 9, 'Moderator', None, '', 'Hi', {'title': 'T'}, {'sender_code': ''}, [22])
        with patch.object(server, 'require_permission', return_value={'user_id': '10', 'name': 'Other'}), \
             patch.object(server, 'dm_store', self.store):
            response = await self.client.post(
                '/api/dm/bulk/control',
                headers={'Authorization': 'Bearer test'},
                json={'job_id': 'job-auth', 'action': 'pause'},
            )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.store.get_bulk_job('job-auth')['status'], 'queued')


    async def test_virtual_bulk_preview_is_bound_to_staff_code(self):
        with patch.object(server, 'require_permission', return_value=_virtual_session('CODE-A')), \
             patch.object(server, 'dm_store', self.store):
            preview_response = await self.client.post(
                '/api/dm/bulk/preview',
                headers={'Authorization': 'Bearer test'},
                json={'guild_id': '11', 'filters': {'mode': 'selected', 'member_ids': ['22']}, 'content': 'Hi {username}'},
            )
            token = (await preview_response.json())['preview_token']

        with patch.object(server, 'require_permission', return_value=_virtual_session('CODE-B')), \
             patch.object(server, 'dm_store', self.store):
            rejected = await self.client.post(
                '/api/dm/bulk/start',
                headers={'Authorization': 'Bearer test'},
                json={'preview_token': token, 'confirm': True, 'confirmed_count': 1},
            )
        self.assertEqual(rejected.status, 403)

    async def test_bulk_control_allows_creator_to_pause(self):
        self.store.create_bulk_job('job-owner', 11, 9, 'Moderator', None, '', 'Hi', {'title': 'T'}, {}, [22])
        with patch.object(server, 'require_permission', return_value={'user_id': '9', 'name': 'Moderator'}), \
             patch.object(server, 'dm_store', self.store), \
             patch.object(server.ranks, 'log_action'):
            response = await self.client.post(
                '/api/dm/bulk/control',
                headers={'Authorization': 'Bearer test'},
                json={'job_id': 'job-owner', 'action': 'pause'},
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.get_bulk_job('job-owner')['status'], 'paused')


if __name__ == '__main__':
    unittest.main()

