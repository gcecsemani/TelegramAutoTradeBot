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
        self.assertIn("Lots and slippage", account.text)
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
        self.assertNotIn("Lots and slippage", hidden.text)

        admin = self._login("admin@cca.com", "admin")
        self.assertIn("Ada desk", admin)
        self.assertIn("Ben desk", admin)
        self.assertIn("admin@cca.com", admin)
        opened = self.client.get("/accounts/2")
        self.assertIn("Lots and slippage", opened.text)
        self.assertIn("Ben desk", opened.text)

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
