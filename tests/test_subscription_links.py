import io
import json
import unittest

from promotion_studio.bot_transport import BotAPITransport
from promotion_studio.diagnostics import read_queue_status
from promotion_studio.job_queue import QueueConflict
from promotion_studio.security import DPAPI
from promotion_studio.storage import open_profile
from promotion_studio.subscription_links import (PERIOD_SECONDS, create_subscription, read_subscription_link,
                                                 reconcile_subscription)
from tests import test_bot_transport as http
from tests import test_executor
from tests.support import OfflineProfile


class InviteOpener(http.FakeOpener):
    def __init__(self, *, can_invite=True, outcome="created"):
        super().__init__([])
        self.can_invite, self.outcome = can_invite, outcome
        self.receipt = None

    def open(self, request, timeout):
        method = request.full_url.rsplit("/", 1)[-1]
        data = json.loads(request.data)
        self.requests.append((method, data))
        if method == "getMe":
            result = {"id": 42, "is_bot": True}
        elif method == "getChat":
            result = {"id": -100123, "type": "channel"}
        elif method == "getChatMember":
            result = {"status": "administrator", "user": {"id": 42}, "can_invite_users": self.can_invite}
        else:
            if method != "createChatSubscriptionInviteLink":
                raise AssertionError(method)
            self.receipt = {
                "invite_link": "https://t.me/+PrivateFixture", "creator": {"id": 42},
                "name": data["name"], "is_revoked": False,
                "subscription_price": data["subscription_price"], "subscription_period": PERIOD_SECONDS,
            }
            result = self.receipt
            if self.outcome == "timeout":
                raise TimeoutError()
            if self.outcome == "wrong_price":
                result = result | {"subscription_price": 251}
        return io.BytesIO(json.dumps(http.success(result)).encode())


class SubscriptionTests(unittest.TestCase):
    def run_creation(self, db, opener, **overrides):
        options = dict(request_id="request", campaign_id="campaign", bot_account_id="bot",
                       chat_id=-100123, price=250, content_confirmed=True)
        return create_subscription(db, DPAPI(), BotAPITransport(http.Secrets(), opener=opener),
                                   lambda: 100, **(options | overrides))

    def test_success_and_idempotency(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            test_executor.ExecutorTests().prepare(db, profile)
            opener = InviteOpener()
            self.assertEqual(self.run_creation(db, opener), "created")
            self.assertEqual(self.run_creation(db, opener), "created")
            method, request = opener.requests[-1]
            self.assertEqual((method, request["subscription_price"], request["subscription_period"]),
                             ("createChatSubscriptionInviteLink", 250, PERIOD_SECONDS))
            self.assertEqual(len(opener.requests), 4)
            self.assertEqual(read_subscription_link(db, DPAPI(), "request"), opener.receipt["invite_link"])
            self.assertNotIn(b"PrivateFixture", (profile / "studio.sqlite3").read_bytes())

    def test_timeout_or_wrong_price_requires_review_after_reopen(self):
        for outcome in ("timeout", "wrong_price"):
            with self.subTest(outcome=outcome), OfflineProfile() as profile:
                opener = InviteOpener(outcome=outcome)
                with open_profile(profile) as db:
                    test_executor.ExecutorTests().prepare(db, profile)
                    self.assertEqual(self.run_creation(db, opener), "needs_review")
                with open_profile(profile) as db:
                    self.assertEqual(self.run_creation(db, opener), "needs_review")
                    with self.assertRaises(QueueConflict):
                        self.run_creation(db, opener, request_id="new")
                    with self.assertRaises(QueueConflict):
                        reconcile_subscription(db, DPAPI(), "request", 110, receipt=opener.receipt,
                                               verified_chat_id=-100456)
                    self.assertEqual(reconcile_subscription(db, DPAPI(), "request", 110,
                                                           receipt=opener.receipt,
                                                           verified_chat_id=-100123), "created")
                    self.assertEqual(len(opener.requests), 4)

    def test_invite_right_and_explicit_content_confirmation(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            test_executor.ExecutorTests().prepare(db, profile)
            opener = InviteOpener(can_invite=False)
            with self.assertRaises(ValueError):
                self.run_creation(db, opener, content_confirmed=False)
            with self.assertRaises(QueueConflict):
                self.run_creation(db, opener, chat_id=-100456)
            self.assertEqual(opener.requests, [])
            self.assertEqual(self.run_creation(db, opener), "rejected")
            self.assertEqual([method for method, _ in opener.requests], ["getMe", "getChat", "getChatMember"])

    def test_crash_after_response_never_creates_second_invite(self):
        class SimulatedCrash(BaseException):
            pass

        class CrashBeforePersistence:
            def unprotect(self, value):
                return DPAPI().unprotect(value)

            def protect(self, value):
                raise SimulatedCrash

        with OfflineProfile() as profile:
            opener = InviteOpener()
            with open_profile(profile) as db:
                test_executor.ExecutorTests().prepare(db, profile)
                with self.assertRaises(SimulatedCrash):
                    create_subscription(db, CrashBeforePersistence(),
                                        BotAPITransport(http.Secrets(), opener=opener), lambda: 100,
                                        request_id="request", campaign_id="campaign", bot_account_id="bot",
                                        chat_id=-100123, price=250, content_confirmed=True)
                self.assertEqual(db.execute("SELECT state FROM subscription_requests").fetchone(), ("pending",))
            report = read_queue_status(profile)
            self.assertEqual(report["subscriptions"], [{"id": "request", "state": "needs_review", "started_at": 100}])
            self.assertNotIn("PrivateFixture", json.dumps(report))
            with open_profile(profile) as db:
                self.assertEqual(self.run_creation(db, opener), "needs_review")
                self.assertEqual(len(opener.requests), 4)

