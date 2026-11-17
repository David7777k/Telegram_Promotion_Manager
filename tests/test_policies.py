import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from promotion_studio.policies import Consent, Offer, publication_issues, subscription_terms, telegram_offer_link


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.offer = Offer("https://t.me/+Ab_01", 250, 30, True)
        self.base = dict(chat_id=-100123, chat_type="channel", bot_id=1, can_publish=True,
                         consent=Consent(-100123, 1, self.now), when=self.now,
                         offer=self.offer, preview=f"Канал\n{subscription_terms(self.offer)}\n{self.offer.link}")

    def test_consent_scope_and_time(self):
        self.assertEqual(publication_issues(**self.base), ())
        for override, issue in (({"thread_id": 2}, "missing_consent"),
                                ({"bot_id": 2}, "missing_consent"),
                                ({"can_publish": False}, "missing_publish_rights"),
                                ({"chat_type": "private"}, "invalid_chat"),
                                ({"consent": replace(self.base["consent"], expires_at=self.now)}, "missing_consent"),
                                ({"last_sent": self.now - timedelta(seconds=3), "cooldown_seconds": 10}, "cooldown"),
                                ({"in_allowed_window": False}, "outside_allowed_window")):
            self.assertIn(issue, publication_issues(**(self.base | override)))

    def test_link_and_offer_checks(self):
        for link in ("https://t.me.evil.test/+123", "https://user@t.me/+123", "https://t.me:443/+123",
                     "https://t.me/+123?redirect=other", "https://t.me/+12\n3", "http://t.me/+123"):
            self.assertFalse(telegram_offer_link(link))
        self.assertIn("invalid_period", publication_issues(**(self.base | {"offer": replace(self.offer, period_days=7)})))
        self.assertIn("invalid_price", publication_issues(**(self.base | {"offer": replace(self.offer, price_stars=True)})))
        self.assertIn("invalid_offer_link", publication_issues(**(self.base | {"button_url": "https://t.me/other"})))

    def test_terms_and_caption_limits(self):
        preview = self.base["preview"].replace("250 Stars", "251 Stars")
        self.assertIn("missing_subscription_terms", publication_issues(**(self.base | {"preview": preview})))
        preview = "x" * 1024 + "\n" + self.base["preview"]
        self.assertIn("preview_too_long",
                      publication_issues(**(self.base | {"preview": preview, "has_photo": True})))

