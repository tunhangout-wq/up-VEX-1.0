"""
levels.py
----------
نظام الخبرة والمستويات (XP / Level) مع رتب تلقائية حسب النشاط:
  - العضو ياخذ خبرة كل ما يتكلم (مع كولداون لمنع السبام)
  - كل ما يوصل مستوى جديد → إعلان + إعطاء رتبة تلقائية لو محددة بالإعدادات
  - /rank لعرض مستواك و /levels لقائمة الأعلى مستوى

الرتب التلقائية تُضبط من لوحة التحكم: settings.levels.level_roles
مثال: [{"level": 5, "role_id": "123..."}, {"level": 10, "role_id": "456..."}]
"""

import random
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.data_manager import (
    atomic_update_user, get_user, load_settings, load_users, get_levels_leaderboard, now_ts
)
from bot.utils.embeds import base_embed, success_embed, error_embed, currency

logger = logging.getLogger(__name__)


def xp_needed(level: int) -> int:
    """الخبرة المطلوبة للانتقال من مستوى `level` إلى المستوى التالي."""
    return 100 + 50 * level


def progress_bar(current: int, needed: int, length: int = 10) -> str:
    filled = int(length * min(1.0, current / max(1, needed)))
    return "█" * filled + "░" * (length - filled)


class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.level_up_queue = {}  # (guild_id, user_id, new_level) → channel_id للتأخير

    # ------------------------------------------------------------- خبرة الكلام
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        settings = load_settings()
        cfg = settings.get("levels", {})
        if not cfg.get("enabled", True):
            return

        now = now_ts()
        try:
            cooldown = int(cfg.get("xp_cooldown", 60))
            xp_min = int(cfg.get("xp_min", 3))
            xp_max = int(cfg.get("xp_max", 8))
            starting_balance = int(settings.get("economy", {}).get("starting_balance", 100))
            if cooldown < 0 or xp_min < 0 or xp_min > xp_max or xp_max > 10000:
                raise ValueError("invalid XP settings")
        except (AttributeError, TypeError, ValueError, OverflowError):
            logger.exception("Invalid levels configuration")
            return
        outcome = {}

        def award_xp(user):
            try:
                last_xp = int(user.get("last_xp", 0))
                old_level = max(0, int(user.get("level", 0)))
                xp = max(0, int(user.get("xp", 0)))
                messages = max(0, int(user.get("messages", 0)))
                if old_level > 100000 or xp > 1_000_000_000_000:
                    raise ValueError("stored XP is out of bounds")
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if last_xp < 0 or last_xp > now:
                last_xp = 0
            if now - last_xp < cooldown:
                return False
            level = old_level
            xp_in_level = xp + random.randint(xp_min, xp_max)
            for _ in range(1000):
                needed = xp_needed(level)
                if xp_in_level < needed or level >= 100000:
                    break
                xp_in_level -= needed
                level += 1
            user.update({
                "xp": xp_in_level,
                "level": level,
                "last_xp": now,
                "messages": messages + 1,
            })
            outcome.update({"old_level": old_level, "level": level})
            return True

        try:
            atomic_update_user(message.author.id, award_xp, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("XP update failed user=%s guild=%s", message.author.id, message.guild.id)
            return
        if outcome.get("invalid"):
            logger.warning("Invalid saved XP data user=%s", message.author.id)
            return
        if outcome.get("level", 0) > outcome.get("old_level", 0):
            await self._handle_level_up(message, outcome["level"])

    # ------------------------------------------------------------- ترقية مستوى
    async def _handle_level_up(self, message: discord.Message, new_level: int):
        cfg = load_settings().get("levels", {})

        # مكافأة فلوس عند الترقية (اختيارية)
        reward = int(cfg.get("level_up_reward", 100))
        if reward > 0:
            from bot.utils.data_manager import add_wallet
            settings = load_settings()["economy"]
            add_wallet(message.author.id, reward, settings["max_wallet"])

        if cfg.get("announce", True):
            try:
                await message.channel.send(
                    embed=success_embed(
                        "🎉 مستوى جديد!",
                        f"مبروك {message.author.mention}! وصلت للمستوى **{new_level}**"
                        + (f" وجائزة {currency(reward)} 🪙" if reward > 0 else "")
                    ),
                    delete_after=10
                )
            except discord.HTTPException:
                logger.exception("Could not announce level up guild=%s user=%s", message.guild.id, message.author.id)

        # رتب تلقائية
        role_entries = cfg.get("level_roles", []) or []
        if not isinstance(role_entries, list):
            logger.warning("Invalid level_roles setting; expected a list")
            return
        parsed_entries = []
        for entry in role_entries[:1000]:
            if not isinstance(entry, dict):
                logger.warning("Ignoring malformed automatic level-role entry")
                continue
            try:
                required_level = max(0, int(entry.get("level", 0)))
            except (TypeError, ValueError, OverflowError):
                logger.warning("Ignoring automatic level-role entry with invalid level")
                continue
            parsed_entries.append((required_level, entry))

        for required_level, entry in sorted(parsed_entries, key=lambda item: item[0], reverse=True):
            if new_level >= required_level and entry.get("role_id"):
                try:
                    role_id = int(entry["role_id"])
                except (TypeError, ValueError, OverflowError):
                    logger.warning("Ignoring automatic level-role entry with invalid role ID")
                    continue
                role = message.guild.get_role(role_id)
                if role and role not in message.author.roles:
                    bot_member = message.guild.me
                    if bot_member is None or role >= bot_member.top_role or role.managed:
                        logger.warning(
                            "Automatic level role blocked by bot hierarchy guild=%s role=%s user=%s",
                            message.guild.id, role.id, message.author.id,
                        )
                        break
                    try:
                        await message.author.add_roles(role, reason=f"وصل المستوى {new_level}")
                    except discord.HTTPException:
                        logger.exception("Could not grant level role guild=%s role=%s user=%s", message.guild.id, role.id, message.author.id)
                break

    # ------------------------------------------------------------- بطاقة المستوى
    @commands.hybrid_command(name="rank", aliases=["مستواي", "level"], description="عرض مستواك وخبرتك")
    @app_commands.describe(member="العضو اللي تبي تشوف مستواه")
    async def rank(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        settings = load_settings()
        user = get_user(member.id, settings["economy"]["starting_balance"])

        level = int(user.get("level", 0))
        xp = int(user.get("xp", 0))
        needed = xp_needed(level)

        # ترتيب العضو
        users = load_users()
        sorted_users = sorted(users.items(), key=lambda kv: (kv[1].get("level", 0), kv[1].get("xp", 0)), reverse=True)
        position = next((i + 1 for i, (uid, _) in enumerate(sorted_users) if uid == str(member.id)), len(sorted_users))

        embed = base_embed(f"📊 مستوى {member.display_name}")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="🏅 المستوى", value=str(level), inline=True)
        embed.add_field(name="⭐ الخبرة", value=f"{xp} / {needed}", inline=True)
        embed.add_field(name="📈 الترتيب", value=f"#{position} من {len(users)}", inline=True)
        embed.add_field(
            name="التقدم للمستوى القادم",
            value=f"`{progress_bar(xp, needed)}`",
            inline=False
        )
        embed.add_field(name="💬 الرسائل", value=f"{user.get('messages', 0)}", inline=True)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------- لوحة المستويات
    @commands.hybrid_command(name="levels", aliases=["toplevels"], description="أعلى الأعضاء مستوى")
    async def levels(self, ctx: commands.Context):
        top = get_levels_leaderboard(10)
        if not top:
            await ctx.send(embed=error_embed("لا توجد بيانات", "ما فيه أي نشاط بعد."))
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, data) in enumerate(top):
            member = ctx.guild.get_member(int(uid)) if ctx.guild else None
            name = member.display_name if member else f"عضو {uid[-4:]}"
            medal = medals[i] if i < 3 else f"`#{i + 1}`"
            lines.append(f"{medal} **{name}** — مستوى {data.get('level', 0)} ({data.get('xp', 0)} XP)")

        embed = base_embed("📊 أعلى الأعضاء مستوى")
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))
