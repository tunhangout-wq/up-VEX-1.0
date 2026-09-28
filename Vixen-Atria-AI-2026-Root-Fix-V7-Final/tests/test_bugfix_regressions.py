"""
Regression tests for the crash reported in the traceback:

    NameError: name 'List' is not defined

and two related, previously-latent NameErrors that were only reachable at
runtime (not at import time):

  * bot/cogs/loans.py  -> LoanView.decline() called save_loans() without
                           importing it.
  * bot/cogs/rob.py    -> Rob.robstats() called get_user() without
                           importing it.

Each test below exercises the exact code path that used to raise, using a
temporary data directory so the real bot/data/*.json files are untouched.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.utils import data_manager


class DataManagerTypingTests(unittest.TestCase):
    """bot/utils/data_manager.py used List in a type hint without importing it."""

    def test_list_is_importable_and_usable_as_a_type_hint(self):
        # The bug manifested as a NameError raised the moment the module was
        # imported/executed (function annotations are evaluated eagerly at
        # def-time), which is why `python3 -m bot.main` crashed before doing
        # anything else. Merely importing the module successfully is the
        # regression check.
        self.assertTrue(hasattr(data_manager, "List"))
        self.assertTrue(callable(data_manager.atomic_update_users))

    def test_atomic_update_users_accepts_a_list_of_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            users_file = Path(directory) / "users.json"
            with patch.object(data_manager, "USERS_FILE", users_file):
                result = data_manager.atomic_update_users(
                    [111, 222],
                    lambda users: [u.__setitem__("wallet", u.get("wallet", 0) + 50) for u in users.values()],
                    starting_balance=100,
                )
        self.assertEqual(result["111"]["wallet"], 150)
        self.assertEqual(result["222"]["wallet"], 150)


class LoansSaveLoansImportTests(unittest.IsolatedAsyncioTestCase):
    """bot/cogs/loans.py: LoanView.decline() previously raised
    NameError: name 'save_loans' is not defined as soon as a borrower
    clicked the "reject" button, because save_loans was never imported."""

    async def test_decline_button_persists_declined_status(self):
        from bot.cogs import loans as loans_module

        with tempfile.TemporaryDirectory() as directory:
            loans_file = Path(directory) / "loans.json"
            seed = {
                "next_id": 2,
                "loans": [
                    {
                        "id": 1,
                        "lender": "10",
                        "borrower": "20",
                        "amount": 500,
                        "status": "pending",
                        "due": 0,
                    }
                ],
            }
            with patch.object(data_manager, "LOANS_FILE", loans_file):
                data_manager.save_loans(seed)

                view = loans_module.LoanView(cog=SimpleNamespace(), loan_id=1, borrower_id=20)
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=20),
                    response=SimpleNamespace(
                        send_message=AsyncMock(),
                        edit_message=AsyncMock(),
                    ),
                )

                # This call used to raise NameError: name 'save_loans' is not defined.
                await view.decline.callback(interaction)

                persisted = data_manager.load_loans()

        interaction.response.edit_message.assert_awaited_once()
        self.assertEqual(persisted["loans"][0]["status"], "declined")


class RobGetUserImportTests(unittest.IsolatedAsyncioTestCase):
    """bot/cogs/rob.py: Rob.robstats() previously raised
    NameError: name 'get_user' is not defined on every /robstats call."""

    async def test_robstats_command_runs_without_nameerror(self):
        from bot.cogs import rob as rob_module

        with tempfile.TemporaryDirectory() as directory:
            users_file = Path(directory) / "users.json"
            settings_file = Path(directory) / "settings.json"
            with patch.object(data_manager, "USERS_FILE", users_file), patch.object(
                data_manager, "SETTINGS_FILE", settings_file
            ):
                cog = rob_module.Rob(bot=SimpleNamespace())
                ctx = SimpleNamespace(
                    author=SimpleNamespace(id=42),
                    send=AsyncMock(),
                )

                # This call used to raise NameError: name 'get_user' is not defined.
                await cog.robstats.callback(cog, ctx)

        ctx.send.assert_awaited_once()
        sent_embed = ctx.send.await_args.kwargs["embed"]
        self.assertIn("إحصائيات سرقاتك", sent_embed.title)


if __name__ == "__main__":
    unittest.main()
