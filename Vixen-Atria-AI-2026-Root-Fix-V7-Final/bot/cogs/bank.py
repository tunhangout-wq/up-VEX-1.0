"""
bank.py
--------
أوامر البنك: إيداع، سحب، وسحب الفايدة اليومية /interest.

الفايدة البنكية: كل يوم تقدر تسحب فايدة على رصيد بنكك (نسبة مئوية
تُضبط من لوحة التحكم settings.bank_interest) — كافئة للادخار!
"""

import logging
import math

from discord import app_commands
from discord.ext import commands

from bot.utils.data_manager import atomic_update_user, load_settings, now_ts
from bot.utils.embeds import success_embed, error_embed, currency

logger = logging.getLogger(__name__)


class Bank(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="deposit", aliases=["dep", "ايداع"], description="أودع فلوس من محفظتك للبنك")
    @app_commands.describe(amount="المبلغ أو اكتب all للكل")
    async def deposit(self, ctx: commands.Context, amount: str):
        settings = load_settings().get("economy", {})
        try:
            max_bank = int(settings["max_bank"])
            starting_balance = int(settings["starting_balance"])
            all_amount = isinstance(amount, str) and amount.lower() in ("all", "الكل", "كل")
            value = None if all_amount else int(amount)
            if max_bank < 0:
                raise ValueError("invalid bank limit")
        except (KeyError, TypeError, ValueError, OverflowError):
            await ctx.send(embed=error_embed("خطأ", "اكتب مبلغاً صالحاً أو `all`."))
            return

        result = {}

        def deposit_amount(user):
            try:
                wallet = max(0, int(user.get("wallet", 0)))
                bank = max(0, int(user.get("bank", 0)))
            except (TypeError, ValueError, OverflowError):
                result["invalid"] = True
                return False
            deposit = wallet if all_amount else value
            if deposit is None or deposit <= 0:
                result["invalid"] = True
                return False
            if wallet < deposit:
                result["insufficient"] = True
                return False
            if bank + deposit > max_bank:
                result["limit"] = True
                return False
            user.update({"wallet": wallet - deposit, "bank": bank + deposit})
            result["amount"] = deposit
            return True

        try:
            atomic_update_user(ctx.author.id, deposit_amount, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Deposit failed for user %s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ الإيداع. حاول لاحقًا."))
            return
        if result.get("insufficient"):
            await ctx.send(embed=error_embed("رصيد غير كافي", "ما عندك فلوس كافية بالمحفظة."))
            return
        if result.get("limit"):
            await ctx.send(embed=error_embed("تجاوزت الحد", f"أقصى رصيد بالبنك هو {currency(max_bank)}"))
            return
        if result.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "المبلغ لازم يكون أكبر من صفر."))
            return
        logger.info("deposit user=%s amount=%s", ctx.author.id, result["amount"])
        await ctx.send(embed=success_embed("تم الإيداع", f"أودعت {currency(result['amount'])} بالبنك."))

    @commands.hybrid_command(name="withdraw", aliases=["with", "سحب"], description="اسحب فلوس من البنك لمحفظتك")
    @app_commands.describe(amount="المبلغ أو اكتب all للكل")
    async def withdraw(self, ctx: commands.Context, amount: str):
        settings = load_settings().get("economy", {})
        try:
            max_wallet = int(settings["max_wallet"])
            starting_balance = int(settings["starting_balance"])
            all_amount = isinstance(amount, str) and amount.lower() in ("all", "الكل", "كل")
            value = None if all_amount else int(amount)
            if max_wallet < 0:
                raise ValueError("invalid wallet limit")
        except (KeyError, TypeError, ValueError, OverflowError):
            await ctx.send(embed=error_embed("خطأ", "اكتب مبلغاً صالحاً أو `all`."))
            return

        result = {}

        def withdraw_amount(user):
            try:
                wallet = max(0, int(user.get("wallet", 0)))
                bank = max(0, int(user.get("bank", 0)))
            except (TypeError, ValueError, OverflowError):
                result["invalid"] = True
                return False
            withdrawal = bank if all_amount else value
            if withdrawal is None or withdrawal <= 0:
                result["invalid"] = True
                return False
            if bank < withdrawal:
                result["insufficient"] = True
                return False
            if wallet + withdrawal > max_wallet:
                result["limit"] = True
                return False
            user.update({"wallet": wallet + withdrawal, "bank": bank - withdrawal})
            result["amount"] = withdrawal
            return True

        try:
            atomic_update_user(ctx.author.id, withdraw_amount, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Withdrawal failed for user %s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ السحب. حاول لاحقًا."))
            return
        if result.get("insufficient"):
            await ctx.send(embed=error_embed("رصيد غير كافي", "ما عندك فلوس كافية بالبنك."))
            return
        if result.get("limit"):
            await ctx.send(embed=error_embed("تجاوزت الحد", f"أقصى رصيد بالمحفظة هو {currency(max_wallet)}"))
            return
        if result.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "المبلغ لازم يكون أكبر من صفر."))
            return
        logger.info("withdraw user=%s amount=%s", ctx.author.id, result["amount"])
        await ctx.send(embed=success_embed("تم السحب", f"سحبت {currency(result['amount'])} من البنك."))

    # ------------------------------------------------------------- فايدة يومية
    @commands.hybrid_command(name="interest", aliases=["فايدة"], description="اسحب فايدتك اليومية على رصيد البنك")
    async def interest(self, ctx: commands.Context):
        settings_all = load_settings()
        cfg = settings_all.get("bank_interest", {})
        if not cfg.get("enabled", True):
            await ctx.send(embed=error_embed("معطل", "نظام الفايدة معطل من الإدارة."))
            return
        settings = settings_all.get("economy", {})
        now = now_ts()
        cooldown = 24 * 3600
        try:
            rate = float(cfg.get("daily_rate", 2.0))
            max_interest = int(cfg.get("max_interest", 5000))
            min_bank = int(cfg.get("min_bank", 100))
            max_bank = int(settings["max_bank"])
            starting_balance = int(settings["starting_balance"])
            if not math.isfinite(rate) or rate < 0 or min(max_interest, min_bank, max_bank) < 0:
                raise ValueError("invalid bank interest settings")
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.exception("Invalid bank interest settings")
            await ctx.send(embed=error_embed("خطأ", "إعدادات الفائدة غير صالحة."))
            return

        result = {}

        def claim_interest(user):
            try:
                bank_balance = max(0, int(user.get("bank", 0)))
                last_interest = int(user.get("last_interest", 0))
                total_interest = max(0, int(user.get("interest_total", 0)))
            except (TypeError, ValueError, OverflowError):
                result["invalid"] = True
                return False
            if last_interest < 0 or last_interest > now:
                last_interest = 0
            if now - last_interest < cooldown:
                result["remaining"] = cooldown - (now - last_interest)
                return False
            if bank_balance < min_bank:
                result["below_minimum"] = True
                return False
            gain = min(
                int(bank_balance * rate / 100),
                max_interest,
                max(0, max_bank - bank_balance),
            )
            if gain <= 0:
                result["no_gain"] = True
                return False
            user.update({
                "bank": bank_balance + gain,
                "last_interest": now,
                "interest_total": total_interest + gain,
            })
            result.update({
                "bank": bank_balance,
                "gain": gain,
                "new_bank": bank_balance + gain,
                "total_interest": total_interest + gain,
            })
            return True

        try:
            atomic_update_user(ctx.author.id, claim_interest, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Interest claim failed for user %s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ الفائدة. حاول لاحقًا."))
            return
        if "remaining" in result:
            h, rem = divmod(result["remaining"], 3600)
            m, _ = divmod(rem, 60)
            await ctx.send(embed=error_embed("لسا بدري!", f"فايدتك القادمة بعد **{h} ساعة و {m} دقيقة**."))
            return
        if result.get("below_minimum"):
            await ctx.send(embed=error_embed(
                "رصيد البنك ما يكفي",
                f"لازم يكون ببنكك على الأقل {currency(min_bank)} عشان تحسب فايدة.\n"
                "ادّخر بأمر `/deposit` وارجع لك!"
            ))
            return
        if result.get("invalid") or result.get("no_gain"):
            await ctx.send(embed=error_embed("خطأ", "لا يمكن إضافة فائدة إلى رصيدك الحالي."))
            return
        logger.info("interest user=%s amount=%s", ctx.author.id, result["gain"])

        embed = success_embed(
            "🏦 فايدة بنكية!",
            f"رصيد بنكك {currency(result['bank'])} × {rate}% = **{currency(result['gain'])}** أضافت لبنكك!"
        )
        embed.add_field(name="🏦 رصيد البنك الجديد", value=currency(result["new_bank"]), inline=True)
        embed.add_field(name="💰 إجمالي فوايدك", value=currency(result["total_interest"]), inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Bank(bot))
