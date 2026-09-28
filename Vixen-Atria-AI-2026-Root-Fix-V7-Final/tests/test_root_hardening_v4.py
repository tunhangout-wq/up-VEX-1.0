import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

from bot.services.ai_provider import AIProvider, AIProviderError
from bot.utils.ai_store import AIStore
from bot.utils import ranks

try:
    from bot.services.ai_chat import AIChatService
except ModuleNotFoundError:
    AIChatService = None


class _FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None, content_type="application/json"):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self.content = self
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self, *_):
        return self._body


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.created_posts = 0

    def post(self, url, **kwargs):
        self.created_posts += 1
        return _FakeResponse(200, json.dumps({"choices":[{"message":{"content":"OK"}}]}).encode())


class RootHardeningV4Tests(unittest.TestCase):
    def test_store_external_id_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            store = AIStore(Path(d) / "ai.sqlite3")
            first = store.add_chat_message(1, 2, 3, "user", "hello", "discord:123:user")
            second = store.add_chat_message(1, 2, 3, "user", "hello", "discord:123:user")
            self.assertEqual(first, second)
            self.assertEqual(len(store.recent_chat_entries(1, 2, 10)), 1)

    def test_store_migrates_existing_chat_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ai.sqlite3"
            import sqlite3
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE ai_chat_messages (message_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, timestamp REAL NOT NULL)")
            con.commit(); con.close()
            store = AIStore(path)
            store.add_chat_message(1, 2, 3, "user", "hello", "migrate:1")
            con = sqlite3.connect(path)
            cols = {row[1] for row in con.execute("PRAGMA table_info(ai_chat_messages)").fetchall()}
            con.close()
            self.assertIn("external_id", cols)

    def test_provider_active_request_counter_is_explicit(self):
        provider = AIProvider()
        self.assertEqual(provider.status()["active_requests"], 0)
        self.assertTrue(hasattr(provider, "_active_requests"))

    def test_provider_error_can_carry_partial_text(self):
        exc = AIProviderError("partial", partial_text="hello")
        self.assertEqual(exc.partial_text, "hello")

    def test_provider_close_resets_session_reference(self):
        class S:
            closed = False
            async def close(self):
                self.closed = True
        async def run():
            provider = AIProvider(); provider._session = S()
            await provider.close()
            self.assertIsNone(provider._session)
        asyncio.run(run())

    def test_one_use_activation_rejects_second_use(self):
        with tempfile.TemporaryDirectory() as d:
            staff = Path(d) / "staff.json"
            import bot.utils.ranks as rm
            payload = {"codes":{"CB-TEST":{"rank":"team","used_by":"99","used_at":1}},"staff":{},"usage":{}}
            staff.write_text(json.dumps(payload))
            with patch.object(rm, "STAFF_FILE", staff):
                result = rm.activate_code("CB-TEST", 100)
            self.assertFalse(result["ok"])
            self.assertIn("تم استخدامه مسبقًا", result["message"])

    @unittest.skipIf(AIChatService is None, "discord.py is not installed in this test environment")
    def test_split_discord_does_not_lstrip_boundary_content(self):
        text = "a" * 1900 + "\n  b"
        chunks = AIChatService._split_discord(text, limit=1900)
        self.assertTrue(len(chunks) >= 2)
        self.assertTrue(chunks[1].startswith("\n  b"))

    def test_user_default_fields_are_persisted_for_existing_users(self):
        import bot.utils.data_manager as dm
        with tempfile.TemporaryDirectory() as d:
            users_file = Path(d) / "users.json"
            users_file.write_text(json.dumps({"7": {"wallet": 250}}))
            with patch.object(dm, "USERS_FILE", users_file):
                user = dm.get_user(7, starting_balance=100)
                self.assertIn("job", user)
                persisted = json.loads(users_file.read_text())
                self.assertIn("job", persisted["7"])

    def test_users_and_loans_commit_together(self):
        import bot.utils.data_manager as dm
        with tempfile.TemporaryDirectory() as d:
            users_file = Path(d) / "users.json"
            loans_file = Path(d) / "loans.json"
            journal_file = Path(d) / "financial_transaction.json"
            users_file.write_text(json.dumps({"1": {"wallet": 100}, "2": {"wallet": 0}}))
            loans_file.write_text(json.dumps({"next_id": 2, "loans": [{"id": 1, "lender": "1", "borrower": "2", "principal": 50, "status": "pending"}]}))
            with patch.object(dm, "USERS_FILE", users_file), patch.object(dm, "LOANS_FILE", loans_file), patch.object(dm, "FINANCIAL_TXN_FILE", journal_file):
                def settle(users, loans):
                    users["1"]["wallet"] -= 50
                    users["2"]["wallet"] += 50
                    loans["loans"][0]["status"] = "active"
                    return True
                dm.atomic_update_users_and_loans([1, 2], settle)
                self.assertEqual(json.loads(users_file.read_text())["1"]["wallet"], 50)
                self.assertEqual(json.loads(users_file.read_text())["2"]["wallet"], 50)
                self.assertEqual(json.loads(loans_file.read_text())["loans"][0]["status"], "active")
                self.assertFalse(journal_file.exists())

    def test_moderation_dashboard_persists_settings_atomically(self):
        server_source = (Path(__file__).resolve().parents[1] / "bot/web/server.py").read_text()
        start = server_source.index("async def api_ai_moderation_settings")
        end = server_source.index("def _safe_ai_public_error", start)
        block = server_source[start:end]
        self.assertIn("atomic_update_settings(update_moderation_settings)", block)
        self.assertIn("legacy.update(moderation_enabled=enabled, timeout_minutes=timeout_minutes)", block)

    def test_interrupted_financial_commit_recovers_from_journal(self):
        import bot.utils.data_manager as dm
        with tempfile.TemporaryDirectory() as d:
            users_file = Path(d) / "users.json"
            loans_file = Path(d) / "loans.json"
            journal_file = Path(d) / "financial_transaction.json"
            users_file.write_text(json.dumps({"1": {"wallet": 100}, "2": {"wallet": 0}}))
            loans_file.write_text(json.dumps({"next_id": 2, "loans": [{"id": 1, "status": "pending"}]}))
            original_atomic = dm._atomic_write
            calls = {"n": 0}
            def fail_on_loan_write(path, data):
                calls["n"] += 1
                if calls["n"] == 3:
                    raise OSError("simulated crash after users commit")
                return original_atomic(path, data)
            with patch.object(dm, "USERS_FILE", users_file), patch.object(dm, "LOANS_FILE", loans_file), patch.object(dm, "FINANCIAL_TXN_FILE", journal_file), patch.object(dm, "_atomic_write", side_effect=fail_on_loan_write):
                def settle(users, loans):
                    users["1"]["wallet"] = 50
                    users["2"]["wallet"] = 50
                    loans["loans"][0]["status"] = "active"
                    return True
                with self.assertRaises(OSError):
                    dm.atomic_update_users_and_loans([1, 2], settle)
            self.assertTrue(journal_file.exists())
            dm._atomic_write = original_atomic
            try:
                with patch.object(dm, "USERS_FILE", users_file), patch.object(dm, "LOANS_FILE", loans_file), patch.object(dm, "FINANCIAL_TXN_FILE", journal_file):
                    recovered_users = dm.load_users()
                    recovered_loans = dm.load_loans()
                    self.assertEqual(recovered_users["1"]["wallet"], 50)
                    self.assertEqual(recovered_users["2"]["wallet"], 50)
                    self.assertEqual(recovered_loans["loans"][0]["status"], "active")
                    self.assertFalse(journal_file.exists())
            finally:
                dm._atomic_write = original_atomic


