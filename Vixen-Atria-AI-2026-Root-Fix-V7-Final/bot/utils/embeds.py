"""
embeds.py
----------
دوال مساعدة لإنشاء Embeds احترافية وموحدة الشكل بكل أوامر البوت.
"""

import discord
from datetime import datetime, timezone
import logging
import math
from bot.utils.data_manager import load_settings

logger = logging.getLogger(__name__)


def _hex_to_int(hex_color: str) -> int:
    if not isinstance(hex_color, str) or len(hex_color.strip().lstrip("#")) not in (3, 6):
        logger.warning("Invalid embed color setting; using the default color")
        return 0xF5A623
    try:
        return int(hex_color.replace("#", ""), 16)
    except (TypeError, ValueError):
        logger.warning("Invalid embed color setting; using the default color")
        return 0xF5A623


def base_embed(title: str = None, description: str = None, color_key: str = "embed_color") -> discord.Embed:
    settings = load_settings()
    bot_cfg = settings.get("bot", {})
    color_hex = bot_cfg.get(color_key, "#F5A623")
    embed = discord.Embed(
        title=title,
        description=description,
        color=_hex_to_int(color_hex),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=bot_cfg.get("name", "Vixen EDR"))
    return embed


def success_embed(title: str, description: str) -> discord.Embed:
    return base_embed(f"✅ {title}", description, color_key="success_color")


def error_embed(title: str, description: str) -> discord.Embed:
    return base_embed(f"❌ {title}", description, color_key="error_color")


def currency(amount: int) -> str:
    settings = load_settings()
    symbol = settings.get("bot", {}).get("currency_symbol", "🪙")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        logger.warning("Invalid currency amount; displaying zero")
        amount = 0
    elif isinstance(amount, float) and not math.isfinite(amount):
        logger.warning("Non-finite currency amount; displaying zero")
        amount = 0
    return f"{amount:,} {symbol}"
