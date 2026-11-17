"""Offline verification of account, catalog, campaign and scheduler boundaries."""
import asyncio
import os
import unittest
from dataclasses import dataclass
from importlib.util import find_spec
from threading import Event
from time import time
from unittest.mock import patch
from uuid import uuid4

from promotion_studio.campaigns import render
from promotion_studio.desktop_service import DesktopService
from promotion_studio.executor import Access, TransportRejected, TransportUnavailable
from promotion_studio.job_queue import QueueConflict
from promotion_studio.storage import open_profile
from promotion_studio.telegram_accounts import Chat, Identity, LoginError, login
from tests.support import OfflineProfile


@dataclass
class FakeSender:
    identity: int
    calls: list
    outcome: object = None

    def identify(self):
        return Identity(self.identity, f"Профиль {self.identity}", "studio_user")

    def dialogs(self):
        return [Chat(-100100, "Группа", "allowed_group", "supergroup", 12)]

    def resolve(self, reference):
        return self.dialogs()[0]

    def inspect(self, account, chat, topic):
        self.calls.append(("inspect", self.identity, chat))
        return Access(chat, "supergroup", self.identity, chat != -100666, chat != -100666)

    def send(self, account, chat, topic, text, button, **kwargs):
        self.calls.append(("send", self.identity, chat, text, button))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return 42


