"""
rob.py
-------
نظام السرقة بين الأعضاء (Rob) بمخاطرة:
  - /rob @عضو → تحاول تسرق جزء من محفظته
  - نجاح → تاخذ نسبة من فلوس الضحية
  - فشل → تدفع غرامة (تروح للضحية) + كولداون أطول
  - /robstats → إحصائيات سرقاتك

كل النسب والفرص تُضبط من لوحة التحكم: settings.rob
"""

import random
import logging
import math

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.data_manager import atomic_update_users, get_user, load_settings, now_ts
from bot.utils.embeds import base_embed, success_embed, error_embed, currency

logger = logging.getLogger(__name__)


def fmt_left(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h} ساعة و {m} دقيقة"
    return f"{m} دقيقة و {s} ثانية"


class Rob(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------- السرقة
    @commands.hybrid_command(name="rob", aliases=["سرقة", "اسرق"], description="حاول تسرق عضو آخر (بمخاطرة!)")
    @app_commands.describe(member="الضحية")
    async def rob(self, ctx: commands.Context, member: discord.Member):
        cfg = load_settings().get("rob", {})
        if not cfg.get("enabled", True):
            await ctx.send(embed=error_embed("معطل", "نظام السرقة معطل من الإدارة."))
            return
        if member.bot or member.id == ctx.author.id:
            await ctx.send(embed=error_embed("خطأ", "اختر ضحية صحيحة غيرك!"))
            return

        settings = load_settings().get("economy", {})
        try:
            min_wallet = int(cfg.get("min_victim_wallet", 100))
            cooldown = int(cfg.get("cooldown_minutes", 30)) * 60
            success_chance = float(cfg.get("success_chance", 45))
            max_steal_pct = float(cfg.get("max_steal_percent", 40))
            fine_pct = float(cfg.get("fine_percent", 25))
            max_wallet = int(settings["max_wallet"])
            starting_balance = int(settings["starting_balance"])
            if (
                min_wallet < 1 or cooldown < 0 or max_wallet < 0
                or not math.isfinite(success_chance) or not 0 <= success_chance <= 100
                or not math.isfinite(max_steal_pct) or not 0 < max_steal_pct <= 100
                or not math.isfinite(fine_pct) or not 0 <= fine_pct <= 100
            ):
                raise ValueError("invalid robbery settings")
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.exception("Invalid robbery settings")
            await ctx.send(embed=error_embed("خطأ", "إعدادات السرقة غير صالحة."))
            return

        now = now_ts()
        outcome = {}

        def rob_transaction(users):
            robber = users[str(ctx.author.id)]
            victim = users[str(member.id)]
            try:
                robber_wallet = max(0, int(robber.get("wallet", 0)))
                victim_wallet = max(0, int(victim.get("wallet", 0)))
                last_rob = int(robber.get("last_rob", 0))
                successes = max(0, int(robber.get("rob_success", 0)))
                failures = max(0, int(robber.get("rob_fail", 0)))
                profits = max(0, int(robber.get("rob_profits", 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if last_rob < 0 or last_rob > now:
                last_rob = 0
            elapsed = now - last_rob
            if elapsed < cooldown:
                outcome["remaining"] = cooldown - elapsed
                return False
            if robber_wallet < min_wallet:
                outcome["poor_robber"] = True
                return False
            if victim_wallet < min_wallet:
                outcome["poor_victim"] = victim_wallet
                return False

            if random.uniform(0, 100) <= success_chance:
                capacity = max(0, max_wallet - robber_wallet)
                stolen = min(
                    max(1, int(victim_wallet * (max_steal_pct / 100) * random.uniform(0.5, 1.0))),
                    victim_wallet,
                    capacity,
                )
                if stolen <= 0:
                    outcome["full"] = True
                    return False
                robber.update({
                    "wallet": robber_wallet + stolen,
                    "last_rob": now,
                    "rob_success": successes + 1,
                    "rob_profits": profits + stolen,
                })
                victim["wallet"] = victim_wallet - stolen
                outcome.update({"success": True, "amount": stolen, "wallet": robber_wallet + stolen})
            else:
                fine = min(robber_wallet, max(1, int(robber_wallet * fine_pct / 100)))
                robber.update({
                    "wallet": robber_wallet - fine,
                    "last_rob": now,
                    "rob_fail": failures + 1,
                })
                if cfg.get("fine_to_victim", True):
                    victim["wallet"] = min(max_wallet, victim_wallet + fine)
                outcome.update({
                    "success": False,
                    "amount": fine,
                    "wallet": robber_wallet - fine,
                    "fine_to_victim": bool(cfg.get("fine_to_victim", True)),
                })
            return True

        try:
            atomic_update_users([ctx.author.id, member.id], rob_transaction, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Robbery transaction failed robber=%s victim=%s", ctx.author.id, member.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ نتيجة السرقة. حاول لاحقًا."))
            return
        if "remaining" in outcome:
            await ctx.send(embed=error_embed(
                "ارتاح شوي 😅",
                f"تقدر تحاول تسرق مرة ثانية بعد **{fmt_left(outcome['remaining'])}**."
            ))
            return
        if outcome.get("poor_robber"):
            await ctx.send(embed=error_embed(
                "محفظتك فاضية",
                f"لازم يكون بجيبك على الأقل {currency(min_wallet)} حتى تحاول تسرق."
            ))
            return
        if "poor_victim" in outcome:
            await ctx.send(embed=error_embed(
                "لا يستاهل",
                f"محفظة {member.display_name} فيها {currency(outcome['poor_victim'])} — "
                f"أقل حد للسرقة {currency(min_wallet)}."
            ))
            return
        if outcome.get("invalid") or outcome.get("full"):
            await ctx.send(embed=error_embed("خطأ", "تعذر تنفيذ السرقة على هذا الرصيد."))
            return

        logger.info(
            "rob robber=%s victim=%s outcome=%s amount=%s",
            ctx.author.id, member.id, "success" if outcome["success"] else "failure", outcome["amount"]
        )
        if outcome["success"]:
            embed = success_embed(
                "🚨 سرقة ناجحة!",
                f"خشيت جيب {member.mention} بسرعة وكسبت **{currency(outcome['amount'])}** 🤑"
            )
            embed.add_field(name="محفظتك الآن", value=currency(outcome["wallet"]), inline=True)
        else:
            fine_txt = (
                f"الغرامة ({currency(outcome['amount'])}) رحت للضحية {member.mention} 🤣"
                if outcome["fine_to_victim"] else f"دفعت غرامة {currency(outcome['amount'])} 💸"
            )
            embed = error_embed("🚔 فشلت السرقة!", f"مسكوك على حار {member.mention}!\n{fine_txt}")
            embed.add_field(name="محفظتك الآن", value=currency(outcome["wallet"]), inline=True)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------- إحصائيات
    @commands.hybrid_command(name="robstats", aliases=["سرقاتي"], description="إحصائيات سرقاتك")
    async def robstats(self, ctx: commands.Context):
        settings = load_settings()["economy"]
        user = get_user(ctx.author.id, settings["starting_balance"])
        success = user.get("rob_success", 0)
        fail = user.get("rob_fail", 0)
        total = success + fail
        rate = (success / total * 100) if total else 0

        embed = base_embed("🥷 إحصائيات سرقاتك")
        embed.add_field(name="✅ سرقات ناجحة", value=str(success), inline=True)
        embed.add_field(name="❌ مرات الفشل", value=str(fail), inline=True)
        embed.add_field(name="📊 نسبة النجاح", value=f"{rate:.0f}%", inline=True)
        embed.add_field(name="💰 إجمالي المكاسب", value=currency(user.get("rob_profits", 0)), inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Rob(bot))
