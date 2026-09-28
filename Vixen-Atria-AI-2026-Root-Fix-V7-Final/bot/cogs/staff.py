"""
staff.py
---------
أوامر نظام الرتب الإدارية (Dev / Founder / Team):
  - /activate <كود>      : تفعيل رتبة بكود فريق
  - /myrank              : عرض رتبتك وصلاحياتك وحدك اليومي
  - /gencode <رتبة>      : توليد أكواد (يحتاج صلاحية manage_codes)
  - /codes               : عرض الأكواد (يحتاج manage_codes)
  - /revokecode <كود>    : سحب كود (يحتاج manage_codes)
  - /setrank <عضو> <رتبة>: تعيين رتبة مباشرة (يحتاج manage_staff)
  - /stafflist           : قائمة الفريق (يحتاج view_stats)
  - /removestaff <عضو>   : إزالة عضو من الفريق (يحتاج manage_staff)

الآيدي الثابت للمالك يُقرأ من ملف .env (OWNER_ID) ويمتلك كل الصلاحيات تلقائيًا.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils import ranks
from bot.utils.embeds import base_embed, success_embed, error_embed, currency
from bot.utils.data_manager import atomic_update_user, load_settings

logger = logging.getLogger(__name__)


class Staff(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------ تفعيل كود
    @commands.hybrid_command(name="activate", aliases=["تفعيل"], description="تفعيل رتبة إدارية بكود")
    @app_commands.describe(code="كود التفعيل (مثال: CB-DEV-8F3K9A)")
    async def activate(self, ctx: commands.Context, code: str):
        result = ranks.activate_code(code, ctx.author.id)
        if not result["ok"]:
            await ctx.send(embed=error_embed("فشل التفعيل", result["message"]))
            return

        rank = result["rank"]
        perms = ranks.get_permissions(ctx.author.id)
        limit = ranks.get_daily_limit(ctx.author.id)
        limit_txt = "بلا حدود" if limit == 0 else currency(limit) + " / يوم"

        rank_role = {
            "dev": ("🛠️", "#9B59B6"),
            "founder": ("💎", "#F1C40F"),
            "team": ("🛡️", "#3498DB"),
        }.get(rank, ("⭐", "#F5A623"))

        embed = discord.Embed(
            title=f"{rank_role[0]} تم التفعيل بنجاح!",
            description=result["message"],
            color=discord.Color.from_str(rank_role[1])
        )
        embed.add_field(name="🎖️ رتبتك", value=ranks.RANK_NAMES_AR.get(rank, rank), inline=True)
        embed.add_field(name="💳 حد الإضافة اليومي", value=limit_txt, inline=True)
        embed.add_field(name="🔑 صلاحياتك", value=", ".join(f"`{p}`" for p in perms) or "لا شيء", inline=False)
        embed.set_footer(text="الآن تقدر تستخدم أوامر الإدارة وتدخل لوحة التحكم بنفس الكود")
        await ctx.send(embed=embed)
        ranks.log_action(ctx.author.id, str(ctx.author), "activate_rank", f"رتبة: {rank}", "discord")

    # ------------------------------------------------------------ رتبتي
    @commands.hybrid_command(name="myrank", aliases=["رتبتي"], description="عرض رتبتك وصلاحياتك الإدارية")
    async def myrank(self, ctx: commands.Context):
        rank = ranks.get_rank(ctx.author.id)
        if rank is None:
            await ctx.send(embed=error_embed("ما عندك رتبة", "مو عندك رتبة إدارية. جيب كود من الإدارة وفعّله بأمر `/activate`."))
            return

        perms = ranks.get_permissions(ctx.author.id)
        limit = ranks.get_daily_limit(ctx.author.id)
        used = ranks.get_today_usage(ctx.author.id)
        remaining = ranks.remaining_daily_quota(ctx.author.id)

        limit_txt = "♾️ بلا حدود" if limit == 0 else currency(limit)
        remaining_txt = "♾️ بلا حدود" if remaining is None else currency(remaining)

        staff_entry = ranks.list_staff().get(str(ctx.author.id), {})
        activated_at = staff_entry.get("activated_at", 0)
        import time as _t
        date_str = _t.strftime("%Y-%m-%d", _t.localtime(activated_at)) if activated_at else "—"

        emoji = ranks.RANK_EMOJI.get(rank, "⭐")
        embed = base_embed(f"{emoji} رتبتك الإدارية: {ranks.RANK_NAMES_AR.get(rank, rank)}")
        embed.add_field(name="💳 حد الإضافة اليومي", value=limit_txt, inline=True)
        embed.add_field(name="📥 المضاف اليوم", value=currency(used), inline=True)
        embed.add_field(name="📥 المتبقي لليوم", value=remaining_txt, inline=True)
        embed.add_field(name="📅 تاريخ التفعيل", value=date_str, inline=True)
        embed.add_field(name="🔑 الصلاحيات", value=", ".join(f"`{p}`" for p in perms) or "—", inline=False)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------ توليد أكواد
    @commands.hybrid_command(name="gencode", description="توليد كود تفعيل لرتبة إدارية")
    @app_commands.describe(rank="الرتبة", count="عدد الأكواد (1-5)", note="ملاحظة على الكود")
    @app_commands.choices(rank=[
        app_commands.Choice(name="🛡️ Team (فريق)", value="team"),
        app_commands.Choice(name="💎 Founder (فاوندر)", value="founder"),
        app_commands.Choice(name="🛠️ Dev (ديفيلوبر)", value="dev"),
    ])
    @ranks.rank_check("manage_codes")
    async def gencode(self, ctx: commands.Context, rank: app_commands.Choice[str], count: int = 1, note: str = ""):
        actor_rank = ranks.get_rank(ctx.author.id) or ""
        actor_level = 4 if actor_rank == "owner" else ranks.RANK_LEVELS.get(actor_rank, 0)
        if actor_rank != "owner" and ranks.RANK_LEVELS.get(rank.value, 0) >= actor_level:
            await ctx.send(embed=error_embed("Hierarchy", "ما تقدر تولد كود لرتبة مساوية أو أعلى من رتبتك."))
            return
        count = max(1, min(count, 5))
        codes = []
        for _ in range(count):
            code = ranks.generate_code(rank.value, ctx.author.id, note)
            if code:
                codes.append(code)

        embed = discord.Embed(
            title="🔑 أكواد تفعيل جديدة",
            description=f"رتبة: **{ranks.RANK_NAMES_AR.get(rank.value, rank.value)}**\n"
                        f"الأكواد تظهر **لك فقط** — شاركها مع من تثق به.",
            color=discord.Color.gold()
        )
        embed.add_field(name="الأكواد", value="\n".join(f"`{c}`" for c in codes), inline=False)
        if note:
            embed.add_field(name="📝 ملاحظة", value=note, inline=False)
        embed.set_footer(text="أكواد تستخدم مرة واحدة لعضو واحد")
        await ctx.send(embed=embed, ephemeral=True)
        ranks.log_action(ctx.author.id, str(ctx.author), "generate_codes", f"{count} كود رتبة {rank.value}", "discord")

    # ------------------------------------------------------------ عرض الأكواد
    @commands.hybrid_command(name="codes", description="عرض أكواد التفعيل (إدارة)")
    @ranks.rank_check("manage_codes")
    async def codes(self, ctx: commands.Context):
        all_codes = ranks.list_codes()
        if not all_codes:
            await ctx.send(embed=error_embed("لا توجد أكواد", "ما تم توليد أي كود بعد. استخدم `/gencode`."))
            return

        lines = []
        for code, entry in list(all_codes.items())[:15]:
            status = "⛔ مسحوب" if entry.get("revoked") else ("✅ مستخدم" if entry.get("used_by") else "🟢 متاح")
            lines.append(f"`{code}` — {ranks.RANK_NAMES_AR.get(entry['rank'], entry['rank'])} — {status}")

        embed = base_embed("🔑 أكواد التفعيل")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"إجمالي الأكواد: {len(all_codes)} — الشاشة تعرض أول 15")
        await ctx.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ سحب كود
    @commands.hybrid_command(name="revokecode", description="سحب كود تفعيل (إدارة)")
    @app_commands.describe(code="الكود المراد سحبه")
    @ranks.rank_check("manage_codes")
    async def revokecode(self, ctx: commands.Context, code: str):
        code = code.strip().upper()
        entry = ranks.list_codes().get(code)
        actor_rank = ranks.get_rank(ctx.author.id) or ""
        actor_level = 4 if actor_rank == "owner" else ranks.RANK_LEVELS.get(actor_rank, 0)
        target_level = ranks.RANK_LEVELS.get(entry.get("rank", ""), 0) if isinstance(entry, dict) else 0
        if entry and actor_rank != "owner" and target_level >= actor_level:
            await ctx.send(embed=error_embed("Hierarchy", "ما تقدر تسحب كود لرتبة مساوية أو أعلى من رتبتك."))
            return
        if ranks.revoke_code(code):
            await ctx.send(embed=success_embed("تم السحب", f"الكود `{code}` تم سحبه ولن يعمل بعد الآن."))
            ranks.log_action(ctx.author.id, str(ctx.author), "revoke_code", f"سحب {code}", "discord")
        else:
            await ctx.send(embed=error_embed("خطأ", "الكود غير موجود."))

    # ------------------------------------------------------------ تعيين رتبة مباشرة
    @commands.hybrid_command(name="setrank", description="تعيين رتبة إدارية لعضو مباشرة بدون كود")
    @app_commands.describe(member="العضو", rank="الرتبة")
    @app_commands.choices(rank=[
        app_commands.Choice(name="🛡️ Team (فريق)", value="team"),
        app_commands.Choice(name="💎 Founder (فاوندر)", value="founder"),
        app_commands.Choice(name="🛠️ Dev (ديفيلوبر)", value="dev"),
    ])
    @ranks.rank_check("manage_staff")
    async def setrank(self, ctx: commands.Context, member: discord.Member, rank: app_commands.Choice[str]):
        if ranks.is_owner(member.id):
            await ctx.send(embed=error_embed("خطأ", "هذا العضو المالك نفسه — رتبته ثابتة ما تتغير."))
            return
        if member.bot:
            await ctx.send(embed=error_embed("خطأ", "ما تقدر تعطي رتبة لبوت."))
            return
        actor_rank = ranks.get_rank(ctx.author.id) or ""
        actor_level = 4 if actor_rank == "owner" else ranks.RANK_LEVELS.get(actor_rank, 0)
        target_level = ranks.RANK_LEVELS.get(rank.value, 0)
        current_target = ranks.get_rank(member.id) or ""
        if target_level >= actor_level:
            await ctx.send(embed=error_embed("Hierarchy", "ما تقدر تمنح رتبة مساوية أو أعلى من رتبتك."))
            return
        if current_target and ranks.RANK_LEVELS.get(current_target, 0) >= actor_level:
            await ctx.send(embed=error_embed("Hierarchy", "ما تقدر تعدّل رتبة عضو أعلى منك."))
            return
        if ranks.set_rank(member.id, rank.value, ctx.author.id):
            await ctx.send(embed=success_embed(
                "تم التعيين",
                f"أعطيت {member.mention} رتبة **{ranks.RANK_NAMES_AR.get(rank.value)}** {ranks.RANK_EMOJI.get(rank.value)}"
            ))
            ranks.log_action(ctx.author.id, str(ctx.author), "set_rank",
                             f"منح {member} رتبة {rank.value}", "discord")
        else:
            await ctx.send(embed=error_embed("خطأ", "رتبة غير صالحة."))

    # ------------------------------------------------------------ قائمة الفريق
    @commands.hybrid_command(name="stafflist", aliases=["الفريق"], description="عرض قائمة الفريق الإداري")
    @ranks.rank_check("view_stats")
    async def stafflist(self, ctx: commands.Context):
        staff = ranks.list_staff()
        owner_id = ranks.get_owner_id()

        lines = []
        if owner_id:
            owner = self.bot.get_user(owner_id)
            name = owner.name if owner else "المالك"
            lines.append(f"👑 **{name}** (`{owner_id}`) — المالك")

        for uid, entry in staff.items():
            member = self.bot.get_user(int(uid))
            name = member.name if member else f"ID {uid}"
            emoji = ranks.RANK_EMOJI.get(entry.get("rank", ""), "⭐")
            rank_name = ranks.RANK_NAMES_AR.get(entry.get("rank", ""), entry.get("rank", ""))
            lines.append(f"{emoji} **{name}** (`{uid}`) — {rank_name}")

        if len(lines) <= (1 if owner_id else 0):
            embed = error_embed("الفريق فاضي", "ما فيه أحد مفعل رتبة بعد. استخدم `/gencode` أو `/setrank`.")
        else:
            embed = base_embed(f"👥 الفريق الإداري ({len(staff) + (1 if owner_id else 0)})")
            embed.description = "\n".join(lines[:20])
        await ctx.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ إزالة عضو
    @commands.hybrid_command(name="removestaff", description="إزالة رتبة عضو من الفريق")
    @app_commands.describe(member="العضو المراد إزالته")
    @ranks.rank_check("manage_staff")
    async def removestaff(self, ctx: commands.Context, member: discord.Member):
        actor_rank = ranks.get_rank(ctx.author.id) or ""
        actor_level = 4 if actor_rank == "owner" else ranks.RANK_LEVELS.get(actor_rank, 0)
        target_rank = ranks.get_rank(member.id) or ""
        if target_rank == "owner" or ranks.RANK_LEVELS.get(target_rank, 0) >= actor_level:
            await ctx.send(embed=error_embed("Hierarchy", "ما تقدر تزيل رتبة مساوية أو أعلى من رتبتك."))
            return
        if ranks.remove_staff(member.id):
            await ctx.send(embed=success_embed("تمت الإزالة", f"تم إزالة رتبة {member.mention} من الفريق."))
            ranks.log_action(ctx.author.id, str(ctx.author), "remove_staff", f"إزالة {member}", "discord")
        else:
            await ctx.send(embed=error_embed("خطأ", "هذا العضو ما عنده رتبة أو هو المالك."))

    # ------------------------------------------------------------ إضافة فلوس (برتبة الإدارة)
    @commands.hybrid_command(name="addmoney", description="إضافة فلوس لعضو (إدارة/رتبة)")
    @app_commands.describe(member="العضو (اكتب نفسك لتضيف لنفسك)", amount="المبلغ", to_bank="أضف للبنك بدل المحفظة؟")
    @ranks.rank_check("add_money")
    async def addmoney_rank(self, ctx: commands.Context, member: discord.Member, amount: int, to_bank: bool = False):
        if amount <= 0:
            await ctx.send(embed=error_embed("خطأ", "المبلغ لازم يكون أكبر من صفر."))
            return

        limit = ranks.get_daily_limit(ctx.author.id)
        if not ranks.reserve_daily_quota(ctx.author.id, amount, limit):
            remaining = ranks.remaining_daily_quota(ctx.author.id)
            await ctx.send(embed=error_embed(
                "تجاوزت حدك اليومي",
                f"المتبقي لك اليوم: {currency(remaining or 0)} فقط.\nاطلب ترقية رتبتك من الإدارة."
            ))
            return

        settings = load_settings().get("economy", {})
        outcome = {}

        def add_funds(user):
            key = "bank" if to_bank else "wallet"
            cap_key = "max_bank" if to_bank else "max_wallet"
            try:
                balance = max(0, int(user.get(key, 0)))
                cap = int(settings[cap_key])
                earned = max(0, int(user.get("total_earned", 0)))
                if cap < 0:
                    raise ValueError("invalid balance cap")
            except (KeyError, TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            credited = min(amount, max(0, cap - balance))
            if credited <= 0:
                outcome["full"] = True
                return False
            user[key] = balance + credited
            if not to_bank:
                user["total_earned"] = earned + credited
            outcome.update({"balance": balance + credited, "credited": credited})
            return True

        try:
            atomic_update_user(member.id, add_funds, int(settings["starting_balance"]))
        except (OSError, TypeError, ValueError, OverflowError):
            ranks.adjust_daily_usage(ctx.author.id, -amount)
            logger.exception("Staff money addition failed actor=%s target=%s", ctx.author.id, member.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ الإضافة المالية. حاول لاحقًا."))
            return
        if outcome.get("invalid") or outcome.get("full"):
            ranks.adjust_daily_usage(ctx.author.id, -amount)
            await ctx.send(embed=error_embed("تجاوزت الحد", "رصيد العضو وصل إلى الحد الأقصى."))
            return
        if outcome["credited"] < amount:
            ranks.adjust_daily_usage(ctx.author.id, outcome["credited"] - amount)

        target_txt = "لنفسك" if member.id == ctx.author.id else f"لـ {member.mention}"
        await ctx.send(embed=success_embed(
            "تمت الإضافة",
            f"أضفت {currency(outcome['credited'])} {target_txt}\nالرصيد الجديد: {currency(outcome['balance'])}"
        ))
        ranks.log_action(ctx.author.id, str(ctx.author), "add_money",
                         f"أضاف {outcome['credited']} لـ {member} ({'بنك' if to_bank else 'محفظة'})", "discord")

    # ------------------------------------------------------------ سحب فلوس (برتبة الإدارة)
    @commands.hybrid_command(name="removemoney", description="سحب فلوس من عضو (إدارة/رتبة)")
    @app_commands.describe(member="العضو", amount="المبلغ", from_bank="اسحب من البنك بدل المحفظة؟")
    @ranks.rank_check("remove_money")
    async def removemoney_rank(self, ctx: commands.Context, member: discord.Member, amount: int, from_bank: bool = False):
        if amount <= 0:
            await ctx.send(embed=error_embed("خطأ", "المبلغ لازم يكون أكبر من صفر."))
            return
        settings = load_settings().get("economy", {})
        outcome = {}

        def remove_funds(user):
            key = "bank" if from_bank else "wallet"
            try:
                balance = max(0, int(user.get(key, 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            removed = min(amount, balance)
            user[key] = balance - removed
            outcome.update({"balance": balance - removed, "removed": removed})
            return True

        try:
            atomic_update_user(member.id, remove_funds, int(settings["starting_balance"]))
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Staff money removal failed actor=%s target=%s", ctx.author.id, member.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ السحب المالي. حاول لاحقًا."))
            return
        if outcome.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "رصيد العضو غير صالح."))
            return
        await ctx.send(embed=success_embed(
            "تم السحب",
            f"سحبت {currency(outcome['removed'])} من {member.mention}\nرصيده الجديد: {currency(outcome['balance'])}"
        ))
        ranks.log_action(ctx.author.id, str(ctx.author), "remove_money", f"سحب {outcome['removed']} من {member}", "discord")

    # ------------------------------------------------------------ تصفير (برتبة الإدارة)
    @commands.hybrid_command(name="resetbalance", description="تصفير رصيد عضو (إدارة/رتبة)")
    @app_commands.describe(member="العضو")
    @ranks.rank_check("reset_user")
    async def resetbalance_rank(self, ctx: commands.Context, member: discord.Member):
        def reset_balances(user):
            user["wallet"] = 0
            user["bank"] = 0
            return True

        settings = load_settings().get("economy", {})
        try:
            atomic_update_user(member.id, reset_balances, int(settings.get("starting_balance", 100)))
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Staff balance reset failed actor=%s target=%s", ctx.author.id, member.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ تصفير الرصيد. حاول لاحقًا."))
            return
        await ctx.send(embed=success_embed("تم التصفير", f"تم تصفير رصيد {member.mention} بالكامل."))
        ranks.log_action(ctx.author.id, str(ctx.author), "reset_user", f"تصفير رصيد {member}", "discord")


async def setup(bot: commands.Bot):
    await bot.add_cog(Staff(bot))
