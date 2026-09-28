import asyncio
import io
import json
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.services.ai_chat import AIChatService
from bot.services.ai_intent import ai_intent_router

ROOT = Path(__file__).resolve().parents[1]


class AIUltraV3Tests(unittest.IsolatedAsyncioTestCase):
    def test_long_answer_is_split_not_truncated(self):
        markers = [f"SECTION-{i:03d}" for i in range(220)]
        text = "\n".join(markers)
        chunks = AIChatService._split_discord(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        joined = "\n".join(chunks)
        for marker in markers:
            self.assertIn(marker, joined)

    def test_code_fences_stay_balanced_across_chunks(self):
        text = "```python\n" + "\n".join(f"print('line {i}')" for i in range(400)) + "\n```"
        chunks = AIChatService._split_discord(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        for chunk in chunks:
            self.assertEqual(chunk.count("```") % 2, 0)

    async def test_stream_chunk_sync_grows_without_truncating(self):
        service = AIChatService(provider=SimpleNamespace(), store=SimpleNamespace())

        class FakeSent:
            next_id = 1
            def __init__(self, content):
                self.id = FakeSent.next_id
                FakeSent.next_id += 1
                self.content = content
                self.edits = 0
                self.deleted = False
            async def edit(self, *, content):
                self.content = content
                self.edits += 1
            async def delete(self):
                self.deleted = True

        created = []
        class FakeSource:
            def __init__(self):
                self.channel = self
            async def reply(self, content, **kwargs):
                msg = FakeSent(content)
                created.append(msg)
                return msg
            async def send(self, content, **kwargs):
                msg = FakeSent(content)
                created.append(msg)
                return msg

        source = FakeSource()
        small = "A" * 200
        large = "\n".join(f"LINE {i:03d} " + ("x" * 90) for i in range(70))
        messages, contents = await service._sync_discord_chunks(source, [], [], small)
        self.assertEqual(len(messages), 1)
        self.assertEqual(contents[0], small)
        messages, contents = await service._sync_discord_chunks(source, messages, contents, large)
        self.assertGreater(len(messages), 1)
        self.assertEqual(len(messages), len(contents))
        self.assertTrue(all(len(chunk) <= 1900 for chunk in contents))
        for i in range(70):
            self.assertIn(f"LINE {i:03d}", "\n".join(contents))

    def test_micro_greeting_with_discord_mention_gets_micro_route(self):
        route = ai_intent_router.route("Ahla wink <@1488236522921787413>", {"speed_mode": "flash"})
        self.assertEqual(route.intent, "greeting")
        self.assertEqual(route.max_output_tokens, 300)
        self.assertIn("micro-greeting", route.reason)

    def test_micro_social_quality_guard_flags_long_reply(self):
        from bot.services.ai_chat import AIChatService
        issues = AIChatService._quality_issues(
            "A" * 421, "Ahla wink <@1488236522921787413>", "Twix", []
        )
        self.assertIn("simple_prompt_overlong", issues)

    def test_micro_social_fallback_never_truncates_into_an_internal_fragment(self):
        from bot.services.ai_chat import AIChatService
        route = ai_intent_router.route("Ahla wink <@1488236522921787413>", {"speed_mode": "flash"})
        fallback = AIChatService._enforce_micro_social_contract("A" * 1000, "Ahla wink <@1488236522921787413>", route)
        self.assertLessEqual(len(fallback), 260)
        self.assertIn("houni", fallback)

    def test_user_example_does_not_get_forced_second_paragraph_by_contract(self):
        from bot.services.ai_chat import AIChatService
        profile = AIChatService._response_profile(
            "Ahla wink <@1488236522921787413>",
            ai_intent_router.route("Ahla wink <@1488236522921787413>", {"speed_mode": "flash"}),
            5, 3
        )
        self.assertIn("one short natural line", profile)
        self.assertIn("3-12 words", profile)

    def test_creative_intent_gets_real_route(self):
        route = ai_intent_router.route("اعطيني 5 افكار مضحكة لاسم البوت", {"speed_mode": "flash"})
        self.assertEqual(route.intent, "creative")
        self.assertGreaterEqual(route.max_output_tokens, 2200)

    def test_requirements_are_currently_pinned(self):
        requirements = (ROOT / "requirements.txt").read_text()
        expected = [
            "discord.py==2.7.1",
            "aiohttp==3.14.3",
            "python-dotenv==1.2.3",
            "PyNaCl==1.6.2",
            "davey==0.1.6",
            "orjson==3.12.0",
        ]
        for item in expected:
            self.assertIn(item, requirements)

    def test_exact_vex_banner_is_preserved(self):
        try:
            from discord.ext import commands  # noqa: F401
            signature = inspect.signature(commands.Bot)
            if "command_prefix" not in signature.parameters or "intents" not in signature.parameters:
                self.skipTest("test runtime does not provide the real discord.py Bot API")
        except (ModuleNotFoundError, TypeError, ValueError):
            self.skipTest("discord.py is not installed in this analysis environment")
        expected = """.:::::::.         .:::::::.       .::::::::::::::::.       .:::::::.           .:::::::.
 ':::::::.       .:::::::'        '::::::::::::::::::.     ':::::::.         .:::::::'
  ':::::::.     .:::::::'         '::::::::::::::::::.      ':::::::.       .:::::::'
   ':::::::.   .:::::::'          '::::::.                   ':::::::.     .:::::::'
    ':::::::. .:::::::'           '::::::.                    ':::::::.   .:::::::'
     ':::::::..:::::::'            '::::::::::::::.             ':::::::..:::::::'
      ':::::::::::::'             '::::::::::::::.              '::::::::::::'
       ':::::::::::'              '::::::.                     .::::::::::::.
        ':::::::::'               '::::::.                    .:::::::.  .:::::::.
         ':::::::'                '::::::::::::::::::.       .:::::::.    .:::::::.
          ':::::'                 '::::::::::::::::::.      .:::::::.      .:::::::.
           ':::'                  '::::::::::::::::::.     .:::::::.        .:::::::.
            ''                    '::::::::::::::::::.    .:::::::.          .:::::::.
""".strip("\n")
        import bot.main as main_module
        self.assertEqual(main_module.VIXEN_BANNER, expected)

    def test_follow_up_keywords_use_continuation_route(self):
        for text in ("كمل", "كمّل", "زيد", "نواصل", "واصل", "continue", "keep going"):
            route = ai_intent_router.route(text, {"speed_mode": "flash"})
            self.assertEqual(route.intent, "follow_up", text)

    def test_provider_public_error_text_hides_rate_limit_and_resend_prompts(self):
        provider = (ROOT / "bot/services/ai_provider.py").read_text(errors="ignore")
        chat = (ROOT / "bot/services/ai_chat.py").read_text(errors="ignore")
        server = (ROOT / "bot/web/server.py").read_text(errors="ignore")
        corpus = "\n".join((provider, chat, server))
        for bad in ("جرّب بعد", "جرّب بعد 12ث", "جرّب بعد 18ث", "رسالة أقصر", "قصّر رسالتك", "حاول نفس الرسالة مرة أخرى"):
            self.assertNotIn(bad, corpus)

    def test_startup_banner_renders_on_tty(self):
        try:
            from discord.ext import commands  # noqa: F401
            signature = inspect.signature(commands.Bot)
            if "command_prefix" not in signature.parameters or "intents" not in signature.parameters:
                self.skipTest("test runtime does not provide the real discord.py Bot API")
        except (ModuleNotFoundError, TypeError, ValueError):
            self.skipTest("discord.py is not installed in this analysis environment")
        import bot.main as main_module
        class TTYBuffer(io.StringIO):
            def isatty(self):
                return True
        output = TTYBuffer()
        with patch.object(main_module.sys, "stdout", output), patch.object(main_module.time, "sleep"):
            main_module.render_startup_banner()
        rendered = output.getvalue()
        self.assertIn(main_module.VIXEN_BANNER, rendered)
        self.assertIn("VEX // ATRIA AI SYSTEM", rendered)
        self.assertIn("✓ startup pipeline", rendered)

    def test_provider_observability_includes_ttft_and_total_latency(self):
        source = (ROOT / "bot/services/ai_provider.py").read_text(errors="ignore")
        self.assertIn("first_token_latency_ms", source)
        self.assertIn("total_latency_ms", source)

    def test_dashboard_has_longer_stream_budget_and_crlf_parser(self):
        js = (ROOT / "dashboard/assets/app.js").read_text()
        self.assertIn("240000", js)
        self.assertIn("/\\r?\\n\\r?\\n/", js)


if __name__ == "__main__":
    unittest.main()
