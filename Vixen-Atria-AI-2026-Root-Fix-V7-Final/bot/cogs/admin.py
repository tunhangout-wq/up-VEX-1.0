"""
admin.py
---------
أوامر إدارية عامة:
  - /logs — عرض آخر عمليات الإدارة (تفعيل رتب، إضافة فلوس، ...)

أوامر إدارة الفلوس انتقلت إلى staff.py بنظام صلاحيات الرتب
(Dev / Founder / Team + المالك) — وتقبل أيضًا صلاحية Administrator بالديسكورد.
"""

import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils import ranks
from bot.utils.embeds import base_embed


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="logs", aliases=["سجل"], description="عرض آخر عمليات الإدارة")
    @ranks.rank_check("view_logs")
    async def logs(self, ctx: commands.Context):
        entries = ranks.load_logs()
        if not entries:
            embed = base_embed("📜 سجل العمليات")
            embed.description = "ما فيه أي عمليات مسجلة بعد."
            await ctx.send(embed=embed, ephemeral=True)
            return

        lines = []
        for entry in entries[-12:]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(entry.get("ts", 0)))
            source = "🖥️" if entry.get("source") == "dashboard" else "💬"
            lines.append(
                f"`{ts}` {source} **{entry.get('actor_name', '؟')}** — "
                f"{entry.get('action', '')}: {entry.get('details', '')}"
            )

        embed = base_embed(f"📜 آخر {min(12, len(entries))} عمليات إدارية")
        embed.description = "\n".join(lines)
        embed.set_footer(text="💬 ديسكورد | 🖥️ لوحة التحكم — السجل الكامل بالداشبورد")
        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