class FakeBot:
    def identity(self, token):
        return {"id": 9, "first_name": "Бот", "username": "studio_test_bot"}

    def account_identity(self, account):
        return self.identity(None)

    def __init__(self, store, calls):
        self.calls = calls
        self.outcome = None

    def inspect(self, account, chat, topic):
        return Access(chat, "channel", 9, True, True)

    def send(self, account, chat, topic, text, button, **kwargs):
        self.calls.append(("send", 9, chat, text, button))
        return 43


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class RedesignWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.profile_context = OfflineProfile()
        self.profile = self.profile_context.__enter__()
        self.addCleanup(self.profile_context.__exit__, None, None, None)
        self.calls = []
        self.outcome = None

        def fake_login(api_id, api_hash, method, phone, prompt, present, **options):
            return Identity(api_id, f"Аккаунт {api_id}", f"user{api_id}"), "synthetic-session"

        self.service = DesktopService(self.profile, login_flow=fake_login,
                                      user_factory=lambda _, __, ___, i: FakeSender(i, self.calls, self.outcome),
                                      bot_factory=lambda store: FakeBot(store, self.calls))
        self.service.initialize()

    def user(self, number=101):
        return self.service.add_user(number, "b" * 32, "qr", "", None, None)

    def venue(self, number=-100100):
        self.service.save_chat_selection(None, [(Chat(number, f"Площадка {number}", None, "supergroup"), "")])
        venue = next(row for row in self.service.venue_rows() if row["chat"] == number)
        self.service.set_venue(venue["id"], decision="allowed", category="Тест", favorite=True, excluded=False)
        return venue["id"]

    def target(self, venue, account, *, when=None, repeats=1):
        now = int(time())
        return dict(venue=venue, account=account, note="Согласование тестовой площадки",
                    when=when or now, until=now + 3600, interval=5, repeats=repeats)

    def data(self):
        return dict(title="Тестовая кампания", text="Текст для согласованной площадки",
                    link="https://t.me/studio_example", button=True, confirmed=True)

    def test_account_sync_link_and_duplicate(self):
        user = self.user()
        second = self.user(102)
        self.assertNotEqual(user, second)
        self.assertEqual(user, self.user())
        self.assertTrue(self.service.check_account(user))
        chats = self.service.live_chats(user)
        self.service.save_chat_selection(user, [(chat, "") for chat in chats], member=True)
        resolved, warnings = self.service.resolve_chats(
            user, [("https://t.me/allowed_group", "", "официальная"), ("@allowed_group", "", ""),
                   ("unusable?!", "", "")])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(warnings), 2)
        self.service.save_chat_selection(user, resolved)
        self.assertEqual(len(self.service.venue_rows()), 1)
        row = next(a for a in self.service.account_rows() if a["id"] == user)
        self.assertEqual(row["venues"], 1)
        self.assertIsNotNone(row["synced"])
        db = (self.profile / "studio.sqlite3").read_bytes()
        self.assertNotIn(b"synthetic-session", db)
        self.service.disable_account(user, erase=True)
        with open_profile(self.profile) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM account_secrets WHERE account_id=?", (user,)).fetchone(),
                             (0,))
        self.assertFalse(list(self.profile.glob("*.session")))

    def test_multiple_destinations_and_explicit_sender(self):
        user = self.user()
        bot = self.service.connect_bot("9:fake")
        draft = uuid4().hex
        targets = (self.target(self.venue(), user), self.target(self.venue(-100200), bot))
        self.assertEqual(self.service.approve_campaign(draft, self.data(), targets, False, preflight_only=True), 2)
        self.assertFalse(self.service.status()["jobs"])
        jobs = self.service.approve_campaign(draft, self.data(), targets, False)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(set(jobs), set(self.service.approve_campaign(draft, self.data(), targets, False)))
        self.assertIsNone(self.service.dispatch(auto_only=True))
        self.assertEqual(self.service.dispatch(jobs[0])[1], "sent")
        self.assertEqual(self.service.dispatch(jobs[1])[1], "sent")
        sent = [c for c in self.calls if c[0] == "send"]
        self.assertEqual(sent[0][1:3], (101, -100100))
        self.assertIn(self.data()["link"], sent[0][3])
        self.assertIsNone(sent[0][4])
        self.assertEqual(sent[1][1:3], (9, -100200))
        self.assertEqual(sent[1][4], self.data()["link"])
        self.assertEqual({r["state"] for r in self.service.status()["jobs"]}, {"sent"})

    def test_preflight_rolls_back_on_denied_target(self):
        targets = (self.target(self.venue(), self.user()), self.target(self.venue(-100666), self.user(102)))
        with self.assertRaisesRegex(QueueConflict, "Недостаточно прав"):
            self.service.approve_campaign(uuid4().hex, self.data(), targets, True)
        self.assertFalse(self.service.status()["jobs"])
        self.assertFalse(self.service.campaign_list())

    def test_auto_opt_in_and_unknown_result_survive_restart(self):
        draft = uuid4().hex
        job, future = self.service.approve_campaign(
            draft, self.data(), (self.target(self.venue(), self.user(), repeats=2),), True)
        stopped = Event()
        stopped.set()
        self.assertIsNone(self.service.dispatch(auto_only=True, stop=stopped))
        stopped.clear()
        self.outcome = TransportUnavailable("synthetic timeout")
        self.assertEqual(self.service.dispatch(auto_only=True, stop=stopped), (job, "needs_review"))
        self.service.initialize()
        self.assertIsNone(self.service.dispatch(auto_only=True))
        result = {r["id"]: r for r in self.service.status()["jobs"]}
        self.assertEqual(result[job]["state"], "needs_review")
        self.assertTrue(result[future]["auto"])
        self.assertEqual(self.service.pause_automatic(), 1)
        self.assertEqual(len([call for call in self.calls if call[0] == "send"]), 1)
        self.assertEqual(self.service.review_job(job, (-100100, 981, int(time()))), "sent")
        self.assertEqual(self.service.load_campaign(draft)[1], draft)

    def test_flood_wait_never_switches_sender(self):
        selected = self.user()
        self.user(102)
        job, = self.service.approve_campaign(uuid4().hex, self.data(),
                                             (self.target(self.venue(), selected),), True)
        self.outcome = TransportRejected(rate_limited=True, retry_after=60)
        self.assertEqual(self.service.dispatch(auto_only=True), (job, "paused"))
        with self.assertRaises(QueueConflict):
            self.service.change_job(job, "resume")
        self.assertEqual([c[1] for c in self.calls if c[0] == "send"], [101])

    def test_revoked_destination_stops_automatic(self):
        venue = self.venue()
        job, = self.service.approve_campaign(uuid4().hex, self.data(), (self.target(venue, self.user()),), True)
        self.service.set_venue(venue, decision="review", favorite=False, excluded=False, category="")
        self.assertIsNone(self.service.dispatch(auto_only=True))
        with open_profile(self.profile) as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone(), ("paused",))
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())

    def test_auto_repeat_waits_for_interval_since_actual_send(self):
        job, repeat = self.service.approve_campaign(
            uuid4().hex, self.data(), (self.target(self.venue(), self.user(), repeats=2),), True)
        self.assertEqual(self.service.dispatch(auto_only=True), (job, "sent"))
        now = int(time())
        with open_profile(self.profile) as db:
            db.execute("UPDATE jobs SET scheduled_utc=? WHERE id=?", (now, repeat))
        self.assertIsNone(self.service.dispatch(auto_only=True))
        self.assertEqual(next(j["state"] for j in self.service.status()["jobs"] if j["id"] == repeat), "queued")
        with open_profile(self.profile) as db:
            db.execute("UPDATE jobs SET sent_at=? WHERE id=?", (now - 301, job))
        self.outcome = TransportRejected(status=403)
        self.assertEqual(self.service.dispatch(auto_only=True), (repeat, "paused"))

    def test_template_versions_and_render(self):
        template = self.service.save_template(None, "Объявление", "{Привет|Здравствуйте}")
        self.service.save_template(template, "Объявление", "{Новый|Ещё} {{campaign_name}}")
        self.assertEqual([v[0] for v in self.service.template_versions(template)], [2, 1])
        text, button, offer = render(dict(self.data(), spintax=True, text="{Новый|Ещё} {{campaign_name}}"), "user")
        self.assertIn(self.data()["title"], text)
        self.assertIsNone(button)
        self.assertIsNone(offer)
        self.service.archive_template(template)
        self.assertFalse(self.service.template_list())
        self.assertEqual(len(self.service.template_versions(template)), 2)

    @unittest.skipUnless(find_spec("PySide6"), "Requires Qt")
    def test_editor_to_queue_using_selected_user_and_bot(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication
        from promotion_studio.desktop import StudioWindow

        app = QApplication.instance() or QApplication([])
        user, bot = self.user(), self.service.connect_bot("9:fake")
        first, second = self.venue(), self.venue(-100200)
        window = StudioWindow(self.service)
        window.show()
        loop = QEventLoop()
        try:
            with patch("promotion_studio.desktop_campaigns.confirm", return_value=True):
                window.navigate("campaigns")
                form = window.campaigns
                form.title.setText(self.data()["title"])
                form.text.setPlainText(self.data()["text"])
                form.link.setText(self.data()["link"])
                form.confirmed.setChecked(True)
                form.auto.setChecked(True)
                form.add_venues(self.service.venue_rows())
                form.targets[first] = self.target(first, user)
                form.targets[second] = self.target(second, bot)
                form.approve()
                window.worker.finished.connect(loop.quit)
                timer = QTimer()
                timer.setSingleShot(True)
                timer.timeout.connect(loop.quit)
                timer.start(4000)
                loop.exec()
                app.processEvents()
                self.assertTrue(form.queued)
                self.assertEqual(window.tables["jobs"].rowCount(), 2)
                self.assertTrue(all(row["auto"] for row in window.records["jobs"]))
                self.assertFalse(window.auto_active)
                window.scheduler_tick()
                self.assertFalse(any(call[0] == "send" for call in self.calls))
                window.emergency_stop()
                self.assertTrue(all(row["state"] == "paused" for row in window.records["jobs"]))
        finally:
            window.close()


@unittest.skipUnless(find_spec("telethon"), "Requires Telethon")
class LoginTests(unittest.TestCase):
    def test_phone_code_two_factor_and_in_memory_session(self):
        from types import SimpleNamespace
        from telethon import errors

        called = []

        class Client:
            session = SimpleNamespace(save=lambda: "fake-session")

            async def connect(self):
                called.append("connect")

            async def disconnect(self):
                called.append("disconnect")

            async def send_code_request(self, phone):
                return SimpleNamespace(phone_code_hash="fake-hash")

            async def sign_in(self, **fields):
                if "code" in fields:
                    self.assert_code = fields["code"]
                    raise errors.SessionPasswordNeededError(None)
                called.append(bool(fields.get("password")))

            async def get_me(self):
                return SimpleNamespace(id=11, bot=False, first_name="Test", last_name=None,
                                       username=None, photo=None)

        async def prompt(kind):
            called.append(kind)
            return "code" if kind == "code" else "secret"

        with patch("promotion_studio.telegram_accounts._client", return_value=Client()):
            identity, saved = login(12, "a"*32, "phone", "+12345", prompt, lambda *_: None)
        self.assertEqual((identity.telegram_id, saved), (11, "fake-session"))
        self.assertEqual(called, ["connect", "code", "password", True, "disconnect"])

    def test_qr_expiry_cancel_and_bad_credentials(self):
        called = []
        cancelled = Event()

        class QR:
            url = "tg://login?token=synthetic"
            expires = None

            async def wait(self):
                await asyncio.sleep(10)

        class Client:
            async def connect(self):
                pass

            async def qr_login(self):
                return QR()

            async def disconnect(self):
                called.append("disconnect")

        def present(*_):
            cancelled.set()

        with patch("promotion_studio.telegram_accounts._client", return_value=Client()):
            with self.assertRaisesRegex(LoginError, "отменён"):
                login(12, "a"*32, "qr", "", None, present, cancel=cancelled)
        self.assertEqual(called, ["disconnect"])
        with self.assertRaisesRegex(LoginError, "API ID"):
            login(-1, "plain", "qr", "", None, None)

    def test_invalid_phone_code_is_reported_without_echo(self):
        from types import SimpleNamespace
        from telethon import errors

        class Client:
            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def send_code_request(self, _):
                return SimpleNamespace(phone_code_hash="fake-hash")

            async def sign_in(self, **_):
                raise errors.PhoneCodeInvalidError(None)

        async def prompt(_):
            return "12345"

        with patch("promotion_studio.telegram_accounts._client", return_value=Client()):
            with self.assertRaisesRegex(LoginError, "Код неверный") as failure:
                login(12, "a" * 32, "phone", "+12345", prompt, None)
        self.assertNotIn("12345", str(failure.exception))


@unittest.skipUnless(find_spec("PySide6") and os.name == "nt", "Requires Qt and Windows")
class RedesignedQtTests(unittest.TestCase):
    def test_import_formats_and_responsive_worker(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication
        from promotion_studio.desktop import StudioWindow
        from promotion_studio.desktop_directory import import_rows

        self.assertEqual(import_rows("link,name,category\nhttps://t.me/example,Имя,Тест", True),
                         [("https://t.me/example", "Имя", "Тест")])
        self.assertEqual(import_rows("@allowed_group\n-100100"), [("@allowed_group", "", ""), ("-100100", "", "")])
        app = QApplication.instance() or QApplication([])
        with OfflineProfile() as profile:
            service = DesktopService(profile)
            service.initialize()
            window = StudioWindow(service)
            window.show()
            app.processEvents()
            loop = QEventLoop()
            responsive, complete = Event(), []
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(loop.quit)
            timeout.start(4000)
            try:
                self.assertFalse(window.auto_active)
                window._run_network(lambda: responsive.wait(1), lambda ok: (complete.append(ok), loop.quit()))
                QTimer.singleShot(40, responsive.set)
                loop.exec()
                self.assertEqual(complete, [True])
                self.assertTrue(window.stop_button.isEnabled())
            finally:
                window.close()
