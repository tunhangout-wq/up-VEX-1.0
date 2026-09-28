"""
gambling.py
------------
أوامر المقامرة: سلوتس، كوين فليب، بلاك جاك.
"""

import random
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.data_manager import atomic_update_user, add_wallet, load_settings
from bot.utils.embeds import base_embed, success_embed, error_embed, currency

SLOT_EMOJIS = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
logger = logging.getLogger(__name__)


def check_gambling_enabled():
    async def predicate(ctx):
        settings = load_settings()["gambling"]
        if not settings.get("gambling_enabled", True):
            await ctx.send(embed=error_embed("مغلق", "نظام المقامرة موقوف حاليًا من الإدارة."))
            return False
        return True
    return commands.check(predicate)


class BlackjackView(discord.ui.View):
    def __init__(self, ctx, bet: int, max_wallet: int):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.bet = bet
        self.max_wallet = max_wallet
        self.deck = self._new_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.finished = False
        self.action_lock = asyncio.Lock()

    def _new_deck(self):
        suits = ["♠️", "♥️", "♦️", "♣️"]
        ranks = list(range(2, 11)) + ["J", "Q", "K", "A"]
        deck = [(r, s) for s in suits for r in ranks]
        random.shuffle(deck)
        return deck

    @staticmethod
    def hand_value(hand):
        value = 0
        aces = 0
        for rank, _ in hand:
            if rank == "A":
                value += 11
                aces += 1
            elif rank in ("J", "Q", "K"):
                value += 10
            else:
                value += rank
        while value > 21 and aces:
            value -= 10
            aces -= 1
        return value

    @staticmethod
    def hand_str(hand):
        return " ".join(f"{r}{s}" for r, s in hand)

    def build_embed(self, reveal_dealer=False, result_text=None):
        embed = base_embed("🃏 بلاك جاك")
        embed.add_field(
            name=f"يدك ({self.hand_value(self.player)})",
            value=self.hand_str(self.player),
            inline=False
        )
        if reveal_dealer:
            embed.add_field(
                name=f"يد الديلر ({self.hand_value(self.dealer)})",
                value=self.hand_str(self.dealer),
                inline=False
            )
        else:
            embed.add_field(
                name="يد الديلر",
                value=f"{self.hand_str([self.dealer[0]])} 🎴",
                inline=False
            )
        embed.add_field(name="الرهان", value=currency(self.bet), inline=False)
        if result_text:
            embed.description = result_text
        return embed

    async def end_game(self, interaction, outcome: str):
        if self.finished:
            await interaction.response.send_message("انتهت هذه الجولة بالفعل.", ephemeral=True)
            return
        self.finished = True
        for child in self.children:
            child.disabled = True

        if outcome == "win":
            add_wallet(self.ctx.author.id, self.bet * 2, self.max_wallet)
            text = f"🎉 فزت! ربحت {currency(self.bet)}"
        elif outcome == "push":
            add_wallet(self.ctx.author.id, self.bet, self.max_wallet)
            text = "🤝 تعادل! رجع لك رهانك."
        elif outcome == "blackjack":
            win_amount = int(self.bet * 1.5)
            add_wallet(self.ctx.author.id, self.bet + win_amount, self.max_wallet)
            text = f"🂡 بلاك جاك! ربحت {currency(win_amount)}"
        else:
            text = f"💥 خسرت {currency(self.bet)}"
        logger.info("blackjack_settlement user=%s outcome=%s bet=%s", self.ctx.author.id, outcome, self.bet)

        embed = self.build_embed(reveal_dealer=True, result_text=text)
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="اسحب (Hit)", style=discord.ButtonStyle.primary, emoji="🎴")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("هذي مو لعبتك!", ephemeral=True)
            return
        async with self.action_lock:
            if self.finished:
                await interaction.response.send_message("انتهت هذه الجولة بالفعل.", ephemeral=True)
                return
            self.player.append(self.deck.pop())
            if self.hand_value(self.player) > 21:
                await self.end_game(interaction, "lose")
                return
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="وقف (Stand)", style=discord.ButtonStyle.secondary, emoji="✋")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("هذي مو لعبتك!", ephemeral=True)
            return
        async with self.action_lock:
            if self.finished:
                await interaction.response.send_message("انتهت هذه الجولة بالفعل.", ephemeral=True)
                return
            while self.hand_value(self.dealer) < 17:
                self.dealer.append(self.deck.pop())

            player_val = self.hand_value(self.player)
            dealer_val = self.hand_value(self.dealer)

            if dealer_val > 21 or player_val > dealer_val:
                await self.end_game(interaction, "win")
            elif player_val == dealer_val:
                await self.end_game(interaction, "push")
            else:
                await self.end_game(interaction, "lose")

    async def on_timeout(self):
        async with self.action_lock:
            self.finished = True
            for child in self.children:
                child.disabled = True


