"""
loans.py
---------
نظام القروض بين الأعضاء (Loans):
  - /loan request @عضو مبلغ [أيام] → العضو يوافق أو يرفض بالأزرار
  - عند الموافقة: الفلوس تنتقل فورًا من محفظة المُقرض للمقترض
  - المقترض يرجع المبلغ + فايدة محددة بأمر /loan repay
  - لو تأخر بالسداد → القرض يتعلم "متأخر" ويمنعه من طلب قروض جديدة
  - /loan my → قروضك كدائن ومدين

كل شيء محفوظ في bot/data/loans.json.
"""

import logging
import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.utils.data_manager import (
    atomic_update_loans, atomic_update_users_and_loans, get_user, load_settings, load_loans, now_ts
)
from bot.utils.embeds import base_embed, success_embed, error_embed, currency

logger = logging.getLogger(__name__)


def fmt_left(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h} ساعة و {m} دقيقة"
    return f"{m} دقيقة"


def has_overdue(loans_data: dict, user_id: int) -> bool:
    now = now_ts()
    for loan in loans_data["loans"]:
        if loan["borrower"] != str(user_id):
            continue
        # نشط ولم يستحق بعد، أو متأخر بالفعل — كلاهما يمنع قرضًا جديدًا
        if loan["status"] == "overdue" or (loan["status"] == "active" and loan["due"] < now):
            return True
    return False


def refresh_overdue(loans_data: dict) -> None:
    """يعلم أي قرض نشط فات موعده كـ متأخر."""
    now = now_ts()

    def mark_overdue(data):
        changed = False
        for loan in data["loans"]:
            if not isinstance(loan, dict):
                continue
            try:
                due = int(loan.get("due", 0))
            except (TypeError, ValueError, OverflowError):
                logger.warning("Invalid due timestamp in loan record")
                continue
            if loan.get("status") == "active" and due > 0 and due < now:
                loan["status"] = "overdue"
                changed = True
        return changed

    try:
        current = atomic_update_loans(mark_overdue)
        loans_data.clear()
        loans_data.update(current)
    except (OSError, TypeError, ValueError):
        logger.exception("Could not refresh overdue loan state")


