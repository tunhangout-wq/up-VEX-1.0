import asyncio
import os
import unittest
from pathlib import Path

from bot.services.ai_intent import ai_intent_router
from bot.services.ai_orchestrator import ai_orchestrator
from bot.services.ai_provider import AIProvider, AIProviderError
from bot.services.ai_chat import AIChatService

ROOT = Path(__file__).resolve().parents[1]


class RootHardening2026Tests(unittest.TestCase):
    def test_discord_mention_greeting_is_micro_chat(self):
        text = "Ahla wink <@1488236522921787413>"
        route = ai_intent_router.route(text, {"speed_mode": "flash"})
        plan = ai_orchestrator.plan(text, {"speed_mode": "flash"})
        self.assertEqual(route.intent, "greeting")
        self.assertEqual(plan.intent, "greeting")
        self.assertGreaterEqual(plan.max_output_tokens, 300)

    def test_combined_laugh_and_greeting_stays_social(self):
        route = ai_intent_router.route("هههه لاباس؟", {"speed_mode": "flash"})
        self.assertEqual(route.intent, "greeting")

    def test_technical_timeout_is_not_misclassified_as_admin_action(self):
        route = ai_intent_router.route("كيفاش نصلح timeout في aiohttp؟", {"speed_mode": "flash"})
        self.assertNotEqual(route.intent, "owner_action")
        self.assertEqual(route.intent, "code_or_complex")

    def test_tiny_ack_has_small_budget(self):
        route = ai_intent_router.route("ok", {"speed_mode": "flash"})
        plan = ai_orchestrator.plan("ok", {"speed_mode": "flash"})
        self.assertEqual(route.intent, "casual")
        self.assertGreaterEqual(plan.max_output_tokens, 800)

    def test_social_contract_does_not_truncate_real_question(self):
        class ServiceProbe(AIChatService):
            pass
        route = ai_intent_router.route("كيفاش نصلح timeout في aiohttp؟", {"speed_mode": "flash"})
        answer = "هذا جواب طويل لكنه مفيد. " * 80
        self.assertEqual(route.intent, "code_or_complex")
        self.assertGreater(len(ServiceProbe._enforce_micro_social_contract(answer, "كيفاش نصلح timeout في aiohttp؟", route)), 520)

    def test_social_contract_falls_back_only_for_greeting(self):
        text = "Ahla wink <@1488236522921787413>"
        route = ai_intent_router.route(text, {})
        long_answer = "This is far too long. " * 80
        fixed = AIChatService._enforce_micro_social_contract(long_answer, text, route)
        self.assertLessEqual(len(fixed), 260)
        self.assertIn("Ahla", fixed)

    def test_quality_metrics_keep_decimal_precision(self):
        service = AIChatService()
        service._metrics["quality_evals"] = 2
        service._metrics["quality_score_sum"] = 1.37
        service._metrics["quality_score_last"] = 0.83
        metrics = service.metrics()
        self.assertAlmostEqual(metrics["quality_score_avg"], 0.685, places=3)
        self.assertAlmostEqual(metrics["quality_score_last"], 0.83, places=3)

    def test_provider_status_does_not_claim_unverified_context_window(self):
        provider = AIProvider()
        caps = provider.status()["capabilities"]
        self.assertNotIn("context_window_tokens", caps)
        self.assertTrue(caps["streaming"])

    def test_circuit_breaker_opens_after_repeated_transient_failures(self):
        async def scenario():
            provider = AIProvider()
            await provider._circuit_failure()
            await provider._circuit_failure()
            await provider._circuit_failure()
            with self.assertRaises(AIProviderError) as ctx:
                await provider._circuit_check()
            return ctx.exception.safe_message
        message = asyncio.run(scenario())
        self.assertIn("تمّ إيقاف المحاولات الزائدة", message)
        self.assertNotIn("12ث", message)
        self.assertNotIn("18ث", message)

    def test_banner_and_heartbeat_are_in_main(self):
        main = (ROOT / "bot/main.py").read_text(errors="ignore")
        self.assertIn("VIXEN_BANNER =", main)
        self.assertIn("asyncio.sleep(60)", main)
        self.assertIn("active=%s circuit=%s", main)
        self.assertIn('getattr(bot, "_vixen_commands_synced", False)', main)

    def test_stream_path_initializes_last_sent(self):
        source = (ROOT / "bot/services/ai_chat.py").read_text(errors="ignore")
        self.assertIn('last_sent = ""', source)
        self.assertIn("len(visible) - len(last_sent)", source)

    def test_discord_micro_social_bypasses_provider_and_context_hydration(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        class ProviderProbe:
            def __init__(self):
                self.calls = 0
            async def complete(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("Discord micro-social must not call Atria")
        async def scenario():
            provider = ProviderProbe()
            store_calls = {"recent": 0, "prune": 0}
            store = SimpleNamespace(
                add_chat_message=lambda *args: None,
                recent_chat_entries=lambda *args: store_calls.__setitem__("recent", store_calls["recent"] + 1) or [],
                prune_chat=lambda *args: store_calls.__setitem__("prune", store_calls["prune"] + 1),
            )
            service = AIChatService(provider=provider, store=store)
            reply = AsyncMock(return_value=SimpleNamespace(id=777))
            channel = SimpleNamespace(id=10, permissions_for=lambda _: SimpleNamespace(send_messages=True))
            author = SimpleNamespace(id=3, bot=False, display_name="Khalil")
            message = SimpleNamespace(
                id=123, guild=SimpleNamespace(id=1, name="Guild"), channel=channel, author=author,
                content="Ahla wink <@9>", mentions=[], role_mentions=[], reply=reply,
            )
            bot = SimpleNamespace(user=SimpleNamespace(id=9, name="Vixen"))
            config = {"enabled": True, "allowed_channels": ["10"], "response_mode": "every_message", "micro_social_fast_lane": True}
            result = await service.handle_message(bot, message, config)
            return result, provider.calls, store_calls, reply.await_args.args[0], service.metrics()
        result, calls, store_calls, sent, metrics = asyncio.run(scenario())
        self.assertTrue(result)
        self.assertEqual(calls, 0)
        self.assertEqual(store_calls["recent"], 0)
        self.assertLessEqual(len(sent), 100)
        self.assertEqual(metrics["micro_social_provider_bypass"], 1)

    def test_micro_social_fast_lane_is_provider_free(self):
        class ProviderProbe:
            def __init__(self):
                self.calls = 0
            async def complete(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("micro-social must not call the provider")
        async def scenario():
            provider = ProviderProbe()
            service = AIChatService(provider=provider)
            config = {"speed_mode": "flash", "micro_social_fast_lane": True}
            answer = await service.test_prompt("Ahla wink <@1488236522921787413>", config)
            return provider.calls, answer, service.metrics()
        calls, answer, metrics = asyncio.run(scenario())
        self.assertEqual(calls, 0)
        self.assertLessEqual(len(answer), 100)
        self.assertGreaterEqual(metrics["micro_social_provider_bypass"], 1)

    def test_micro_social_variants_remain_readable(self):
        samples = [
            "Ahla wink <@1488236522921787413>",
            "ahla <@1>",
            "winek bro",
            "سلام وينك",
        ]
        for sample in samples:
            route = ai_intent_router.route(sample, {})
            answer = AIChatService._micro_social_fallback(sample)
            self.assertEqual(route.intent, "greeting")
            self.assertLessEqual(len(answer), 100)
            self.assertNotRegex(answer, r"\bnrj3\b|\baywa\b")

    def test_micro_social_fast_lane_can_be_disabled(self):
        service = AIChatService()
        route = ai_intent_router.route("Ahla wink <@1>", {})
        self.assertTrue(service._use_micro_social_fast_lane({"micro_social_fast_lane": True}, route))
        self.assertFalse(service._use_micro_social_fast_lane({"micro_social_fast_lane": False}, route))


if __name__ == "__main__":
    unittest.main()
