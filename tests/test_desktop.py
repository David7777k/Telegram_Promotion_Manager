import os
import unittest
from importlib.util import find_spec
from time import time

from promotion_studio.desktop_service import DesktopService
from promotion_studio.job_queue import claim_due, finish, mark_dispatch, reserve
from promotion_studio.photos import load_photo
from promotion_studio.security import DPAPI
from promotion_studio.storage import open_profile
from tests import test_job_queue
from tests.support import OfflineProfile
from tests.test_photos import PNG


@unittest.skipUnless(os.name == "nt", "Windows profile and DPAPI")
class DesktopServiceTests(unittest.TestCase):
    def test_empty_profile_and_queue_controls(self):
        with OfflineProfile() as profile:
            service = DesktopService(profile)
            service.initialize()
            self.assertEqual(service.status(), {"jobs": [], "subscriptions": []})
            with open_profile(profile) as db:
                draft = test_job_queue.JobQueueTests().prepare(db, profile)
                job = reserve(db, **draft)
            self.assertEqual(service.status()["jobs"][0]["id"], job)
            self.assertEqual(service.change_job(job, "pause"), "paused")
            self.assertEqual(service.status()["jobs"][0]["state"], "paused")
            self.assertEqual(service.change_job(job, "cancel"), "cancelled")

    def test_photo_import_persists(self):
        with OfflineProfile() as profile:
            service = DesktopService(profile)
            service.initialize()
            file = profile / "sample.png"
            file.write_bytes(PNG)
            digest = service.import_photo(file)
            file.unlink()
            with open_profile(profile) as db:
                self.assertEqual(load_photo(db, DPAPI(), digest).content, PNG)

    def test_review_operations_are_local_and_audited(self):
        with OfflineProfile() as profile:
            service = DesktopService(profile)
            with open_profile(profile) as db:
                job = reserve(db, **test_job_queue.JobQueueTests().prepare(db, profile))
                _, lease = claim_due(db, 100)
                mark_dispatch(db, job, lease, 100)
                finish(db, job, lease, 101)
                db.execute("""INSERT INTO subscription_requests(id,campaign_id,bot_account_id,chat_id,
                             telegram_bot_id,price,name,state,started_at)
                             VALUES ('s','campaign','bot',-100123,42,250,'test','pending',100)""")
            self.assertEqual(service.status()["subscriptions"][0]["state"], "needs_review")
            self.assertEqual(service.review_job(job, (-100123, 5, 101)), "sent")
            self.assertEqual(service.close_subscription_review("s"), "closed")
            self.assertEqual(service.status()["subscriptions"][0]["state"], "closed")
            with open_profile(profile) as db:
                events = db.execute("SELECT event_code FROM audit_events ORDER BY id").fetchall()
                self.assertEqual(events, [("manual_reconciliation",), ("manual_subscription_reconciliation",)])


@unittest.skipUnless(find_spec("PySide6") is not None and os.name == "nt", "Requires Windows and PySide6")
class DesktopWindowTests(unittest.TestCase):
    def test_select_job_and_pause_without_transport(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from promotion_studio.desktop import StudioWindow

        app = QApplication.instance() or QApplication([])
        with OfflineProfile() as profile:
            service = DesktopService(profile)
            with open_profile(profile) as db:
                draft = test_job_queue.JobQueueTests().prepare(db, profile)
                draft["scheduled"] = int(time())
                job = reserve(db, **draft)
            window = StudioWindow(service)
            try:
                window.show()
                window.navigate("jobs")
                app.processEvents()
                window.tables["jobs"].selectRow(0)
                self.assertTrue(window.buttons["pause"].isEnabled())
                self.assertTrue(window.action_bars["jobs"].isVisible())
                self.assertFalse(window.buttons["resume"].isVisible())
                window.buttons["pause"].click()
                self.assertEqual(service.status()["jobs"][0]["id"], job)
                self.assertEqual(service.status()["jobs"][0]["state"], "paused")
                self.assertTrue(window.buttons["resume"].isEnabled())
            finally:
                window.close()

    def test_empty_dashboard_and_each_navigation_page(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from promotion_studio.desktop import StudioWindow

        app = QApplication.instance() or QApplication([])
        with OfflineProfile() as profile:
            service = DesktopService(profile)
            service.initialize()
            window = StudioWindow(service)
            try:
                window.show()
                app.processEvents()
                self.assertIs(window.stack.currentWidget(), window.pages["home"])
                self.assertEqual(window.dashboard.cta.text(), "Создать кампанию")
                self.assertEqual(window.dashboard.values["queued"].text(), "0")
                for key in ("campaigns", "catalog", "templates", "accounts", "report", "settings"):
                    window.nav_buttons[key].click()
                    self.assertIs(window.stack.currentWidget(), window.pages[key])
                    self.assertTrue(window.nav_buttons[key].isChecked())
                window.navigate("jobs")
                self.assertIs(window.campaigns.queue_tabs.currentWidget(), window.pages["jobs"])
                self.assertEqual(window.queue_views["jobs"].currentIndex(), 0)
                self.assertFalse(window.action_bars["jobs"].isVisible())
                self.assertEqual(window.panels.report_values["total"].text(), "0")
            finally:
                window.close()