if __name__ == "__main__":
    unittest.main()


class RootSecurityV4Tests(unittest.TestCase):
    def test_owner_id_ignores_mutable_settings_override(self):
        import bot.utils.ranks as rm
        with patch.dict(os.environ, {"OWNER_ID": "123456789"}, clear=False), patch.object(
            rm, "load_settings", return_value={"ranks": {"owner_id": "999999999"}}
        ):
            self.assertEqual(rm.get_owner_id(), 123456789)
            self.assertFalse(rm.is_owner(999999999))

    def test_owner_id_fails_closed_without_valid_environment_value(self):
        import bot.utils.ranks as rm
        with patch.dict(os.environ, {"OWNER_ID": "not-an-id"}, clear=False):
            self.assertIsNone(rm.get_owner_id())


class RootConcurrencyV4Tests(unittest.TestCase):
    def test_provider_open_is_singleton_under_concurrent_start(self):
        async def run():
            provider = AIProvider()
            created = []

            class Session:
                closed = False

                async def close(self):
                    self.closed = True

            def factory(*args, **kwargs):
                session = Session()
                created.append(session)
                return session

            with patch("bot.services.ai_provider.aiohttp.ClientSession", side_effect=factory):
                await asyncio.gather(*(provider.open() for _ in range(12)))
                self.assertEqual(len(created), 1)
                self.assertIs(provider._session, created[0])
                await provider.close()
                self.assertIsNone(provider._session)

        asyncio.run(run())

    def test_atomic_loan_update_does_not_allow_stale_snapshot_overwrite(self):
        import bot.utils.data_manager as dm
        with tempfile.TemporaryDirectory() as d:
            loan_file = Path(d) / "loans.json"
            loan_file.write_text(json.dumps({"next_id": 2, "loans": [{"id": 1, "status": "active", "paid": 0, "total": 100, "due": 1}]}))
            with patch.object(dm, "LOANS_FILE", loan_file):
                result = dm.atomic_update_loans(
                    lambda data: next(l for l in data["loans"] if l["id"] == 1).update(status="overdue") or True
                )
                self.assertEqual(result["loans"][0]["status"], "overdue")
                latest = dm.load_loans()
                self.assertEqual(latest["loans"][0]["status"], "overdue")

