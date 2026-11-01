"""Register, link a broker, and refuse to arm live mode without confirmation."""

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("PORTAL_SECRET", "portal-test-secret")

from cryptography.fernet import Fernet          # noqa: E402

os.environ.setdefault("CREDENTIALS_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient       # noqa: E402

from portal.app import app                      # noqa: E402


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, html[:500]
    return match.group(1)


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["PORTAL_DB"] = os.path.join(self.tmp, "portal.db")
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_register_link_and_require_a_live_confirmation(self):
        page = self.client.get("/register")
        created = self.client.post("/register", data={
            "csrf": _csrf(page.text), "email": "owner@example.com", "password": "password1",
        }, follow_redirects=True)
        self.assertEqual(created.status_code, 200)
        self.assertIn("Link a broker", created.text)

        form = self.client.get("/accounts/new")
        saved = self.client.post("/accounts/new", data={
            "csrf": _csrf(form.text), "label": "Main", "broker": "zerodha",
            "api_key": "key", "api_secret": "secret",
        }, follow_redirects=True)
        self.assertEqual(saved.status_code, 200)
        self.assertIn("Main", saved.text)
        self.assertNotIn('value="secret"', saved.text)
        self.assertNotIn('value="key"', saved.text)

        account = self.client.get("/accounts/1")
        self.assertIn("Paper balance", account.text)
        self.assertNotIn("Save symbol rule", account.text)
        blocked = self.client.post("/accounts/1", data={
            "csrf": _csrf(account.text), "label": "Main", "broker": "zerodha",
            "product": "MIS", "mode": "live", "enabled": "1", "auto_kill": "1",
        }, follow_redirects=False)
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("confirmation", blocked.text)

    def test_admin_sees_every_account_and_a_user_sees_only_their_own(self):
        from execution import store
        from portal.passwords import hash_password

        store.init()
        store.create_user("ada@cca.com", hash_password("password1"))
        store.create_user("ben@cca.com", hash_password("password1"))
        store.create_user("admin@cca.com", hash_password("admin"), role="admin")
        store.create_account(store.user_by_email("ada@cca.com")["id"], "zerodha", "Ada desk", "", "k", "s")
        store.create_account(store.user_by_email("ben@cca.com")["id"], "zerodha", "Ben desk", "", "k", "s")

        ada = self._login("ada@cca.com", "password1")
        self.assertIn("Ada desk", ada)
        self.assertNotIn("Ben desk", ada)
        hidden = self.client.get("/accounts/2", follow_redirects=True)
        self.assertNotIn("Paper balance", hidden.text)

        admin = self._login("admin@cca.com", "admin")
        self.assertIn("Ada desk", admin)
        self.assertIn("Ben desk", admin)
        self.assertIn("admin@cca.com", admin)
        opened = self.client.get("/accounts/2")
        self.assertIn("Paper balance", opened.text)
        self.assertNotIn("Save symbol rule", opened.text)
        self.assertIn("Quote login", self.client.get("/dashboard").text)
        quote = self.client.get("/quote")
        self.assertIn("Save today's quote token", quote.text)
        config_page = self.client.get("/config")
        self.assertIn("Rupees for one lot", config_page.text)
        self.assertIn("BANKNIFTY", config_page.text)
        saved = self.client.post("/config/margin", data={
            "csrf": _csrf(config_page.text), "symbol": "CRUDEOIL", "rupees_per_lot": "150000",
        }, follow_redirects=True)
        self.assertIn("CRUDEOIL", saved.text)
        self.assertIn("150,000", saved.text)
        slipped = self.client.post("/config/slippage", data={
            "csrf": _csrf(saved.text), "symbol": "NIFTY", "slippage_mode": "POINTS", "slippage_value": "1.5",
        }, follow_redirects=True)
        self.assertIn("1.5 points", slipped.text)
        kept = self.client.post("/config/margin/delete", data={
            "csrf": _csrf(slipped.text), "symbol": "*",
        }, follow_redirects=True)
        self.assertIn("default row stays", kept.text)
        self._login("ada@cca.com", "password1")
        hidden = self.client.get("/quote", follow_redirects=True)
        self.assertIn("Only the admin", hidden.text)
        self.assertNotIn("Save today's quote token", hidden.text)
        self.assertIn("Ben desk", opened.text)
        blocked = self.client.get("/config", follow_redirects=True)
        self.assertIn("Only the admin", blocked.text)
        self.assertNotIn("Save lot amount", blocked.text)

    def test_a_new_api_key_clears_the_saved_login(self):
        from execution import store
        from portal.passwords import hash_password

        store.init()
        owner = store.create_user("ada@cca.com", hash_password("password1"))
        account_id = store.create_account(owner, "zerodha", "Ada desk", "AB1", "k", "s")
        store.set_token(account_id, "old-token", "AB1")
        self._login("ada@cca.com", "password1")
        page = self.client.get(f"/accounts/{account_id}")
        saved = self.client.post(f"/accounts/{account_id}", data={
            "csrf": _csrf(page.text), "label": "Ada desk", "broker": "zerodha",
            "client_id": "AB1", "product": "MIS", "mode": "paper", "enabled": "1",
            "auto_kill": "1", "paper_balance": "100000", "api_key": "new-key",
        }, follow_redirects=True)
        self.assertIn("Daily login", saved.text)
        fresh = store.get_account(account_id)
        self.assertEqual(fresh["access_token_enc"], "")
        self.assertEqual(fresh["token_date"], "")

    def test_admin_can_remove_an_account_unless_lots_are_open(self):
        from execution import store
        from portal.passwords import hash_password

        store.init()
        store.create_user("admin@cca.com", hash_password("admin"), role="admin")
        owner = store.create_user("ada@cca.com", hash_password("password1"))
        account_id = store.create_account(owner, "zerodha", "Ada desk", "", "k", "s")
        self._login("admin@cca.com", "admin")
        page = self.client.get(f"/accounts/{account_id}")
        self.assertIn("Remove account", page.text)

        store.apply_fill(
            account_id, "pos-1", side="BUY", lots=1, quantity=65, price=100,
            multiplier=65, tradingsymbol="NIFTY", exchange="NFO", product="MIS",
        )
        blocked = self.client.post(f"/accounts/{account_id}/delete", data={
            "csrf": _csrf(page.text),
        }, follow_redirects=True)
        self.assertIn("Flatten the open lots", blocked.text)
        self.assertIsNotNone(store.get_account(account_id))

        store.apply_fill(
            account_id, "pos-1", side="SELL", lots=1, quantity=65, price=90,
            multiplier=65, tradingsymbol="NIFTY", exchange="NFO", product="MIS",
        )
        page = self.client.get(f"/accounts/{account_id}")
        removed = self.client.post(f"/accounts/{account_id}/delete", data={
            "csrf": _csrf(page.text),
        }, follow_redirects=True)
        self.assertIn("Account removed", removed.text)
        self.assertNotIn("Ada desk", removed.text)
        self.assertIsNone(store.get_account(account_id))

    def _login(self, email: str, password: str) -> str:
        self.client.post("/logout", data={"csrf": _csrf(self.client.get("/dashboard").text)})
        page = self.client.get("/login")
        logged = self.client.post("/login", data={
            "csrf": _csrf(page.text), "email": email, "password": password,
        }, follow_redirects=True)
        self.assertEqual(logged.status_code, 200, logged.text[:400])
        return logged.text


if __name__ == "__main__":
    unittest.main()
