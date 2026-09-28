"""
shop.py
--------
نظام المتجر: عرض المنتجات، الشراء، وعرض المخزون (Inventory).
المنتجات تُدار بالكامل من لوحة التحكم عن طريق settings.json -> shop.items
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.data_manager import atomic_update_user, get_user, load_settings
from bot.utils.embeds import base_embed, success_embed, error_embed, currency

logger = logging.getLogger(__name__)


class Shop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="shop", aliases=["متجر"], description="عرض متجر السيرفر")
    async def shop(self, ctx: commands.Context):
        settings = load_settings()
        items = settings.get("shop", {}).get("items", [])

        if not items:
            await ctx.send(embed=error_embed("المتجر فارغ", "ما فيه منتجات بالمتجر حاليًا."))
            return

        embed = base_embed("🛒 المتجر", "استخدم `buy <id>` عشان تشتري منتج")
        for item in items:
            embed.add_field(
                name=f"{item.get('emoji', '📦')} {item['name']}  —  {currency(item['price'])}",
                value=f"{item.get('description', '')}\n`ID: {item['id']}`",
                inline=False
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="buy", aliases=["شراء"], description="اشتري منتج من المتجر")
    @app_commands.describe(item_id="آيدي المنتج من المتجر")
    async def buy(self, ctx: commands.Context, item_id: str):
        settings = load_settings()
        e = settings.get("economy", {})
        items = settings.get("shop", {}).get("items", [])
        if not isinstance(items, list):
            await ctx.send(embed=error_embed("خطأ", "قائمة المتجر غير صالحة."))
            return

        item = next((entry for entry in items if isinstance(entry, dict) and entry.get("id") == item_id), None)
        if not item:
            await ctx.send(embed=error_embed("غير موجود", "ما فيه منتج بهذا الآيدي. شوف `shop` للقائمة الكاملة."))
            return

        price = item.get("price")
        try:
            max_wallet = int(e["max_wallet"])
            starting_balance = int(e["starting_balance"])
            if not isinstance(price, int) or isinstance(price, bool) or price < 0 or price > max_wallet:
                raise ValueError("invalid product price")
            if not isinstance(item_id, str) or not item_id or len(item_id) > 100:
                raise ValueError("invalid product id")
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.warning("Invalid shop item configuration for item %r", item_id)
            await ctx.send(embed=error_embed("خطأ", "سعر المنتج أو إعداداته غير صالحة."))
            return

        role = None
        role_added = False
        if item.get("type") == "role":
            try:
                role_id = int(item.get("role_id"))
                role = ctx.guild.get_role(role_id)
                bot_member = ctx.guild.me
                if role is None or bot_member is None or role.managed or role >= bot_member.top_role:
                    raise ValueError("shop role is missing or above the bot")
                if role not in ctx.author.roles:
                    await ctx.author.add_roles(role, reason="شراء من المتجر")
                    role_added = True
            except (discord.Forbidden, discord.HTTPException, TypeError, ValueError, AttributeError):
                await ctx.send(embed=error_embed(
                    "خطأ بالصلاحيات",
                    "ما قدرت أعطيك الرتبة. تأكد إن رتبة البوت أعلى من الرتبة المطلوبة."
                ))
                return

        result = {}

        def purchase(user):
            try:
                wallet = max(0, int(user.get("wallet", 0)))
            except (TypeError, ValueError, OverflowError):
                result["invalid"] = True
                return False
            inventory = user.get("inventory", [])
            if not isinstance(inventory, list) or len(inventory) >= 1000:
                result["invalid"] = True
                return False
            if wallet < price:
                result["insufficient"] = True
                return False
            user.update({"wallet": wallet - price, "inventory": inventory + [item_id]})
            return True

        try:
            atomic_update_user(ctx.author.id, purchase, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Shop purchase failed user=%s item=%s", ctx.author.id, item_id)
            result["failed"] = True

        if result:
            if role_added and role is not None:
                try:
                    await ctx.author.remove_roles(role, reason="إلغاء عملية شراء لم تُحفظ")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not roll back shop role user=%s role=%s", ctx.author.id, role.id)
            if result.get("insufficient"):
                await ctx.send(embed=error_embed("رصيد غير كافي", f"تحتاج {currency(price)} لشراء هذا المنتج."))
            elif result.get("invalid"):
                await ctx.send(embed=error_embed("خطأ", "تعذر إكمال الشراء بسبب بيانات غير صالحة."))
            else:
                await ctx.send(embed=error_embed("خطأ", "تعذر حفظ عملية الشراء. حاول لاحقًا."))
            return

        logger.info("shop_purchase user=%s item=%s amount=%s", ctx.author.id, item_id, price)
        await ctx.send(embed=success_embed(
            "تم الشراء! 🎉",
            f"اشتريت **{item.get('name', item_id)}** مقابل {currency(price)}"
        ))

    @commands.hybrid_command(name="inventory", aliases=["inv", "مخزون"], description="عرض مخزونك من المنتجات")
    async def inventory(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        settings = load_settings()
        items = {i["id"]: i for i in settings.get("shop", {}).get("items", [])}

        user = get_user(member.id, settings["economy"]["starting_balance"])
        inv = user.get("inventory", [])

        if not inv:
            await ctx.send(embed=base_embed("المخزون", f"{member.display_name} ما عنده أي منتجات."))
            return

        counts = {}
        for iid in inv:
            counts[iid] = counts.get(iid, 0) + 1

        lines = []
        for iid, count in counts.items():
            item = items.get(iid)
            name = item["name"] if item else iid
            emoji = item.get("emoji", "📦") if item else "📦"
            lines.append(f"{emoji} {name} × {count}")

        embed = base_embed(f"🎒 مخزون {member.display_name}", "\n".join(lines))
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Shop(bot))
