"""
career.py
----------
نظام الوظائف والبزنس (Career / Business):
  - سلم وظائف بمستويات ترقية: توصيل → كاشير → مدير → المدير التنفيذي...
  - كل وظيفة لها راتب ومتطلبات مستوى (XP Level)
  - كل ما تشتغل تاخذ راتب + نقاط أقدمية → تترقى لوظيفة أعلى
  - البزنس: تشتري مشروعك الخاص → أرباح يومية تجمعها بأمر collect
           وتقدر تطوره لزيادة الأرباح

الوظائف والأرقام كلها من لوحة التحكم: settings.career
"""

import random
import logging
import math

from discord import app_commands
from discord.ext import commands

from bot.utils.data_manager import atomic_update_user, get_user, load_settings, now_ts
from bot.utils.embeds import base_embed, success_embed, error_embed, currency

logger = logging.getLogger(__name__)


def fmt_left(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h} ساعة و {m} دقيقة"
    return f"{m} دقيقة"


class Career(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _get_job(self, job_id: str):
        cfg = load_settings().get("career", {})
        return next((j for j in cfg.get("jobs", []) if j["id"] == job_id), None)

    # ------------------------------------------------------------- قائمة الوظائف
    @commands.hybrid_group(name="career", aliases=["وظيفة", "وظائف"], invoke_without_command=True,
                           description="نظام الوظائف والبزنس")
    async def career(self, ctx: commands.Context):
        prefix = load_settings().get("bot", {}).get("prefix", "!")
        embed = base_embed("💼 نظام الوظائف والبزنس")
        embed.description = (
            f"`{prefix}career list` — الوظائف المتاحة\n"
            f"`{prefix}career apply <id>` — تقدم لوظيفة\n"
            f"`{prefix}career work` — اشتغل واحصل على راتبك\n"
            f"`{prefix}career promote` — اطلب ترقية\n"
            f"`{prefix}career quit` — استقل من وظيفتك\n"
            f"`{prefix}career status` — وضعك الوظيفي\n"
            f"`{prefix}career business` — نظام البزنس الخاص"
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------- عرض الوظائف
    @career.command(name="list", description="الوظائف المتاحة")
    async def career_list(self, ctx: commands.Context):
        cfg = load_settings().get("career", {})
        if not cfg.get("enabled", True):
            await ctx.send(embed=error_embed("معطل", "نظام الوظائف معطل من الإدارة."))
            return

        settings = load_settings()["economy"]
        user = get_user(ctx.author.id, settings["starting_balance"])
        my_level = int(user.get("level", 0))
        current_job = user.get("job", "")

        lines = []
        for job in sorted(cfg.get("jobs", []), key=lambda j: j.get("tier", 1)):
            req = int(job.get("req_level", 0))
            ok = my_level >= req
            mark = "✅" if ok else "🔒"
            current = "👈 وظيفتك الحالية" if job["id"] == current_job else ""
            lines.append(
                f"{mark} {job.get('emoji', '💼')} **{job['name']}** (`{job['id']}`)\n"
                f"└ الراتب: {currency(random.randint(*job.get('salary', [0, 0])))} تقريبًا — "
                f"المطلوب: مستوى {req} {current}"
            )

        embed = base_embed("💼 الوظائف المتاحة")
        embed.description = "\n\n".join(lines) or "لا توجد وظائف معرفة من الإدارة."
        embed.set_footer(text=f"مستواك الحالي: {my_level} — ترقى لوظائف أعلى بنظام XP")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------- التقديم
    @career.command(name="apply", description="التقدم لوظيفة")
    @app_commands.describe(job_id="معرف الوظيفة (شوفه بأمر career list)")
    async def career_apply(self, ctx: commands.Context, job_id: str):
        job = self._get_job(job_id.lower())
        if not job:
            await ctx.send(embed=error_embed("وظيفة غير موجودة", "شوف الوظائف المتاحة بأمر `career list`."))
            return

        settings = load_settings()["economy"]
        user = get_user(ctx.author.id, settings["starting_balance"])
        my_level = int(user.get("level", 0))
        req = int(job.get("req_level", 0))

        if my_level < req:
            await ctx.send(embed=error_embed(
                "ما تجلس المتطلبات",
                f"وظيفة **{job['name']}** تتطلب مستوى **{req}** وأنت مستواك **{my_level}**.\n"
                "اكثر تكلم بالسيرفر وارفع مستوايك!"
            ))
            return

        outcome = {}

        def apply_job(current):
            try:
                level = int(current.get("level", 0))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if level < req:
                outcome["level"] = level
                return False
            current.update({
                "job": job["id"],
                "job_seniority": 1,
                "job_works": 0,
            })
            return True

        try:
            atomic_update_user(ctx.author.id, apply_job, settings["starting_balance"])
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Career apply failed user=%s job=%s", ctx.author.id, job["id"])
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ الوظيفة. حاول لاحقًا."))
            return
        if outcome.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "بيانات حسابك غير صالحة."))
            return
        if outcome.get("level") is not None:
            await ctx.send(embed=error_embed(
                "المستوى ما يكفي",
                f"وظيفة **{job['name']}** تتطلب مستوى **{req}** وأنت **{outcome['level']}**."
            ))
            return

        await ctx.send(embed=success_embed(
            "تم التعيين!",
            f"مبروك! صرت **{job.get('emoji', '💼')} {job['name']}**\n"
            f"اشتغل بأمر `career work` وخذ راتبك."
        ))

    # ------------------------------------------------------------- العمل
    @career.command(name="work", description="اشتغل واحصل على راتبك")
    async def career_work(self, ctx: commands.Context):
        cfg = load_settings().get("career", {})
        if not cfg.get("enabled", True):
            await ctx.send(embed=error_embed("معطل", "نظام الوظائف معطل من الإدارة."))
            return

        settings = load_settings()["economy"]
        user = get_user(ctx.author.id, settings["starting_balance"])
        job_id = user.get("job", "")
        if not job_id:
            await ctx.send(embed=error_embed("ما عندك وظيفة", "تقدم لوظيفة أول بأمر `career list` ثم `career apply`."))
            return

        job = self._get_job(job_id)
        if not job:
            def clear_missing_job(current):
                if current.get("job") != job_id:
                    return False
                current["job"] = ""
                current["job_seniority"] = 1
                current["job_works"] = 0
                return True
            atomic_update_user(ctx.author.id, clear_missing_job, settings["starting_balance"])
            await ctx.send(embed=error_embed("الوظيفة ألغيت", "وظيفتك الحالية ما تعدت موجودة بالإعدادات. تقدم لوظيفة ثانية."))
            return

        now = now_ts()
        try:
            cooldown = int(cfg.get("work_cooldown_minutes", 45)) * 60
            salary_range = job.get("salary", [50, 100])
            if (
                cooldown < 0 or not isinstance(salary_range, list) or len(salary_range) != 2
                or int(salary_range[0]) < 0 or int(salary_range[0]) > int(salary_range[1])
            ):
                raise ValueError("invalid career work settings")
            salary_min, salary_max = int(salary_range[0]), int(salary_range[1])
            max_wallet = int(settings["max_wallet"])
            starting_balance = int(settings["starting_balance"])
            seniority_bonus = int(cfg.get("salary_seniority_bonus", 10))
            promote_after = max(1, int(cfg.get("promote_after", 10)))
            if min(max_wallet, seniority_bonus) < 0:
                raise ValueError("invalid career wallet settings")
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.exception("Invalid career work settings for user %s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "إعدادات العمل غير صالحة."))
            return

        outcome = {}

        def claim_salary(current):
            try:
                last_work = int(current.get("last_job_work", 0))
                wallet = max(0, int(current.get("wallet", 0)))
                total_earned = max(0, int(current.get("total_earned", 0)))
                seniority = max(1, int(current.get("job_seniority", 1)))
                works = max(0, int(current.get("job_works", 0))) + 1
                total_works = max(0, int(current.get("total_works", 0))) + 1
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if last_work < 0 or last_work > now:
                last_work = 0
            elapsed = now - last_work
            if elapsed < cooldown:
                outcome["remaining"] = cooldown - elapsed
                return False
            if current.get("job", "") != job_id:
                outcome["job_changed"] = True
                return False
            bonus_pct = seniority_bonus * (seniority - 1)
            salary = int(random.randint(salary_min, salary_max) * (1 + bonus_pct / 100))
            salary = min(salary, max(0, max_wallet - wallet))
            if salary <= 0:
                outcome["full"] = True
                return False
            current.update({
                "wallet": wallet + salary,
                "total_earned": total_earned + salary,
                "last_job_work": now,
                "job_works": works,
                "total_works": total_works,
            })
            outcome.update({"salary": salary, "bonus_pct": bonus_pct, "works": works})
            return True

        try:
            atomic_update_user(ctx.author.id, claim_salary, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Career salary failed for user %s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ راتبك. حاول لاحقًا."))
            return
        if "remaining" in outcome:
            await ctx.send(embed=error_embed("لسا بدري!", f"شغلتك القادمة بعد **{fmt_left(outcome['remaining'])}**."))
            return
        if outcome.get("job_changed"):
            await ctx.send(embed=error_embed("الوظيفة تغيّرت", "تحقق من وضعك الوظيفي قبل العمل مجددًا."))
            return
        if outcome.get("invalid") or outcome.get("full"):
            await ctx.send(embed=error_embed("خطأ", "تعذر إضافة الراتب إلى محفظتك."))
            return

        ready = outcome["works"] >= promote_after
        embed = success_embed(
            f"{job.get('emoji', '💼')} يومية زينة!",
            f"اشتغلت كـ **{job['name']}** وكسبت {currency(outcome['salary'])}"
            + (f" (بونص أقدمية +{outcome['bonus_pct']}%)" if outcome["bonus_pct"] else "")
        )
        progress = min(outcome["works"], promote_after)
        embed.add_field(
            name="📈 نحو الترقية",
            value=f"`{'█' * progress}{'░' * (promote_after - progress)}` {progress}/{promote_after}"
                  + ("\n✨ جاهز للترقية! استخدم `career promote`" if ready else ""),
            inline=False
        )
        logger.info("career_work user=%s amount=%s", ctx.author.id, outcome["salary"])
        await ctx.send(embed=embed)

    # ------------------------------------------------------------- الترقية
    @career.command(name="promote", description="اطلب ترقية لوظيفة أعلى")
    async def career_promote(self, ctx: commands.Context):
        cfg = load_settings().get("career", {})
        settings = load_settings()["economy"]
        user = get_user(ctx.author.id, settings["starting_balance"])
        job_id = user.get("job", "")

        if not job_id:
            await ctx.send(embed=error_embed("ما عندك وظيفة", "تقدم لوظيفة أول!"))
            return

        job = self._get_job(job_id)
        if not job:
            await ctx.send(embed=error_embed("خطأ", "وظيفتك الحالية غير معرفة بالإعدادات."))
            return

        promote_after = int(cfg.get("promote_after", 10))
        works = int(user.get("job_works", 0))
        if works < promote_after:
            await ctx.send(embed=error_embed(
                "لسا بدري للترقية",
                f"لازم تشتغل **{promote_after}** مرة على الأقل بعد آخر ترقية (شغلت {works})."
            ))
            return

        jobs = sorted(cfg.get("jobs", []), key=lambda j: j.get("tier", 1))
        current_tier = int(job.get("tier", 1))
        my_level = int(user.get("level", 0))
        next_jobs = [j for j in jobs if int(j.get("tier", 1)) > current_tier]

        if next_jobs:
            target = next_jobs[0]
            req = int(target.get("req_level", 0))
            if my_level < req:
                await ctx.send(embed=error_embed(
                    "المستوى ما يكفي",
                    f"وظيفة **{target['name']}** تتطلب مستوى **{req}** وأنت **{my_level}**.\n"
                    "فعّل أكثر وارفع مستوايك بعدين رجوع ترقى!"
                ))
                return
            promotion = {}

            def apply_promotion(current):
                try:
                    current_works = int(current.get("job_works", 0))
                    current_level = int(current.get("level", 0))
                except (TypeError, ValueError, OverflowError):
                    promotion["invalid"] = True
                    return False
                if current.get("job") != job_id:
                    promotion["changed"] = True
                    return False
                if current_works < promote_after:
                    promotion["works"] = current_works
                    return False
                if current_level < req:
                    promotion["level"] = current_level
                    return False
                current.update({"job": target["id"], "job_seniority": 1, "job_works": 0})
                return True

            try:
                atomic_update_user(ctx.author.id, apply_promotion, settings["starting_balance"])
            except (OSError, TypeError, ValueError, OverflowError):
                logger.exception("Career promotion failed user=%s target=%s", ctx.author.id, target["id"])
                await ctx.send(embed=error_embed("خطأ", "تعذر حفظ الترقية. حاول لاحقًا."))
                return
            if promotion.get("invalid"):
                await ctx.send(embed=error_embed("خطأ", "بيانات حسابك غير صالحة."))
                return
            if promotion.get("changed"):
                await ctx.send(embed=error_embed("الوظيفة تغيّرت", "تحقق من وضعك الوظيفي قبل الترقية مجددًا."))
                return
            if promotion.get("works", promote_after) < promote_after:
                await ctx.send(embed=error_embed("لسا بدري للترقية", f"لازم تشتغل **{promote_after}** مرة على الأقل بعد آخر ترقية (شغلت {promotion['works']})."))
                return
            if promotion.get("level", req) < req:
                await ctx.send(embed=error_embed("المستوى ما يكفي", f"وظيفة **{target['name']}** تتطلب مستوى **{req}** وأنت **{promotion['level']}**."))
                return

            await ctx.send(embed=success_embed(
                "🎉 ترقية مستحقة!",
                f"مبروك! رقيت من **{job['name']}** إلى **{target.get('emoji', '💼')} {target['name']}**\n"
                f"الراتب الجديد: {currency(random.randint(*target.get('salary', [0, 0])))} تقريبًا"
            ))
        else:
            # ما فيه وظيفة أعلى → زيادة أقدمية (بونص راتب دائم)
            max_seniority = 5
            seniority = int(user.get("job_seniority", 1))
            if seniority >= max_seniority:
                await ctx.send(embed=error_embed(
                    "قمة السلم! 🏔️",
                    f"وصلت أقصى أقدمية ({max_seniority}) في وظيفتك، وما فيه وظائف أعلى حاليًا."
                ))
                return
            seniority_outcome = {}

            def apply_seniority(current):
                try:
                    current_seniority = int(current.get("job_seniority", 1))
                    current_works = int(current.get("job_works", 0))
                except (TypeError, ValueError, OverflowError):
                    seniority_outcome["invalid"] = True
                    return False
                if current.get("job") != job_id:
                    seniority_outcome["changed"] = True
                    return False
                if current_works < promote_after:
                    seniority_outcome["works"] = current_works
                    return False
                if current_seniority >= max_seniority:
                    seniority_outcome["maxed"] = True
                    return False
                current["job_seniority"] = current_seniority + 1
                current["job_works"] = 0
                seniority_outcome["seniority"] = current_seniority + 1
                return True

            try:
                atomic_update_user(ctx.author.id, apply_seniority, settings["starting_balance"])
            except (OSError, TypeError, ValueError, OverflowError):
                logger.exception("Career seniority promotion failed user=%s", ctx.author.id)
                await ctx.send(embed=error_embed("خطأ", "تعذر حفظ الترقية. حاول لاحقًا."))
                return
            if seniority_outcome.get("invalid"):
                await ctx.send(embed=error_embed("خطأ", "بيانات حسابك غير صالحة."))
                return
            if seniority_outcome.get("changed"):
                await ctx.send(embed=error_embed("الوظيفة تغيّرت", "تحقق من وضعك الوظيفي قبل الترقية مجددًا."))
                return
            if seniority_outcome.get("maxed"):
                await ctx.send(embed=error_embed("قمة السلم! 🏔️", f"وصلت أقصى أقدمية ({max_seniority}) في وظيفتك، وما فيه وظائف أعلى حاليًا."))
                return
            if seniority_outcome.get("works") is not None:
                await ctx.send(embed=error_embed("لسا بدري للترقية", f"لازم تشتغل **{promote_after}** مرة على الأقل بعد آخر ترقية (شغلت {seniority_outcome['works']})."))
                return
            new_seniority = seniority_outcome["seniority"]
            bonus = int(cfg.get("salary_seniority_bonus", 10)) * (new_seniority - 1)
            await ctx.send(embed=success_embed(
                "⭐ ترقية أقدمية!",
                f"صار مستوى أقدميتك **{seniority + 1}** — راتبك زاد بونص دائم **+{bonus}%**"
            ))

    # ------------------------------------------------------------- الاستقالة
    @career.command(name="quit", description="استقل من وظيفتك")
    async def career_quit(self, ctx: commands.Context):
        settings = load_settings()["economy"]
        user = get_user(ctx.author.id, settings["starting_balance"])
        if not user.get("job"):
            await ctx.send(embed=error_embed("خطأ", "ما عندك وظيفة تستقيل منها!"))
            return
        quit_outcome = {}

        def quit_job(current):
            if not current.get("job"):
                quit_outcome["already"] = True
                return False
            current.update({"job": "", "job_seniority": 1, "job_works": 0})
            return True

        try:
            atomic_update_user(ctx.author.id, quit_job, settings["starting_balance"])
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Career quit failed user=%s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ الاستقالة. حاول لاحقًا."))
            return
        if quit_outcome.get("already"):
            await ctx.send(embed=error_embed("خطأ", "ما عندك وظيفة تستقيل منها!"))
            return
        await ctx.send(embed=success_embed("تمت الاستقالة", "استقليت من وظيفتك. تقدر تقدم لوظيفة جديدة بأي وقت."))

    # ------------------------------------------------------------- الوضع الوظيفي
    @career.command(name="status", description="وضعك الوظيفي الحالي")
    async def career_status(self, ctx: commands.Context):
        settings = load_settings()["economy"]
        user = get_user(ctx.author.id, settings["starting_balance"])
        job = self._get_job(user.get("job", "")) if user.get("job") else None

        embed = base_embed("📋 وضعك الوظيفي")
        if job:
            promote_after = int(load_settings().get("career", {}).get("promote_after", 10))
            works = int(user.get("job_works", 0))
            progress = min(works, promote_after)
            embed.add_field(name="💼 وظيفتك", value=f"{job.get('emoji', '💼')} {job['name']}", inline=True)
            embed.add_field(name="⭐ الأقدمية", value=f"مستوى {user.get('job_seniority', 1)}", inline=True)
            embed.add_field(name="✅ إجمالي الشغل", value=str(user.get("total_works", 0)), inline=True)
            embed.add_field(
                name="📈 الترقية",
                value=f"`{'█' * progress}{'░' * (promote_after - progress)}` {progress}/{promote_after}",
                inline=False
            )
        else:
            embed.description = "ما عندك وظيفة — تقدم بأمر `career list` ثم `career apply`."

        # البزنس
        biz = user.get("business")
        if biz:
            biz_cfg = load_settings().get("career", {}).get("business", {})
            next_collect = int(biz.get("last_collect", 0)) + 86400
            ready = now_ts() >= next_collect
            embed.add_field(
                name="🏪 بزنسك",
                value=f"مستوى {biz.get('level', 1)} — "
                      f"{'✅ جاهز للتحصيل!' if ready else f'التحصيل بعد {fmt_left(next_collect - now_ts())}'}",
                inline=False
            )
        else:
            biz_cfg = load_settings().get("career", {}).get("business", {})
            embed.add_field(
                name="🏪 بزنسك",
                value=f"ما عندك بزنس — ابدأ مشروعك بـ {currency(biz_cfg.get('start_cost', 10000))} بأمر `career business start`",
                inline=False
            )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------- البزنس
    @career.command(name="business", description="إدارة بزنسك الخاص")
    @app_commands.describe(action="start / collect / upgrade")
    @app_commands.choices(action=[
        app_commands.Choice(name="🚀 start — ابدأ مشروعك", value="start"),
        app_commands.Choice(name="💵 collect — احصل أرباح اليوم", value="collect"),
        app_commands.Choice(name="📈 upgrade — طوّر بزنسك", value="upgrade"),
    ])
    async def career_business(self, ctx: commands.Context, action: app_commands.Choice[str]):
        cfg = load_settings().get("career", {})
        biz_cfg = cfg.get("business", {})
        settings = load_settings().get("economy", {})
        now = now_ts()
        try:
            starting_balance = int(settings["starting_balance"])
            max_wallet = int(settings["max_wallet"])
            if max_wallet < 0:
                raise ValueError("invalid wallet limit")
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.exception("Invalid business economy settings user=%s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "إعدادات البزنس غير صالحة."))
            return

        outcome = {}

        if action.value == "start":
            try:
                cost = int(biz_cfg.get("start_cost", 10000))
                if cost < 0:
                    raise ValueError("invalid business start cost")
            except (TypeError, ValueError, OverflowError):
                await ctx.send(embed=error_embed("خطأ", "تكلفة البزنس غير صالحة."))
                return

            def start_business(user):
                try:
                    wallet = max(0, int(user.get("wallet", 0)))
                except (TypeError, ValueError, OverflowError):
                    outcome["invalid"] = True
                    return False
                biz = user.get("business")
                if biz:
                    outcome["exists"] = int(biz.get("level", 1)) if isinstance(biz, dict) else 1
                    return False
                if wallet < cost:
                    outcome["insufficient"] = wallet
                    return False
                user.update({"wallet": wallet - cost, "business": {"level": 1, "last_collect": 0}})
                return True

            try:
                atomic_update_user(ctx.author.id, start_business, starting_balance)
            except (OSError, TypeError, ValueError, OverflowError):
                logger.exception("Business purchase failed user=%s", ctx.author.id)
                await ctx.send(embed=error_embed("خطأ", "تعذر حفظ عملية فتح البزنس."))
                return
            if "exists" in outcome:
                await ctx.send(embed=error_embed("عندك بزنس!", f"بزنسك مستواه {outcome['exists']} — طوره بدل ما تفتح ثاني."))
                return
            if "insufficient" in outcome:
                await ctx.send(embed=error_embed("رصيد غير كافي", f"تفتح بزنس بـ {currency(cost)} ومحفظتك فيها {currency(outcome['insufficient'])}."))
                return
            if outcome.get("invalid"):
                await ctx.send(embed=error_embed("خطأ", "رصيد محفظتك غير صالح."))
                return
            logger.info("business_start user=%s amount=%s", ctx.author.id, cost)
            await ctx.send(embed=success_embed(
                "🚀 مشروع جديد!",
                f"فتحت بزنسك الخاص بـ {currency(cost)}!\n"
                f"كل يوم تقدر تحصّل أرباح بأمر `career business collect`."
            ))

        elif action.value == "collect":
            try:
                daily_min = int(biz_cfg.get("daily_min", 100))
                daily_max = int(biz_cfg.get("daily_max", 400))
                if daily_min < 0 or daily_min > daily_max:
                    raise ValueError("invalid business profit settings")
            except (TypeError, ValueError, OverflowError):
                await ctx.send(embed=error_embed("خطأ", "إعدادات أرباح البزنس غير صالحة."))
                return

            def collect_profit(user):
                try:
                    biz = user.get("business")
                    if not isinstance(biz, dict):
                        outcome["missing"] = True
                        return False
                    level = max(1, int(biz.get("level", 1)))
                    last_collect = int(biz.get("last_collect", 0))
                    wallet = max(0, int(user.get("wallet", 0)))
                    earned = max(0, int(user.get("total_earned", 0)))
                except (TypeError, ValueError, OverflowError):
                    outcome["invalid"] = True
                    return False
                if last_collect < 0 or last_collect > now:
                    last_collect = 0
                elapsed = now - last_collect
                if elapsed < 86400:
                    outcome["remaining"] = 86400 - elapsed
                    return False
                profit = random.randint(daily_min, daily_max) * level
                actual = min(profit, max(0, max_wallet - wallet))
                if actual <= 0:
                    outcome["full"] = True
                    return False
                user.update({
                    "wallet": wallet + actual,
                    "total_earned": earned + actual,
                    "business": {"level": level, "last_collect": now},
                })
                outcome.update({"profit": actual, "level": level})
                return True

            try:
                atomic_update_user(ctx.author.id, collect_profit, starting_balance)
            except (OSError, TypeError, ValueError, OverflowError):
                logger.exception("Business collection failed user=%s", ctx.author.id)
                await ctx.send(embed=error_embed("خطأ", "تعذر حفظ أرباح البزنس."))
                return
            if outcome.get("missing"):
                await ctx.send(embed=error_embed("ما عندك بزنس", "ابدأ مشروعك أول بأمر `career business start`."))
                return
            if "remaining" in outcome:
                await ctx.send(embed=error_embed("لسا بدري!", f"أرباح اليوم تتحصل بعد **{fmt_left(outcome['remaining'])}**."))
                return
            if outcome.get("invalid") or outcome.get("full"):
                await ctx.send(embed=error_embed("خطأ", "تعذر إضافة أرباح البزنس إلى محفظتك."))
                return
            logger.info("business_collect user=%s amount=%s", ctx.author.id, outcome["profit"])
            await ctx.send(embed=success_embed(
                "💵 أرباح البزنس",
                f"حصلت على {currency(outcome['profit'])} من بزنسك (مستوى {outcome['level']})."
            ))

        elif action.value == "upgrade":
            try:
                base_cost = int(biz_cfg.get("upgrade_cost", 5000))
                mult = float(biz_cfg.get("upgrade_multiplier", 1.3))
                max_level = int(biz_cfg.get("max_level", 10))
                if base_cost < 0 or not math.isfinite(mult) or mult <= 0 or max_level < 1:
                    raise ValueError("invalid business upgrade settings")
            except (TypeError, ValueError, OverflowError):
                await ctx.send(embed=error_embed("خطأ", "إعدادات تطوير البزنس غير صالحة."))
                return

            def upgrade_business(user):
                try:
                    biz = user.get("business")
                    if not isinstance(biz, dict):
                        outcome["missing"] = True
                        return False
                    level = max(1, int(biz.get("level", 1)))
                    wallet = max(0, int(user.get("wallet", 0)))
                    last_collect = max(0, int(biz.get("last_collect", 0)))
                except (TypeError, ValueError, OverflowError):
                    outcome["invalid"] = True
                    return False
                if level >= max_level:
                    outcome["max_level"] = level
                    return False
                try:
                    cost = int(base_cost * (mult ** (level - 1)))
                except OverflowError:
                    outcome["invalid"] = True
                    return False
                if cost < 0 or cost > max_wallet:
                    outcome["invalid"] = True
                    return False
                if wallet < cost:
                    outcome.update({"insufficient": wallet, "cost": cost, "level": level})
                    return False
                user.update({
                    "wallet": wallet - cost,
                    "business": {"level": level + 1, "last_collect": last_collect},
                })
                outcome.update({"level": level + 1, "cost": cost})
                return True

            try:
                atomic_update_user(ctx.author.id, upgrade_business, starting_balance)
            except (OSError, TypeError, ValueError, OverflowError):
                logger.exception("Business upgrade failed user=%s", ctx.author.id)
                await ctx.send(embed=error_embed("خطأ", "تعذر حفظ تطوير البزنس."))
                return
            if outcome.get("missing"):
                await ctx.send(embed=error_embed("ما عندك بزنس", "ابدأ مشروعك أول بأمر `career business start`."))
                return
            if "max_level" in outcome:
                await ctx.send(embed=error_embed("أقصى مستوى", f"بزنسك وصل أقصى مستوى ({max_level}). مبروك يا رجل أعمال! 👔"))
                return
            if "insufficient" in outcome:
                await ctx.send(embed=error_embed("رصيد غير كافي", f"التطوير يكلف {currency(int(base_cost * (mult ** (outcome['level'] - 1))))} ومحفظتك فيها {currency(outcome['insufficient'])}."))
                return
            if outcome.get("invalid"):
                await ctx.send(embed=error_embed("خطأ", "بيانات البزنس أو تكلفة التطوير غير صالحة."))
                return
            logger.info("business_upgrade user=%s level=%s amount=%s", ctx.author.id, outcome["level"], outcome["cost"])
            await ctx.send(embed=success_embed(
                "📈 تم التطوير!",
                f"بزنسك صار **مستوى {outcome['level']}** — أرباحك اليومية زادت!\n"
                f"التطوير القادم يكلف حوالي {currency(int(base_cost * (mult ** outcome['level'])))}."
            ))


async def setup(bot: commands.Bot):
    await bot.add_cog(Career(bot))
