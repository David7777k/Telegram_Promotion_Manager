import base64
from email import policy
from email.parser import BytesParser
import unittest

from promotion_studio.backup import create_backup, restore_backup
from promotion_studio.bot_transport import BotAPITransport
from promotion_studio.executor import execute_one
from promotion_studio.photos import PhotoError, load_photo, store_photo
from promotion_studio.security import DPAPI
from promotion_studio.storage import open_profile
from tests import test_bot_transport as http
from tests import test_executor
from tests.support import OfflineProfile


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jF1sAAAAASUVORK5CYII=")


class PhotoTests(unittest.TestCase):
    def prepare_photo(self, db, directory):
        file = directory / "original.png"
        file.write_bytes(PNG)
        digest = store_photo(db, DPAPI(), file)
        self.assertEqual(store_photo(db, DPAPI(), file), digest)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM publication_photos").fetchone(), (1,))
        file.write_bytes(b"changed")
        with self.assertRaises(PhotoError):
            store_photo(db, DPAPI(), file)
        file.unlink()
        return digest

    def test_photo_snapshot_sends_one_multipart_request(self):
        with OfflineProfile() as root, open_profile(root) as db:
            digest = self.prepare_photo(db, root)
            job = test_executor.ExecutorTests().prepare(db, root, photo_digest=digest)
            db.execute("UPDATE venue_permissions SET allows_photo=1")
            opener = http.FakeOpener(http.preflight() + [
                http.success({"chat": {"id": -100123}, "message_id": 9})])
            transport = BotAPITransport(http.Secrets(), opener=opener)
            self.assertEqual(execute_one(db, DPAPI(), transport, lambda: 100), (job, "sent"))
            self.assertEqual([method for method, _ in opener.requests].count("sendPhoto"), 1)
            self.assertNotIn("sendMessage", [method for method, _ in opener.requests])
            method, (content_type, body) = opener.requests[-1]
            envelope = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\n\r\n".encode() + body)
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                      for part in envelope.iter_parts()}
            self.assertEqual(fields["photo"], PNG)
            self.assertIn("250 Stars", fields["caption"].decode())
            self.assertIn(b"https://t.me/+Example", fields["reply_markup"])

    def test_photo_consent_and_chat_media_rights_are_independent(self):
        for permit, reason in ((0, "missing_photo_consent"), (1, "missing_photo_rights")):
            with self.subTest(permit=permit), OfflineProfile() as root, open_profile(root) as db:
                digest = self.prepare_photo(db, root)
                job = test_executor.ExecutorTests().prepare(db, root, photo_digest=digest)
                db.execute("UPDATE venue_permissions SET allows_photo=?", (permit,))
                replies = http.preflight()
                replies[1] = http.success({"id": -100123, "type": "supergroup",
                                          "permissions": {"can_send_messages": True, "can_send_photos": False}})
                replies[2] = http.success({"status": "member", "user": {"id": 42}})
                opener = http.FakeOpener(replies)
                self.assertEqual(execute_one(db, DPAPI(), BotAPITransport(http.Secrets(), opener=opener),
                                             lambda: 100), (job, "paused"))
                self.assertEqual(len(opener.requests), 3)
                self.assertEqual(db.execute("SELECT reason_code FROM job_attempts").fetchone(), (reason,))

    def test_backup_restores_encrypted_photo_bytes(self):
        with OfflineProfile() as root:
            source, restored, backup = root / "source", root / "restored", root / "backup.sqlite3"
            with open_profile(source) as db:
                digest = self.prepare_photo(db, source)
            create_backup(source, backup)
            self.assertNotIn(PNG, backup.read_bytes())
            restore_backup(backup, restored)
            with open_profile(restored) as db:
                self.assertEqual(load_photo(db, DPAPI(), digest).content, PNG)
                db.execute("UPDATE publication_photos SET size=size+1")
                with self.assertRaises(PhotoError):
                    load_photo(db, DPAPI(), digest)

