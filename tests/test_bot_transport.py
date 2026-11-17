import io
import json
import unittest
from urllib.error import HTTPError

from promotion_studio.bot_transport import BotAPITransport
from promotion_studio.executor import TransportRejected, TransportUnavailable, execute_one
from promotion_studio.job_queue import claim_due, reserve
from promotion_studio.security import DPAPI
from promotion_studio.storage import open_profile
from tests import test_executor
from tests.support import OfflineProfile


class Secrets:
    def get(self, account, kind):
        return b"42:fixture"


def success(data):
    return {"ok": True, "result": data}


def preflight(can_post=True):
    return [success({"id": 42, "is_bot": True}), success({"id": -100123, "type": "channel"}),
            success({"status": "administrator", "can_post_messages": can_post, "user": {"id": 42}})]


class FakeOpener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def open(self, request, timeout):
        content_type = request.get_header("Content-type")
        if content_type.startswith("multipart/"):
            body = (content_type, request.data)
        else:
            body = json.loads(request.data)
        self.requests.append((request.full_url.rsplit("/", 1)[-1], body))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())


class BotTransportTests(unittest.TestCase):
    def test_text_button_and_bound_preflight(self):
        opener = FakeOpener(preflight() + [success({"chat": {"id": -100123}, "message_id": 7})])
        transport = BotAPITransport(Secrets(), opener=opener)
        with self.assertRaises(TransportRejected):
            transport.send("bot", -100123, None, "test", None)
        self.assertTrue(transport.inspect("bot", -100123, None).can_publish)
        self.assertEqual(transport.send("bot", -100123, None, "test", "https://t.me/+Example"), 7)
        self.assertEqual([method for method, _ in opener.requests],
                         ["getMe", "getChat", "getChatMember", "sendMessage"])
        markup = opener.requests[-1][1]["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["url"], "https://t.me/+Example")

    def test_channel_requires_explicit_post_right(self):
        transport = BotAPITransport(Secrets(), opener=FakeOpener(preflight(False)))
        self.assertFalse(transport.inspect("bot", -100123, None).can_publish)
        with self.assertRaises(TransportRejected):
            transport.send("bot", -100123, None, "test", None)

    def test_lost_response_and_wrong_chat_receipt_are_uncertain(self):
        for reply in (TimeoutError(), success({"chat": {"id": -99}, "message_id": 7})):
            with self.subTest(reply=reply):
                transport = BotAPITransport(Secrets(), opener=FakeOpener(preflight() + [reply]))
                transport.inspect("bot", -100123, None)
                with self.assertRaises(TransportUnavailable):
                    transport.send("bot", -100123, None, "test", None)

    def test_confirmed_rate_limit_pauses_and_holds_account(self):
        for retry_after in (15, None):
            with self.subTest(retry_after=retry_after), OfflineProfile() as profile, open_profile(profile) as db:
                job = test_executor.ExecutorTests().prepare(db, profile)
                next_job = reserve(db, client_key="another", campaign_id="campaign", venue_id="venue",
                                   template_id="template", chat_id=-100456, bot_id="bot",
                                   scheduled=101, cooldown=30,
                                   encrypted_payload=DPAPI().protect(b"test fixture"), price=250)
                body = {"ok": False, "error_code": 429, "parameters": {"retry_after": retry_after}}
                response = HTTPError("https://api.telegram.org", 429, "test", {},
                                     io.BytesIO(json.dumps(body).encode()))
                transport = BotAPITransport(Secrets(), opener=FakeOpener(preflight() + [response]))
                self.assertEqual(execute_one(db, DPAPI(), transport, lambda: 100), (job, "paused"))
                self.assertEqual(db.execute("SELECT until_utc FROM bot_holds").fetchone(),
                                 (115 if retry_after else None,))
                self.assertEqual(db.execute("SELECT reason_code FROM job_attempts").fetchone(),
                                 ("telegram_rate_limited",))
                self.assertIsNone(execute_one(db, DPAPI(), transport, lambda: 101))
                after = claim_due(db, 115)
                if retry_after is None:
                    self.assertIsNone(after)
                else:
                    self.assertEqual(after[0], next_job)

