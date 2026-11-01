"""The morning quote token is written to .env and applied without a restart."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import dotenv_values          # noqa: E402

import config                              # noqa: E402
from core.quote_login import apply_if_changed, save, saved  # noqa: E402


class _Gateway:
    def __init__(self) -> None:
        self.token = ""

    def apply_token(self, access_token: str) -> None:
        self.token = access_token


class QuoteLoginTests(unittest.TestCase):
    def setUp(self):
        self._token = config.ZERODHA_ACCESS_TOKEN
        self._user = config.ZERODHA_USER_ID
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, ".env")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("ZERODHA_API_KEY=key\nZERODHA_API_SECRET=secret\nZERODHA_ACCESS_TOKEN=old\n")

    def tearDown(self):
        config.ZERODHA_ACCESS_TOKEN = self._token
        config.ZERODHA_USER_ID = self._user

    def test_save_records_today_without_returning_the_token_in_status(self):
        save("fresh-token", "AB1234", self.path)
        state = saved(self.path)
        values = dotenv_values(self.path)
        self.assertEqual(values["ZERODHA_ACCESS_TOKEN"], "fresh-token")
        self.assertEqual(values["ZERODHA_USER_ID"], "AB1234")
        self.assertTrue(state["token_today"])
        self.assertEqual(state["user_id"], "AB1234")
        self.assertNotIn("fresh-token", str({k: v for k, v in state.items() if k != "access_token"}))

    def test_a_new_file_token_is_applied_once(self):
        gateway = _Gateway()
        self.assertEqual(apply_if_changed(gateway, "old", self.path), "old")
        self.assertEqual(gateway.token, "")
        save("fresh-token", "AB1234", self.path)
        self.assertEqual(apply_if_changed(gateway, "old", self.path), "fresh-token")
        self.assertEqual(gateway.token, "fresh-token")
        self.assertEqual(apply_if_changed(gateway, "fresh-token", self.path), "fresh-token")


if __name__ == "__main__":
    unittest.main()