class RootProviderConcurrencyRegressionTests(unittest.TestCase):
    def test_stream_continuation_runs_after_outer_request_slot_is_released(self):
        from contextlib import asynccontextmanager

        async def run():
            provider = AIProvider()
            with patch.object(AIProvider, "api_key", new_callable=PropertyMock, return_value="test-key"):
                class Content:
                    def __init__(self):
                        self._lines = [
                            b'data: {"choices":[{"delta":{"content":"part"},"finish_reason":null}]}\n\n',
                            b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
                            b'data: [DONE]\n\n',
                        ]

                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        if not self._lines:
                            raise StopAsyncIteration
                        return self._lines.pop(0)

                response = type("Response", (), {"status": 200, "content": Content()})()

                @asynccontextmanager
                async def gated(*args, **kwargs):
                    await provider._request_gate.acquire()
                    provider._active_requests += 1
                    try:
                        yield response
                    finally:
                        provider._active_requests -= 1
                        provider._request_gate.release()

                async def recover(*args, **kwargs):
                    self.assertEqual(provider._active_requests, 0)
                    return "continued"

                provider._gated_post = gated
                provider._recover_continuation = recover
                try:
                    output = [delta async for delta in provider.stream(
                        [{"role": "user", "content": "hello"}], max_tokens=10, auto_continue=True
                    )]
                    self.assertEqual(output, ["part", " continued"])
                finally:
                    await provider.close()

        asyncio.run(run())

    def test_complete_continuation_runs_after_outer_request_slot_is_released(self):
        from contextlib import asynccontextmanager

        async def run():
            provider = AIProvider()
            with patch.object(AIProvider, "api_key", new_callable=PropertyMock, return_value="test-key"):
                body = json.dumps({"choices":[{"message":{"content":"part"},"finish_reason":"length"}]}).encode()
                response = _FakeResponse(200, body)

                @asynccontextmanager
                async def gated(*args, **kwargs):
                    await provider._request_gate.acquire()
                    provider._active_requests += 1
                    try:
                        yield response
                    finally:
                        provider._active_requests -= 1
                        provider._request_gate.release()

                async def recover(*args, **kwargs):
                    self.assertEqual(provider._active_requests, 0)
                    return "continued"

                provider._gated_post = gated
                provider._recover_continuation = recover
                try:
                    result = await provider.complete(
                        [{"role": "user", "content": "hello"}], max_tokens=10, auto_continue=True
                    )
                    self.assertEqual(result, "part continued")
                finally:
                    await provider.close()

        asyncio.run(run())