class LoanView(discord.ui.View):
    def __init__(self, cog, loan_id: int, borrower_id: int):
        super().__init__(timeout=86400)
        self.cog = cog
        self.loan_id = loan_id
        self.borrower_id = borrower_id

    @discord.ui.button(label="✅ موافقة", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.borrower_id:
            await interaction.response.send_message("هذا الطلب مو موجّه لك!", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog.fund_loan(interaction.message, interaction, self.loan_id)
        self.stop()

    @discord.ui.button(label="❌ رفض", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.borrower_id:
            await interaction.response.send_message("هذا الطلب مو موجّه لك!", ephemeral=True)
            return
        def decline_loan(data):
            loan = next((l for l in data.get("loans", []) if isinstance(l, dict) and l.get("id") == self.loan_id), None)
            if loan and loan.get("status") == "pending":
                loan["status"] = "declined"
                return True
            return False
        try:
            atomic_update_loans(decline_loan)
        except (OSError, TypeError, ValueError):
            logger.exception("Could not persist declined loan id=%s", self.loan_id)
        embed = error_embed("تم الرفض", f"المقترض رفض القرض #{self.loan_id}.")
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


class Loans(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.transaction_lock = asyncio.Lock()
        self.overdue_check.start()

    def cog_unload(self):
        self.overdue_check.cancel()

    # ------------------------------------------------------------- فحص التأخير الدوري
    @tasks.loop(minutes=10)
    async def overdue_check(self):
        data = load_loans()
        refresh_overdue(data)

    @overdue_check.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- طلب قرض
    @commands.hybrid_group(name="loan", aliases=["قرض", "قروض"], invoke_without_command=True, description="نظام القروض بين الأعضاء")
    async def loan(self, ctx: commands.Context):
        embed = base_embed("💳 نظام القروض")
        prefix = load_settings().get("bot", {}).get("prefix", "!")
        embed.description = (
            f"`{prefix}loan request <عضو> <مبلغ> [أيام]` — اطلب قرض من عضو\n"
            f"`{prefix}loan repay <رقم>` — سدد قرضك (الكل افتراضيًا)\n"
            f"`{prefix}loan my` — قروضي كدائن ومدين"
        )
        await ctx.send(embed=embed)

    @loan.command(name="request", description="طلب قرض من عضو آخر")
    @app_commands.describe(lender="العضو اللي تطلب منه القرض", amount="مبلغ القرض", days="مدة السداد بالأيام (افتراضي 3)")
    async def loan_request(self, ctx: commands.Context, lender: discord.Member, amount: int, days: int = 3):
        settings = load_settings()
        cfg = settings.get("loans", {})
        if not cfg.get("enabled", True):
            await ctx.send(embed=error_embed("معطل", "نظام القروض معطل من الإدارة."))
            return
        if lender.bot or lender.id == ctx.author.id:
            await ctx.send(embed=error_embed("خطأ", "اختر عضو صحيح غيرك."))
            return
        try:
            economy = settings.get("economy", {})
            starting_balance = int(economy["starting_balance"])
            minimum = int(cfg.get("min_amount", 100))
            maximum = int(cfg.get("max_amount", 50000))
            max_duration = int(cfg.get("max_duration_days", 7))
            max_active = int(cfg.get("max_active", 2))
            interest_pct = int(cfg.get("default_interest", 10))
            if (
                minimum < 1 or maximum < minimum or not 1 <= max_duration <= 3650
                or max_active < 0 or not 0 <= interest_pct <= 1000
            ):
                raise ValueError("invalid loan configuration")
            amount = int(amount)
            days = max(1, min(int(days), max_duration))
            if amount <= 0:
                raise ValueError("invalid loan amount")
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.exception("Invalid loan request settings borrower=%s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "إعدادات القرض أو المبلغ غير صالحة."))
            return
        if amount < minimum:
            await ctx.send(embed=error_embed("خطأ", f"أقل مبلغ قرض هو {currency(minimum)}."))
            return
        if amount > maximum:
            await ctx.send(embed=error_embed("خطأ", f"أقصى مبلغ قرض هو {currency(maximum)}."))
            return

        lender_user = get_user(lender.id, starting_balance)
        if lender_user["wallet"] < amount:
            await ctx.send(embed=error_embed("ما يكفيه", f"محفظة {lender.display_name} ما تغطي هذا المبلغ."))
            return

        total = int(amount * (1 + interest_pct / 100))
        outcome = {}

        def create_request(data):
            now = now_ts()
            active = []
            for current in data["loans"]:
                if not isinstance(current, dict) or current.get("borrower") != str(ctx.author.id):
                    continue
                if current.get("status") == "active":
                    try:
                        due = int(current.get("due", 0))
                    except (TypeError, ValueError, OverflowError):
                        outcome["invalid"] = True
                        return False
                    if due > 0 and due < now:
                        current["status"] = "overdue"
                if current.get("status") in ("active", "overdue", "pending"):
                    active.append(current)
            if any(loan.get("status") == "overdue" for loan in active):
                outcome["overdue"] = True
                return False
            if len(active) >= max_active:
                outcome["active"] = len(active)
                return False

            loan_id = data["next_id"]
            data["next_id"] += 1
            loan = {
                "id": loan_id,
                "lender": str(lender.id),
                "borrower": str(ctx.author.id),
                "principal": amount,
                "interest_pct": interest_pct,
                "total": total,
                "paid": 0,
                "status": "pending",
                "created": now,
                "due": now + days * 86400,
            }
            data["loans"].append(loan)
            outcome["loan"] = loan
            return True

        try:
            atomic_update_loans(create_request)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Loan request persistence failed borrower=%s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ طلب القرض."))
            return
        if outcome.get("overdue"):
            await ctx.send(embed=error_embed("عندك قرض متأخر!", "سدد قروضك المتأخرة أول قبل ما تطلب قرض جديد."))
            return
        if "active" in outcome:
            await ctx.send(embed=error_embed("وصلت الحد", f"لازم تسدد قرض من قروضك الحالية ({outcome['active']})."))
            return
        if outcome.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "بيانات قروضك الحالية غير صالحة."))
            return
        loan_id = outcome["loan"]["id"]
        logger.info("loan_request borrower=%s lender=%s loan=%s amount=%s", ctx.author.id, lender.id, loan_id, amount)

        embed = base_embed(f"💳 طلب قرض #{loan_id}")
        embed.description = (
            f"{ctx.author.mention} يطلب قرضًا من {lender.mention}\n\n"
            f"💰 المبلغ: {currency(amount)}\n"
            f"📈 الفايدة: {interest_pct}%\n"
            f"💸 المطلوب سداده: {currency(total)}\n"
            f"⏳ المدة: {days} يوم"
        )
        embed.set_footer(text="للمقترض فقط: وافق بالزرار تحت")
        view = LoanView(self, loan_id, ctx.author.id)
        await ctx.send(embed=embed, view=view)

    # ------------------------------------------------------------- تمويل القرض (عند الموافقة)
    async def fund_loan(self, message: discord.Message, interaction: discord.Interaction, loan_id: int):
        async with self.transaction_lock:
            settings = load_settings().get("economy", {})
            try:
                starting_balance = int(settings["starting_balance"])
                max_wallet = int(settings["max_wallet"])
                if max_wallet < 0:
                    raise ValueError("invalid economy limits")
            except (KeyError, TypeError, ValueError, OverflowError):
                logger.exception("Invalid economy settings while funding loan id=%s", loan_id)
                await interaction.followup.send(embed=error_embed("خطأ", "إعدادات الاقتصاد غير صالحة."))
                return

            pending = next(
                (item for item in load_loans().get("loans", [])
                 if isinstance(item, dict) and item.get("id") == loan_id and item.get("status") == "pending"),
                None,
            )
            if not pending:
                await interaction.followup.send(embed=error_embed("انتهى الطلب", "هذا الطلب تم التعامل معه بالفعل."))
                return
            try:
                user_ids = [int(pending["lender"]), int(pending["borrower"])]
            except (KeyError, TypeError, ValueError, OverflowError):
                logger.exception("Invalid pending loan participants id=%s", loan_id)
                await interaction.followup.send(embed=error_embed("خطأ", "بيانات القرض غير صالحة."))
                return
            if user_ids[0] == user_ids[1]:
                await interaction.followup.send(embed=error_embed("خطأ", "بيانات القرض غير صالحة."))
                return

            outcome = {}

            def settle(accounts, loans_doc):
                current = next(
                    (item for item in loans_doc.get("loans", [])
                     if isinstance(item, dict) and item.get("id") == loan_id),
                    None,
                )
                if not current or current.get("status") != "pending":
                    outcome["stale"] = True
                    return False
                try:
                    lender_id = int(current["lender"])
                    borrower_id = int(current["borrower"])
                    principal = int(current["principal"])
                    if lender_id == borrower_id or principal <= 0:
                        raise ValueError("invalid loan settlement")
                    lender = accounts[str(lender_id)]
                    borrower = accounts[str(borrower_id)]
                    lender_wallet = max(0, int(lender.get("wallet", 0)))
                    borrower_wallet = max(0, int(borrower.get("wallet", 0)))
                except (KeyError, TypeError, ValueError, OverflowError):
                    outcome["invalid"] = True
                    return False

                if lender_wallet < principal:
                    current["status"] = "declined"
                    outcome.update({"insufficient": True, "lender_id": lender_id, "borrower_id": borrower_id})
                    return True
                if borrower_wallet + principal > max_wallet:
                    outcome["limit"] = True
                    return False

                lender["wallet"] = lender_wallet - principal
                borrower["wallet"] = borrower_wallet + principal
                current["status"] = "active"
                outcome.update({
                    "loan": dict(current),
                    "lender_id": lender_id,
                    "borrower_id": borrower_id,
                    "principal": principal,
                })
                return True

            try:
                atomic_update_users_and_loans(user_ids, settle, starting_balance)
            except (OSError, TypeError, ValueError, OverflowError):
                logger.exception("Loan funding transaction failed id=%s", loan_id)
                await interaction.followup.send(embed=error_embed("خطأ", "تعذر حفظ عملية القرض. حاول لاحقًا."))
                return

            if outcome.get("stale"):
                await interaction.followup.send(embed=error_embed("انتهى الطلب", "هذا الطلب تم التعامل معه بالفعل."))
                return
            if outcome.get("insufficient"):
                await interaction.followup.send(embed=error_embed("فشل التحويل", "محفظة المُقرض ما تغطي المبلغ بعد الآن."))
                return
            if outcome.get("limit"):
                await interaction.followup.send(embed=error_embed("تجاوزت الحد", "لا يمكن إضافة مبلغ القرض إلى محفظة المقترض."))
                return
            if outcome.get("invalid") or not outcome.get("loan"):
                await interaction.followup.send(embed=error_embed("خطأ", "بيانات القرض غير صالحة."))
                return

            loan = outcome["loan"]
            lender_id = outcome["lender_id"]
            borrower_id = outcome["borrower_id"]

        lender = self.bot.get_user(lender_id)
        borrower = self.bot.get_user(borrower_id)
        lender_name = lender.name if lender else f"ID {lender_id}"
        borrower_name = borrower.name if borrower else f"ID {borrower_id}"

        embed = success_embed(
            f"✅ القرض #{loan_id} نشط!",
            f"تم تحويل {currency(loan['principal'])} من **{lender_name}** إلى **{borrower_name}**\n"
            f"⏳ آخر موعد للسداد: <t:{loan['due']}:R>\n"
            f"💸 المطلوب سداده: {currency(loan['total'])}"
        )
        try:
            await message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------- السداد
    @loan.command(name="repay", description="سداد قرض")
    @app_commands.describe(loan_id="رقم القرض (اتركه فارغًا لسداد كل قروضك)", full="سدد المبلغ كامل؟ (افتراضي نعم)")
    async def loan_repay(self, ctx: commands.Context, loan_id: int = None, full: bool = True):
        loans_data = load_loans()
        refresh_overdue(loans_data)

        if loan_id is None:
            mine = [l for l in loans_data["loans"] if l["borrower"] == str(ctx.author.id)
                    and l["status"] in ("active", "overdue")]
            if not mine:
                await ctx.send(embed=error_embed("ما عندك قروض", "ما عندك أي قروض تحتاج سداد."))
                return
            total_due = sum(l["total"] - l["paid"] for l in mine)
            await self._repay_amount(ctx, loans_data, mine, total_due)
            return

        loan = next((l for l in loans_data["loans"] if l["id"] == loan_id and l["borrower"] == str(ctx.author.id)), None)
        if not loan or loan["status"] not in ("active", "overdue"):
            await ctx.send(embed=error_embed("خطأ", "ما فيه قرض بهذا الرقم باسمك."))
            return
        await self._repay_amount(ctx, loans_data, [loan], loan["total"] - loan["paid"])

    async def _repay_amount(self, ctx, loans_data, loans, amount: int):
        async with self.transaction_lock:
            try:
                loan_ids = {int(loan["id"]) for loan in loans if isinstance(loan, dict)}
                amount = int(amount)
                if not loan_ids or amount <= 0:
                    raise ValueError("invalid repayment request")
            except (KeyError, TypeError, ValueError, OverflowError):
                logger.exception("Invalid selected loan records user=%s", ctx.author.id)
                await ctx.send(embed=error_embed("خطأ", "بيانات السداد غير صالحة."))
                return

            settings = load_settings().get("economy", {})
            try:
                starting_balance = int(settings["starting_balance"])
                max_wallet = int(settings["max_wallet"])
                if max_wallet < 0:
                    raise ValueError("invalid economy limits")
            except (KeyError, TypeError, ValueError, OverflowError):
                logger.exception("Invalid economy settings user=%s", ctx.author.id)
                await ctx.send(embed=error_embed("خطأ", "إعدادات الاقتصاد غير صالحة."))
                return

            outcome = {}

            def settle(accounts, loans_doc):
                current = [
                    loan for loan in loans_doc.get("loans", [])
                    if isinstance(loan, dict)
                    and loan.get("id") in loan_ids
                    and loan.get("borrower") == str(ctx.author.id)
                    and loan.get("status") in ("active", "overdue")
                ]
                if not current:
                    outcome["missing"] = True
                    return False

                payments = []
                remaining = amount
                for loan in current:
                    try:
                        due_now = int(loan["total"]) - int(loan.get("paid", 0))
                        lender_id = int(loan["lender"])
                    except (KeyError, TypeError, ValueError, OverflowError):
                        outcome["invalid"] = True
                        return False
                    if due_now <= 0:
                        continue
                    pay = min(due_now, remaining)
                    if pay > 0:
                        payments.append((loan, lender_id, pay))
                        remaining -= pay
                    if remaining <= 0:
                        break

                # The command computes a full-debt amount. Refuse a changed debt
                # instead of silently paying a different amount after a race.
                current_due = sum(
                    max(0, int(loan.get("total", 0)) - int(loan.get("paid", 0)))
                    for loan in current
                    if isinstance(loan, dict)
                )
                if current_due != amount or remaining != 0 or not payments:
                    outcome["stale"] = True
                    return False

                try:
                    borrower = accounts[str(ctx.author.id)]
                    borrower_wallet = max(0, int(borrower.get("wallet", 0)))
                    lender_wallets = {
                        lender_id: max(0, int(accounts[str(lender_id)].get("wallet", 0)))
                        for _, lender_id, _ in payments
                    }
                except (KeyError, TypeError, ValueError, OverflowError):
                    outcome["invalid"] = True
                    return False

                if borrower_wallet < amount:
                    outcome["insufficient"] = borrower_wallet
                    return False
                for lender_id, wallet in lender_wallets.items():
                    addition = sum(pay for _, target_id, pay in payments if target_id == lender_id)
                    if wallet + addition > max_wallet:
                        outcome["limit"] = True
                        return False

                borrower["wallet"] = borrower_wallet - amount
                for _, lender_id, pay in payments:
                    accounts[str(lender_id)]["wallet"] = lender_wallets[lender_id] + sum(
                        p for _, target_id, p in payments if target_id == lender_id
                    )
                for loan, _, pay in payments:
                    loan["paid"] = int(loan.get("paid", 0)) + pay
                    if loan["paid"] >= int(loan.get("total", 0)):
                        loan["status"] = "paid"
                outcome["payments"] = [(int(loan["id"]), int(pay)) for loan, _, pay in payments]
                return True

            try:
                lender_ids = {
                    int(item["lender"])
                    for item in loans
                    if isinstance(item, dict) and "lender" in item
                }
                if not lender_ids:
                    raise ValueError("repayment has no lender accounts")
                user_ids = [ctx.author.id, *lender_ids]
                atomic_update_users_and_loans(user_ids, settle, starting_balance)
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                logger.exception("Loan repayment transaction failed user=%s", ctx.author.id)
                await ctx.send(embed=error_embed("خطأ", "تعذر حفظ السداد. حاول لاحقًا."))
                return

            if outcome.get("missing") or outcome.get("stale"):
                await ctx.send(embed=error_embed("انتهت العملية", "حالة القرض تغيّرت قبل السداد. اعرض قروضك من جديد ثم أعد المحاولة."))
                return
            if outcome.get("invalid"):
                await ctx.send(embed=error_embed("خطأ", "بيانات القروض غير صالحة."))
                return
            if outcome.get("insufficient") is not None:
                await ctx.send(embed=error_embed(
                    "رصيد غير كافي",
                    f"تحتاج {currency(amount)} بمحفظتك للسداد، وعندك {currency(outcome['insufficient'])}."
                ))
                return
            if outcome.get("limit"):
                await ctx.send(embed=error_embed("تجاوزت الحد", "لا يمكن إضافة قيمة السداد إلى محفظة المُقرض."))
                return

        logger.info("loan_repay user=%s amount=%s loans=%s", ctx.author.id, amount, len(outcome.get("payments", [])))
        await ctx.send(embed=success_embed("تم السداد", f"سددت {currency(amount)} بنجاح. الله يعافيك! 💰"))

    # ------------------------------------------------------------- قروضي
    @loan.command(name="my", description="عرض قروضي كدائن ومدين")
    async def loan_my(self, ctx: commands.Context):
        loans_data = load_loans()
        refresh_overdue(loans_data)

        debts = [l for l in loans_data["loans"] if l["borrower"] == str(ctx.author.id)
                 and l["status"] in ("active", "overdue", "pending")]
        credits = [l for l in loans_data["loans"] if l["lender"] == str(ctx.author.id)
                   and l["status"] in ("active", "overdue")]

        embed = base_embed(f"💳 قروض {ctx.author.display_name}")

        if debts:
            lines = []
            for l in debts:
                status = {"pending": "🟡 معلق", "active": "🟢 نشط", "overdue": "🔴 متأخر"}.get(l["status"], l["status"])
                lines.append(f"#{l['id']} — باقي عليك {currency(l['total'] - l['paid'])} — {status} — <t:{l['due']}:R>")
            embed.add_field(name="📥 ديونك", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📥 ديونك", value="ما عندك ديون ✨", inline=False)

        if credits:
            lines = []
            for l in credits:
                status = "🟢 نشط" if l["status"] == "active" else "🔴 متأخر"
                lines.append(f"#{l['id']} — لك {currency(l['total'] - l['paid'])} — {status} — <t:{l['due']}:R>")
            embed.add_field(name="📤 لك على الآخرين", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📤 لك على الآخرين", value="ما أقرضت أحد بعد", inline=False)

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Loans(bot))
