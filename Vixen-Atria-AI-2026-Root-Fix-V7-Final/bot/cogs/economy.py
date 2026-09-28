"""
economy.py
-----------
الأوامر الأساسية لنظام العملات: الرصيد، اليومي، العمل، التحويل.
"""

import random
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.data_manager import (
    get_user, atomic_update_user, atomic_update_users, load_settings, now_ts
)
from bot.utils.embeds import base_embed, success_embed, error_embed, currency

logger = logging.getLogger(__name__)


def fmt_time(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} ساعة")
    if m:
        parts.append(f"{m} دقيقة")
    if s and not h:
        parts.append(f"{s} ثانية")
    return " و ".join(parts) if parts else "أقل من ثانية"


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------------------------------------------------------- رصيد
    @commands.hybrid_command(name="balance", aliases=["bal", "رصيد"], description="عرض رصيدك أو رصيد عضو آخر")
    @app_commands.describe(member="العضو اللي تبي تشوف رصيده")
    async def balance(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        settings = load_settings()
        user = get_user(member.id, settings["economy"]["starting_balance"])

        embed = base_embed(f"محفظة {member.display_name}")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="💵 المحفظة", value=currency(user["wallet"]), inline=True)
        embed.add_field(name="🏦 البنك", value=currency(user["bank"]), inline=True)
        embed.add_field(name="💰 الإجمالي", value=currency(user["wallet"] + user["bank"]), inline=True)
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------- يومي
    @commands.hybrid_command(name="daily", aliases=["يومي"], description="احصل على مكافأتك اليومية")
    async def daily(self, ctx: commands.Context):
        settings = load_settings().get("economy", {})
        now = now_ts()
        cooldown = 24 * 3600
        try:
            daily_min = int(settings["daily_min"])
            daily_max = int(settings["daily_max"])
            streak_bonus = int(settings["daily_streak_bonus"])
            streak_cap = int(settings["daily_streak_cap"])
            max_wallet = int(settings["max_wallet"])
            starting_balance = int(settings["starting_balance"])
            if min(daily_min, daily_max, streak_bonus, streak_cap, max_wallet) < 0 or daily_min > daily_max:
                raise ValueError("invalid daily reward settings")
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.exception("Invalid daily economy settings")
            await ctx.send(embed=error_embed("خطأ", "تعذر تنفيذ المكافأة الآن."))
            return

        outcome = {}

        def claim(user):
            try:
                last_daily = int(user.get("last_daily", 0))
            except (TypeError, ValueError, OverflowError):
                last_daily = 0
            if last_daily < 0 or last_daily > now:
                last_daily = 0
            elapsed = now - last_daily
            if elapsed < cooldown:
                outcome["remaining"] = cooldown - elapsed
                return False

            try:
                streak = max(0, int(user.get("daily_streak", 0)))
                wallet = max(0, int(user.get("wallet", 0)))
                earned = max(0, int(user.get("total_earned", 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            streak = streak + 1 if elapsed <= cooldown * 2 else 1
            total = random.randint(daily_min, daily_max)
            bonus = min(streak * streak_bonus, streak_cap)
            amount = min(total + bonus, max(0, max_wallet - wallet))
            if amount <= 0:
                outcome["full"] = True
                return False
            user.update({
                "wallet": wallet + amount,
                "total_earned": earned + amount,
                "last_daily": now,
                "daily_streak": streak,
            })
            outcome.update({"amount": amount, "bonus": min(bonus, amount), "streak": streak})
            return True

        try:
            atomic_update_user(ctx.author.id, claim, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Daily reward failed for user %s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ المكافأة. حاول لاحقًا."))
            return
        if "remaining" in outcome:
            await ctx.send(embed=error_embed(
                "لسا مو وقتها!",
                f"لازم تنتظر **{fmt_time(outcome['remaining'])}** عشان تقدر تاخذ يوميتك مرة ثانية."
            ))
            return
        if outcome.get("full") or outcome.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "تعذر إضافة المكافأة إلى محفظتك."))
            return

        embed = success_embed("مكافأة يومية!", f"حصلت على {currency(outcome['amount'])}")
        embed.add_field(name="🔥 سلسلة الأيام", value=f"{outcome['streak']} يوم متواصل", inline=True)
        embed.add_field(name="🎁 بونص السلسلة", value=currency(outcome["bonus"]), inline=True)
        logger.info("daily user=%s amount=%s", ctx.author.id, outcome["amount"])
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------- عمل
    @commands.hybrid_command(name="work", aliases=["عمل"], description="اعمل واكسب فلوس")
    async def work(self, ctx: commands.Context):
        settings = load_settings().get("economy", {})
        now = now_ts()
        try:
            cooldown = int(settings["work_cooldown_minutes"]) * 60
            work_min = int(settings["work_min"])
            work_max = int(settings["work_max"])
            max_wallet = int(settings["max_wallet"])
            starting_balance = int(settings["starting_balance"])
            if min(cooldown, work_min, work_max, max_wallet) < 0 or work_min > work_max:
                raise ValueError("invalid work settings")
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.exception("Invalid work economy settings")
            await ctx.send(embed=error_embed("خطأ", "تعذر تنفيذ العمل الآن."))
            return
        jobs = [
            "دليفري", "برمجة موقع", "تصميم شعار", "تعليم طالب",
            "تصوير مناسبة", "كتابة مقال", "إصلاح جهاز", "بيع بسطة"
        ]
        outcome = {}

        def claim(user):
            try:
                last_work = int(user.get("last_work", 0))
                wallet = max(0, int(user.get("wallet", 0)))
                earned = max(0, int(user.get("total_earned", 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if last_work < 0 or last_work > now:
                last_work = 0
            elapsed = now - last_work
            if elapsed < cooldown:
                outcome["remaining"] = cooldown - elapsed
                return False
            amount = min(random.randint(work_min, work_max), max(0, max_wallet - wallet))
            if amount <= 0:
                outcome["full"] = True
                return False
            user.update({
                "wallet": wallet + amount,
                "total_earned": earned + amount,
                "last_work": now,
            })
            outcome.update({"amount": amount, "job": random.choice(jobs)})
            return True

        try:
            atomic_update_user(ctx.author.id, claim, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Work reward failed for user %s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ راتب العمل. حاول لاحقًا."))
            return
        if "remaining" in outcome:
            await ctx.send(embed=error_embed(
                "لسا متعب!",
                f"لازم تستريح **{fmt_time(outcome['remaining'])}** قبل ما تشتغل مرة ثانية."
            ))
            return
        if outcome.get("full") or outcome.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "تعذر إضافة الراتب إلى محفظتك."))
            return

        embed = success_embed(
            "شغل زين!",
            f"اشتغلت **{outcome['job']}** وكسبت {currency(outcome['amount'])}"
        )
        logger.info("work user=%s amount=%s", ctx.author.id, outcome["amount"])
        await ctx.send(embed=embed)

    # ------------------------------------------------------------- تحويل
    @commands.hybrid_command(name="transfer", aliases=["pay", "تحويل"], description="حول فلوس لعضو آخر")
    @app_commands.describe(member="العضو المستلم", amount="المبلغ")
    async def transfer(self, ctx: commands.Context, member: discord.Member, amount: int):
        settings = load_settings()["economy"]

        if member.bot:
            await ctx.send(embed=error_embed("خطأ", "ما تقدر تحول فلوس لبوت."))
            return
        if member.id == ctx.author.id:
            await ctx.send(embed=error_embed("خطأ", "ما تقدر تحول لنفسك."))
            return
        if amount < settings["transfer_min_amount"]:
            await ctx.send(embed=error_embed("خطأ", f"أقل مبلغ للتحويل هو {currency(settings['transfer_min_amount'])}"))
            return

        try:
            tax_percent = int(settings["transfer_tax_percent"])
            max_wallet = int(settings["max_wallet"])
            starting_balance = int(settings["starting_balance"])
            if not 0 <= tax_percent <= 100 or max_wallet < 0 or amount <= 0:
                raise ValueError("invalid transfer settings or amount")
        except (KeyError, TypeError, ValueError, OverflowError):
            await ctx.send(embed=error_embed("خطأ", "المبلغ أو إعدادات التحويل غير صالحة."))
            return

        tax = int(amount * tax_percent / 100)
        received = amount - tax
        outcome = {}

        def transfer(users):
            try:
                sender_wallet = max(0, int(users[str(ctx.author.id)].get("wallet", 0)))
                receiver_wallet = max(0, int(users[str(member.id)].get("wallet", 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if sender_wallet < amount:
                outcome["insufficient"] = True
                return False
            if receiver_wallet + received > max_wallet:
                outcome["recipient_limit"] = True
                return False
            users[str(ctx.author.id)]["wallet"] = sender_wallet - amount
            users[str(member.id)]["wallet"] = receiver_wallet + received
            outcome.update({"sender": sender_wallet - amount, "receiver": receiver_wallet + received})
            return True

        try:
            atomic_update_users([ctx.author.id, member.id], transfer, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Transfer failed sender=%s recipient=%s", ctx.author.id, member.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ التحويل. حاول لاحقًا."))
            return
        if outcome.get("insufficient"):
            await ctx.send(embed=error_embed("رصيد غير كافي", "ما عندك فلوس كافية بالمحفظة لهذا التحويل."))
            return
        if outcome.get("recipient_limit") or outcome.get("invalid"):
            await ctx.send(embed=error_embed("تجاوزت الحد", "لا يمكن إتمام التحويل مع رصيد المستلم الحالي."))
            return

        embed = success_embed(
            "تم التحويل",
            f"حولت {currency(amount)} إلى {member.mention}\n"
            f"💸 ضريبة التحويل ({settings['transfer_tax_percent']}%): {currency(tax)}\n"
            f"📥 استلم {member.display_name}: {currency(received)}"
        )
        logger.info("transfer sender=%s recipient=%s amount=%s", ctx.author.id, member.id, amount)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
