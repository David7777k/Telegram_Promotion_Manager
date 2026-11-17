import json
import os
import unittest
from pathlib import Path

from promotion_studio.backup import create_backup, restore_backup
from promotion_studio.job_queue import (QueueConflict, claim_due, clear_indefinite_hold, control_job,
                                        finish, mark_dispatch, reconcile, recover, reserve)
from promotion_studio.legacy_archive import stage_legacy
from promotion_studio.legacy_import import import_archived
from promotion_studio.security import DPAPI
from promotion_studio.storage import open_profile
from tests.support import OfflineProfile


@unittest.skipUnless(os.name == "nt", "Windows profile and DPAPI")
class JobQueueTests(unittest.TestCase):
    def test_manual_review_requires_matching_chat_and_does_not_requeue(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            draft = self.prepare(db, profile)
            job = reserve(db, **draft)
            _, lease = claim_due(db, 100)
            mark_dispatch(db, job, lease, 100)
            finish(db, job, lease, 101)
            with self.assertRaisesRegex(QueueConflict, "known chat"):
                reconcile(db, job, 105, receipt=(-100456, 7, 103))
            with self.assertRaisesRegex(QueueConflict, "predates"):
                reconcile(db, job, 105, receipt=(-100123, 7, 99))
            self.assertEqual(reconcile(db, job, 105, receipt=(-100123, 7, 103)), "sent")
            self.assertEqual(db.execute("SELECT sent_at,telegram_message_id FROM jobs WHERE id=?",
                                        (job,)).fetchone(), (103, 7))
            with self.assertRaises(QueueConflict):
                reconcile(db, job, 106)
            self.assertIsNone(claim_due(db, 106))
            self.assertEqual(db.execute("SELECT event_code FROM audit_events").fetchone(),
                             ("manual_reconciliation",))

    def test_close_review_and_clear_only_indefinite_hold(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            job = reserve(db, **self.prepare(db, profile))
            _, lease = claim_due(db, 100)
            mark_dispatch(db, job, lease, 100)
            finish(db, job, lease, 101)
            self.assertEqual(reconcile(db, job, 103), "cancelled")
            db.execute("INSERT INTO bot_holds VALUES ('bot',120,100)")
            with self.assertRaises(QueueConflict):
                clear_indefinite_hold(db, "bot", 103)
            db.execute("UPDATE bot_holds SET until_utc=NULL")
            clear_indefinite_hold(db, "bot", 104)
            self.assertIsNone(db.execute("SELECT 1 FROM bot_holds").fetchone())
            self.assertIsNone(claim_due(db, 104))
            self.assertEqual(db.execute("SELECT event_code FROM audit_events ORDER BY id").fetchall(),
                             [("manual_reconciliation",), ("manual_hold_clear",)])

    def test_manual_controls_obey_hold_and_preserve_cancelled_state(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            job = reserve(db, **self.prepare(db, profile))
            self.assertEqual(control_job(db, job, "pause", 100), "paused")
            self.assertIsNone(claim_due(db, 100))
            db.execute("INSERT INTO bot_holds VALUES ('bot',110,100)")
            with self.assertRaisesRegex(QueueConflict, "hold"):
                control_job(db, job, "resume", 109)
            self.assertEqual(control_job(db, job, "resume", 110), "queued")
            self.assertEqual(control_job(db, job, "cancel", 110), "cancelled")
            self.assertIsNone(claim_due(db, 110))
            with self.assertRaises(QueueConflict):
                control_job(db, job, "resume", 110)

    def prepare(self, db, profile):
        original = Path(__file__).parent / "fixtures" / "v2.json"
        state = json.loads(original.read_text(encoding="utf-8"))
        state["posts"] = []
        fixture = profile / "synthetic.json"
        fixture.write_text(json.dumps(state), encoding="utf-8")
        protector = DPAPI()
        run = stage_legacy(db, fixture, 2, "UTC", protector)
        import_archived(db, run.run_id, protector)
        db.execute("INSERT INTO accounts(id,kind,label,status) VALUES ('bot','bot','bot','ready')")
        return dict(client_key="request-1", campaign_id="campaign", venue_id="venue",
                    template_id="template", chat_id=-100123, bot_id="bot", scheduled=100,
                    cooldown=30, encrypted_payload=protector.protect(b"snapshot"), price=250)

    def test_restart_after_intent_never_requeues(self):
        with OfflineProfile() as profile:
            with open_profile(profile) as db:
                draft = self.prepare(db, profile)
                job = reserve(db, **draft)
                self.assertEqual(reserve(db, **draft), job)
                with self.assertRaises(QueueConflict):
                    reserve(db, **(draft | {"client_key": "request-2", "scheduled": 110}))
                self.assertIsNone(claim_due(db, 99))
                _, lease = claim_due(db, 100)
                mark_dispatch(db, job, lease, 100)
            with open_profile(profile) as db:
                self.assertEqual(recover(db, 101, restart=True), ((job, "needs_review"),))
                self.assertIsNone(claim_due(db, 101))
                with self.assertRaises(QueueConflict):
                    finish(db, job, lease, 101, receipt=4)

    def test_claim_expiry_before_intent_and_confirmed_receipt(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            job = reserve(db, **self.prepare(db, profile))
            _, expired = claim_due(db, 100, lease_seconds=1)
            self.assertEqual(recover(db, 101), ((job, "queued"),))
            _, current = claim_due(db, 101)
            with self.assertRaises(QueueConflict):
                mark_dispatch(db, job, expired, 101)
            mark_dispatch(db, job, current, 101)
            finish(db, job, current, 102, receipt=5)
            self.assertEqual(db.execute("SELECT state,telegram_message_id FROM jobs WHERE id=?",
                                        (job,)).fetchone(), ("sent", 5))
            self.assertEqual(db.execute("SELECT intent_at,outcome FROM job_attempts ORDER BY claimed_at").fetchall(),
                             [(None, "queued"), (101, "sent")])

    def test_restored_inflight_job_requires_review(self):
        with OfflineProfile() as root:
            source, restored, backup = root / "src", root / "new", root / "snapshot.sqlite3"
            with open_profile(source) as db:
                job = reserve(db, **self.prepare(db, source))
                _, lease = claim_due(db, 100)
                mark_dispatch(db, job, lease, 100)
            create_backup(source, backup)
            restore_backup(backup, restored)
            with open_profile(restored) as db:
                self.assertIsNone(claim_due(db, 101))
                self.assertEqual(db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone(),
                                 ("needs_review",))

