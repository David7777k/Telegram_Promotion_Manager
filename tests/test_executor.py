import json
import os
import unittest

from promotion_studio.executor import Access, TransportUnavailable, execute_one
from promotion_studio.job_queue import reserve
from promotion_studio.security import DPAPI
from promotion_studio.storage import open_profile
from tests import test_job_queue
from tests.support import OfflineProfile


class FakeTransport:
    def __init__(self, db, outcome=42, access=Access(-100123, "channel", 42, True), on_inspect=None):
        self.db, self.outcome, self.access = db, outcome, access
        self.on_inspect = on_inspect
        self.sends = []

    def inspect(self, bot, chat, thread):
        if self.on_inspect:
            self.on_inspect()
        return self.access

    def send(self, *args):
        state, intent = self.db.execute("""SELECT state,intent_at FROM jobs
                                          JOIN job_attempts USING(lease_id)""").fetchone()
        if state != "dispatching" or intent != 100:
            raise AssertionError("Transport ran without durable intent")
        self.sends.append(args)
        if self.outcome is None:
            raise TransportUnavailable()
        return self.outcome


@unittest.skipUnless(os.name == "nt", "Windows profile and DPAPI")
class ExecutorTests(unittest.TestCase):
    def prepare(self, db, profile, **snapshot_options):
        draft = test_job_queue.JobQueueTests().prepare(db, profile)
        db.execute("UPDATE accounts SET telegram_user_id=42 WHERE id='bot'")
        db.execute("""INSERT INTO venue_permissions(id,venue_id,chat_id,bot_account_id,thread_id,
                     valid_from,expires_at,revoked_at) VALUES ('permission','venue',-100123,'bot',NULL,90,110,NULL)""")
        offer = dict(link="https://t.me/+Example", price_stars=250, period_days=30, content_confirmed=True)
        snapshot = dict(client_key=draft["client_key"], permission_id="permission", offer=offer,
                        text="Подписка: 250 Stars каждые 30 дней.", button_url=offer["link"])
        snapshot.update(snapshot_options)
        draft["encrypted_payload"] = DPAPI().protect(json.dumps(snapshot).encode())
        return reserve(db, **draft)

    def test_success_or_lost_response(self):
        for outcome, expected in ((42, "sent"), (None, "needs_review"), (False, "needs_review")):
            with self.subTest(outcome=outcome), OfflineProfile() as profile, open_profile(profile) as db:
                job = self.prepare(db, profile)
                gateway = FakeTransport(db, outcome)
                self.assertEqual(execute_one(db, DPAPI(), gateway, lambda: 100), (job, expected))
                self.assertEqual(len(gateway.sends), 1)
                self.assertIsNone(execute_one(db, DPAPI(), gateway, lambda: 100))

    def test_revoked_during_rights_check_pauses_before_intent(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            job = self.prepare(db, profile)
            gateway = FakeTransport(db, on_inspect=lambda: db.execute(
                "UPDATE venue_permissions SET revoked_at=100 WHERE id='permission'"))
            self.assertEqual(execute_one(db, DPAPI(), gateway, lambda: 100), (job, "paused"))
            self.assertEqual(gateway.sends, [])
            self.assertEqual(db.execute("SELECT intent_at,reason_code FROM job_attempts").fetchone(),
                             (None, "missing_consent"))

    def test_wrong_bot_and_missing_rights(self):
        for access, reason in ((Access(-100123, "channel", 99, True), "identity_mismatch"),
                               (Access(-100123, "channel", 42, False), "missing_publish_rights")):
            with self.subTest(reason=reason), OfflineProfile() as profile, open_profile(profile) as db:
                job = self.prepare(db, profile)
                gateway = FakeTransport(db, access=access)
                self.assertEqual(execute_one(db, DPAPI(), gateway, lambda: 100), (job, "paused"))
                self.assertEqual(gateway.sends, [])
                self.assertEqual(db.execute("SELECT reason_code FROM job_attempts").fetchone(), (reason,))

    def test_recent_publication_blocks_a_previously_reserved_job(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            job = self.prepare(db, profile)

            def record_other_publication():
                db.execute("""INSERT INTO jobs(id,campaign_id,venue_id,template_id,state,scheduled_utc,
                              sent_at,payload,canonical_chat_id,bot_account_id,client_key,cooldown_seconds)
                              SELECT 'earlier',campaign_id,venue_id,template_id,'sent',60,95,payload,
                                     canonical_chat_id,bot_account_id,'earlier',30 FROM jobs WHERE id=?""", (job,))

            gateway = FakeTransport(db, on_inspect=record_other_publication)
            self.assertEqual(execute_one(db, DPAPI(), gateway, lambda: 100), (job, "paused"))
            self.assertEqual(gateway.sends, [])
            self.assertEqual(db.execute("SELECT reason_code FROM job_attempts").fetchone(),
                             ("conflicting_publication",))

    def test_window_closes_during_rights_check(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            job = self.prepare(db, profile)
            db.execute("UPDATE venue_permissions SET timezone_name='UTC',expires_at=200")
            db.execute("INSERT INTO venue_permission_windows VALUES ('permission',3,1,2)")
            ticks = iter((100, 100, 120, 120))
            gateway = FakeTransport(db)
            self.assertEqual(execute_one(db, DPAPI(), gateway, lambda: next(ticks)), (job, "paused"))
            self.assertEqual(gateway.sends, [])
            self.assertEqual(db.execute("SELECT reason_code FROM job_attempts").fetchone(),
                             ("outside_allowed_window",))

    def test_active_window_allows_dispatch(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            job = self.prepare(db, profile)
            db.execute("UPDATE venue_permissions SET timezone_name='UTC'")
            db.execute("INSERT INTO venue_permission_windows VALUES ('permission',3,1,2)")
            self.assertEqual(execute_one(db, DPAPI(), FakeTransport(db), lambda: 100), (job, "sent"))

    def test_unknown_timezone_blocks_dispatch(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            job = self.prepare(db, profile)
            db.execute("UPDATE venue_permissions SET timezone_name='Invalid/Fixture'")
            db.execute("INSERT INTO venue_permission_windows VALUES ('permission',3,1,2)")
            gateway = FakeTransport(db)
            self.assertEqual(execute_one(db, DPAPI(), gateway, lambda: 100), (job, "paused"))
            self.assertEqual(gateway.sends, [])
            self.assertEqual(db.execute("SELECT reason_code FROM job_attempts").fetchone(),
                             ("timezone_unavailable",))

