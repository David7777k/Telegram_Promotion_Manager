import json
import unittest
from dataclasses import replace

from promotion_studio.bot_transport import BotAPITransport
from promotion_studio.catalog import import_catalog, parse_catalog, search_catalog
from promotion_studio.executor import execute_one
from promotion_studio.job_queue import QueueConflict
from promotion_studio.operator_workflow import PublicationDraft, enqueue_draft, register_bot
from promotion_studio.reports import export_report, queue_report
from promotion_studio.security import DPAPI, SecretStore
from promotion_studio.storage import open_profile
from tests import test_bot_transport as http
from tests.support import OfflineProfile


class OperatorWorkflowTests(unittest.TestCase):
    def test_clean_profile_through_approval_dispatch_and_report(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            protector = DPAPI()
            secrets = SecretStore(db, protector)
            identity = http.FakeOpener([http.success({"id": 42, "is_bot": True})])
            bot = register_bot(db, protector, BotAPITransport(secrets, opener=identity), "42:fixture")
            draft = PublicationDraft("Кампания", "Еженедельные заметки", -100111, -100123, bot,
                                     "https://t.me/+Fixture", 250, "Подписка на заметки", 100,
                                     "Подтверждено владельцем площадки", 200, True)
            with self.assertRaises(ValueError):
                enqueue_draft(db, protector, replace(draft, content_confirmed=False), 100)
            job = enqueue_draft(db, protector, draft, 100)
            with self.assertRaises(QueueConflict):
                enqueue_draft(db, protector, draft, 100)
            self.assertEqual(db.execute("SELECT count(*) FROM campaigns").fetchone(), (1,))
            encrypted = db.execute("SELECT payload FROM jobs WHERE id=?", (job,)).fetchone()[0]
            self.assertEqual(json.loads(protector.unprotect(encrypted))["text"], draft.preview)
            opener = http.FakeOpener(http.preflight() + [
                http.success({"chat": {"id": -100123}, "message_id": 17})])
            transport = BotAPITransport(secrets, opener=opener)
            self.assertIsNone(execute_one(db, protector, transport, lambda: 100, job_id="other"))
            self.assertEqual(execute_one(db, protector, transport, lambda: 100, job_id=job), (job, "sent"))
            self.assertEqual(opener.requests[-1][1]["text"], draft.preview)
            self.assertEqual(queue_report(db), {"states": {"sent": 1}, "known_cost_records": 0,
                                                "known_cost_uah_minor": None})
            path = profile / "report.csv"
            export_report(db, path)
            self.assertNotIn("https://", path.read_text(encoding="utf-8-sig"))

    def test_catalog_import_does_not_grant_consent(self):
        candidates, rejected, duplicates = parse_catalog(
            "chat_id,name,category\n-100001,Книги,Тексты\n-100001,Дубль,\nunknown,Ошибка,\n".encode(),
            csv_format=True)
        self.assertEqual((rejected, duplicates), (1, 1))
        with OfflineProfile() as profile, open_profile(profile) as db:
            protector = DPAPI()
            self.assertEqual(import_catalog(db, protector, candidates, 100), 1)
            self.assertEqual(import_catalog(db, protector, candidates, 100), 0)
            self.assertEqual(search_catalog(db, protector, "КНИГ"), [("-100001", "Книги", "Тексты")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM venue_permissions").fetchone(), (0,))