class Gambling(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --------------------------------------------------------------- سلوتس
    @commands.hybrid_command(name="slots", aliases=["سلوتس"], description="جرب حظك بالسلوتس")
    @app_commands.describe(bet="مبلغ الرهان")
    @check_gambling_enabled()
    async def slots(self, ctx: commands.Context, bet: int):
        settings = load_settings()
        g = settings["gambling"]
        e = settings["economy"]

        try:
            minimum = int(g["slots_min_bet"])
            maximum = int(g["slots_max_bet"])
            win_multiplier = int(g["slots_win_multiplier"])
            jackpot_multiplier = int(g["slots_jackpot_multiplier"])
            starting_balance = int(e["starting_balance"])
            max_wallet = int(e["max_wallet"])
            if (
                not isinstance(bet, int) or isinstance(bet, bool)
                or min(minimum, maximum, max_wallet) < 0 or minimum > maximum
                or not 1 <= win_multiplier <= 100 or not 1 <= jackpot_multiplier <= 100
            ):
                raise ValueError("invalid slots settings")
        except (KeyError, TypeError, ValueError, OverflowError):
            await ctx.send(embed=error_embed("خطأ", "إعدادات السلوتس غير صالحة."))
            return
        if bet < minimum or bet > maximum:
            await ctx.send(embed=error_embed(
                "رهان غير صالح",
                f"الرهان لازم يكون بين {currency(minimum)} و {currency(maximum)}"
            ))
            return

        result = [random.choice(SLOT_EMOJIS) for _ in range(3)]
        display = " | ".join(result)
        multiplier = 0
        title = ""
        if result[0] == result[1] == result[2]:
            if result[0] == "7️⃣":
                multiplier = jackpot_multiplier
                title = "💎 جاكبوت! 💎"
            else:
                multiplier = win_multiplier
                title = "🎉 فوز! 🎉"
        elif result[0] == result[1] or result[1] == result[2]:
            multiplier = 1
            title = "زوج! 🎊"

        if multiplier < 0 or (multiplier and multiplier > 100):
            await ctx.send(embed=error_embed("خطأ", "إعدادات السلوتس غير صالحة."))
            return
        winnings = bet * multiplier if multiplier > 1 else int(bet * 1.2) if multiplier == 1 else 0
        outcome = {}

        def play(user):
            try:
                wallet = max(0, int(user.get("wallet", 0)))
                earned = max(0, int(user.get("total_earned", 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if wallet < bet:
                outcome["insufficient"] = True
                return False
            remaining_wallet = wallet - bet
            actual_win = min(winnings, max(0, max_wallet - remaining_wallet))
            user["wallet"] = remaining_wallet + actual_win
            if actual_win:
                user["total_earned"] = earned + actual_win
            outcome["winnings"] = actual_win
            return True

        try:
            atomic_update_user(ctx.author.id, play, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Slots transaction failed user=%s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ نتيجة الرهان."))
            return
        if outcome.get("insufficient"):
            await ctx.send(embed=error_embed("رصيد غير كافي", "ما عندك فلوس كافية لهذا الرهان."))
            return
        if outcome.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "بيانات محفظتك غير صالحة."))
            return
        if outcome["winnings"]:
            embed = success_embed(title, f"[ {display} ]\nربحت {currency(outcome['winnings'])}")
        else:
            embed = error_embed("خسرت 😢", f"[ {display} ]\nخسرت {currency(bet)}")
        logger.info("slots user=%s bet=%s winnings=%s", ctx.author.id, bet, outcome["winnings"])

        await ctx.send(embed=embed)

    # ------------------------------------------------------------- كوينفليب
    @commands.hybrid_command(name="coinflip", aliases=["cf", "عملة"], description="راهن على وجه العملة")
    @app_commands.describe(bet="مبلغ الرهان", choice="وجه ولا كتابة")
    @app_commands.choices(choice=[
        app_commands.Choice(name="وجه", value="heads"),
        app_commands.Choice(name="كتابة", value="tails"),
    ])
    @check_gambling_enabled()
    async def coinflip(self, ctx: commands.Context, bet: int, choice: str):
        settings = load_settings()
        g = settings["gambling"]
        e = settings["economy"]

        try:
            minimum = int(g["coinflip_min_bet"])
            maximum = int(g["coinflip_max_bet"])
            starting_balance = int(e["starting_balance"])
            max_wallet = int(e["max_wallet"])
            if not isinstance(bet, int) or isinstance(bet, bool) or min(minimum, maximum, max_wallet) < 0 or minimum > maximum:
                raise ValueError("invalid coinflip settings")
        except (KeyError, TypeError, ValueError, OverflowError):
            await ctx.send(embed=error_embed("خطأ", "إعدادات رهان العملة غير صالحة."))
            return
        if bet < minimum or bet > maximum:
            await ctx.send(embed=error_embed(
                "رهان غير صالح",
                f"الرهان لازم يكون بين {currency(g['coinflip_min_bet'])} و {currency(g['coinflip_max_bet'])}"
            ))
            return

        if not isinstance(choice, str) or choice.lower() not in {"heads", "tails"}:
            await ctx.send(embed=error_embed("خطأ", "اختر وجه أو كتابة."))
            return
        choice = choice.lower()
        result = random.choice(["heads", "tails"])
        winnings = bet * 2 if choice == result else 0
        outcome = {}

        def play(user):
            try:
                wallet = max(0, int(user.get("wallet", 0)))
                earned = max(0, int(user.get("total_earned", 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if wallet < bet:
                outcome["insufficient"] = True
                return False
            remaining_wallet = wallet - bet
            actual_win = min(winnings, max(0, max_wallet - remaining_wallet))
            user["wallet"] = remaining_wallet + actual_win
            if actual_win:
                user["total_earned"] = earned + actual_win
            outcome["winnings"] = actual_win
            return True

        try:
            atomic_update_user(ctx.author.id, play, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Coinflip transaction failed user=%s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حفظ نتيجة الرهان."))
            return
        if outcome.get("insufficient"):
            await ctx.send(embed=error_embed("رصيد غير كافي", "ما عندك فلوس كافية لهذا الرهان."))
            return
        if outcome.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "بيانات محفظتك غير صالحة."))
            return

        result_ar = "وجه 🪙" if result == "heads" else "كتابة 🎯"

        if choice == result:
            embed = success_embed("ربحت!", f"النتيجة: {result_ar}\nربحت {currency(outcome['winnings'])}")
        else:
            embed = error_embed("خسرت", f"النتيجة: {result_ar}\nخسرت {currency(bet)}")

        logger.info("coinflip user=%s bet=%s winnings=%s", ctx.author.id, bet, outcome["winnings"])
        await ctx.send(embed=embed)

    # -------------------------------------------------------------- بلاك جاك
    @commands.hybrid_command(name="blackjack", aliases=["bj", "بلاك"], description="العب بلاك جاك ضد الديلر")
    @app_commands.describe(bet="مبلغ الرهان")
    @check_gambling_enabled()
    async def blackjack(self, ctx: commands.Context, bet: int):
        settings = load_settings()
        g = settings.get("gambling", {})
        e = settings.get("economy", {})
        try:
            minimum = int(g["blackjack_min_bet"])
            maximum = int(g["blackjack_max_bet"])
            max_wallet = int(e["max_wallet"])
            starting_balance = int(e["starting_balance"])
            if (
                not isinstance(bet, int) or isinstance(bet, bool)
                or min(minimum, maximum, max_wallet) < 0 or minimum > maximum
            ):
                raise ValueError("invalid Blackjack settings")
        except (KeyError, TypeError, ValueError, OverflowError):
            await ctx.send(embed=error_embed("خطأ", "إعدادات البلاك جاك غير صالحة."))
            return
        if bet < minimum or bet > maximum:
            await ctx.send(embed=error_embed(
                "رهان غير صالح",
                f"الرهان لازم يكون بين {currency(minimum)} و {currency(maximum)}"
            ))
            return

        outcome = {}

        def place_bet(user):
            try:
                wallet = max(0, int(user.get("wallet", 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            if wallet < bet:
                outcome["insufficient"] = True
                return False
            user["wallet"] = wallet - bet
            return True

        try:
            atomic_update_user(ctx.author.id, place_bet, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Blackjack stake failed user=%s", ctx.author.id)
            await ctx.send(embed=error_embed("خطأ", "تعذر حجز الرهان."))
            return
        if outcome.get("insufficient"):
            await ctx.send(embed=error_embed("رصيد غير كافي", "ما عندك فلوس كافية لهذا الرهان."))
            return
        if outcome.get("invalid"):
            await ctx.send(embed=error_embed("خطأ", "بيانات محفظتك غير صالحة."))
            return

        view = BlackjackView(ctx, bet, e["max_wallet"])

        # بلاك جاك طبيعي (21 من أول ورقتين) يربح فورًا
        if view.hand_value(view.player) == 21:
            for child in view.children:
                child.disabled = True
            winnings = int(bet * 1.5)
            add_wallet(ctx.author.id, bet + winnings, e["max_wallet"])
            embed = view.build_embed(reveal_dealer=True, result_text=f"🂡 بلاك جاك! ربحت {currency(winnings)}")
            view.finished = True
            await ctx.send(embed=embed, view=view)
            view.stop()
            return

        embed = view.build_embed()
        await ctx.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Gambling(bot))
