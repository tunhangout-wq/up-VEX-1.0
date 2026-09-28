"""
leaderboard.py
---------------
عرض لوحة صدارة أغنى الأعضاء بالسيرفر.
"""

import discord
from discord.ext import commands

from bot.utils.data_manager import get_leaderboard, load_settings
from bot.utils.embeds import base_embed, error_embed, currency

MEDALS = ["🥇", "🥈", "🥉"]


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top", "صدارة"], description="عرض أغنى الأعضاء")
    async def leaderboard(self, ctx: commands.Context):
        settings = load_settings()
        limit = settings.get("leaderboard", {}).get("show_top", 10)
        ranked = get_leaderboard(limit)

        if not ranked:
            await ctx.send(embed=error_embed("فاضي", "ما فيه بيانات كافية للصدارة بعد."))
            return

        lines = []
        for idx, (uid, data) in enumerate(ranked):
            total = data.get("wallet", 0) + data.get("bank", 0)
            member = ctx.guild.get_member(int(uid))
            name = member.display_name if member else f"مستخدم ({uid})"
            rank_icon = MEDALS[idx] if idx < 3 else f"`#{idx + 1}`"
            lines.append(f"{rank_icon} **{name}** — {currency(total)}")

        embed = base_embed("🏆 لوحة الصدارة", "\n".join(lines))
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))
