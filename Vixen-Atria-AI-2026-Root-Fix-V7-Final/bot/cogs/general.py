"""Dynamic help: every loaded Discord command is discoverable, including Control Center."""
import discord
from discord.ext import commands
from bot.utils.data_manager import load_settings
from bot.utils.embeds import base_embed

class General(commands.Cog):
    def __init__(self, bot): self.bot=bot
    @commands.hybrid_command(name="help", aliases=["مساعدة"], description="عرض جميع أوامر البوت")
    async def help_cmd(self, ctx):
        prefix=load_settings().get("bot",{}).get("prefix","!")
        embed=base_embed("📚 Vixen EDR — Command Directory")
        groups={}
        for c in self.bot.walk_commands():
            if isinstance(c,commands.Group):
                continue
            parts=c.qualified_name.split()
            group=parts[0]
            groups.setdefault(group,[]).append(c)
        for group, items in sorted(groups.items()):
            lines=[]
            for c in items[:15]:
                lines.append(f"`/{c.qualified_name}` · `{prefix}{c.qualified_name}` — {c.description or 'بدون وصف'}")
            if len(items)>15: lines.append(f"… +{len(items)-15}")
            embed.add_field(name=f"🛡️ {group}",value="\n".join(lines),inline=False)
        embed.set_footer(text=f"{sum(len(v) for v in groups.values())} executable commands • Slash + Prefix")
        await ctx.send(embed=embed)
async def setup(bot): await bot.add_cog(General(bot))
