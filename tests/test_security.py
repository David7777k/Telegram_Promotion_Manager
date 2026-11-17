import os
import unittest

from promotion_studio.security import DPAPI, SecretError, SecretStore
from promotion_studio.storage import open_profile
from tests.support import OfflineProfile


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class SecurityTests(unittest.TestCase):
    def test_current_user_round_trip_and_corrupt_ciphertext(self):
        plaintext = b"test-only token/hash/QR canary"
        cipher = DPAPI().protect(plaintext)
        self.assertNotIn(plaintext, cipher)
        self.assertEqual(DPAPI().unprotect(cipher), plaintext)
        with self.assertRaises(SecretError):
            DPAPI().unprotect(cipher[:20])

    def test_secret_store_scope_reopen_and_delete(self):
        canary = b"not-a-real-bot-token"
        with OfflineProfile() as profile:
            with open_profile(profile) as db:
                for account in ("a", "b"):
                    db.execute("INSERT INTO accounts(id,kind,label,status) VALUES (?,'bot',?,'ready')",
                               (account, account))
                store = SecretStore(db, DPAPI())
                store.put("a", "bot_token", canary)
                cipher = db.execute("SELECT ciphertext FROM account_secrets").fetchone()[0]
                db.execute("INSERT INTO account_secrets VALUES ('b','bot_token',?,1)", (cipher,))
                with self.assertRaises(SecretError):
                    store.get("b", "bot_token")
            self.assertNotIn(canary, (profile / "studio.sqlite3").read_bytes())
            with open_profile(profile) as db:
                store = SecretStore(db, DPAPI())
                self.assertEqual(store.get("a", "bot_token"), canary)
                store.delete("a", "bot_token")
                self.assertIsNone(store.get("a", "bot_token"))

