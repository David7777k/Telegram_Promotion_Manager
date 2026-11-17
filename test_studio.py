"""Offline tests: no Telegram token and no network access needed."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import model
from model import (make_default, new_campaign, new_target, new_post, uid, render_text,
                   audit_creative, target_cooldown, may_send, summarize, export_posts_csv,
                   save_state, load_state, migrate_old)
from telegram_client import BotClient, TelegramError


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.state = make_default()
        self.camp = new_campaign("Реклама", channel="Тестовый канал", topic="Книги",
                                 description="Разборы прочитанных книг каждую неделю.",
                                 link="https://t.me/+safeExample123", price=250,
                                 content_ready=True)
        self.target = new_target("Согласованный партнёр", "@permittedchannel",
                                 approved=True, approval_note="Письменное согласие владельца 11.11",
                                 cooldown_days=7)
        self.state["campaigns"].append(self.camp)
        self.state["targets"].append(self.target)
        self.tmpl = self.state["templates"][0]

    def plan(self, days=1):
        post = new_post(self.camp["id"], self.target["id"], self.tmpl["id"],
                        (datetime.now() + timedelta(days=days)).strftime(model.ISO_FMT))
        self.state["posts"].append(post)
        return post

    def test_dry_run_default_and_auto_disabled(self):
        self.assertTrue(self.state["settings"]["dry_run"])
        self.assertFalse(self.state["settings"]["auto_schedule"])
        self.assertEqual(len(self.state["campaigns"]), 1)

    def test_template_renders_monthly_price_and_link(self):
        text = render_text(self.tmpl["body"], self.camp)
        self.assertIn("250 Stars", text)
        self.assertIn("30 дней", text)
        self.assertIn("https://t.me/", text)
        self.assertFalse(any(sev == "error" for sev, _ in audit_creative(text, self.camp)))

    def test_unsafe_placeholder_and_hidden_monthly_fee(self):
        text = "Канал — 250 Stars! [впиши описание] https://t.me/+abc"
        self.assertGreaterEqual(sum(sev == "error" for sev, _ in audit_creative(text, self.camp)), 2)

    def test_no_approval_no_send(self):
        p = self.plan()
        self.target["approved"] = False
        ok, reason = may_send(self.state, p)
        self.assertFalse(ok)
        self.assertIn("разрешение", reason.lower())

    def test_no_content_no_send(self):
        p = self.plan()
        self.camp["content_ready"] = False
        ok, reason = may_send(self.state, p)
        self.assertFalse(ok)
        self.assertIn("контент", reason.lower())

    def test_cooldown_prevents_repeat(self):
        self.plan(days=1)
        can, _ = target_cooldown(self.state, self.target, datetime.now() + timedelta(days=3))
        self.assertFalse(can)
        can, _ = target_cooldown(self.state, self.target, datetime.now() + timedelta(days=10))
        self.assertTrue(can)

    def test_send_same_post_excludes_its_own_planned_date(self):
        p = self.plan(days=0)
        good, reason = may_send(self.state, p)
        self.assertTrue(good, reason)

    def test_snapshot_price_not_retroactively_changed(self):
        p = self.plan()
        p["status"] = "sent"
        p["paid"] = 4
        p["price_at_send"] = 250
        self.camp["price"] = 500
        self.assertEqual(summarize(self.state)["stars_gross"], 1000)

    def test_persistence_and_auto_optin_reset(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            self.state["settings"]["auto_schedule"] = True
            save_state(self.state, path)
            data = load_state(path)
            self.assertFalse(data["settings"]["auto_schedule"])
            self.assertEqual(data["campaigns"][0]["name"], "Реклама")
            self.assertEqual(data["targets"][0]["approved"], True)

    def test_csv_contains_correct_aggregates(self):
        p = self.plan()
        p.update(status="sent", paid=3, price_at_send=250, actual_cost_uah=120)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stats.csv"
            export_posts_csv(self.state, path)
            txt = path.read_text(encoding="utf-8-sig")
            self.assertIn("750", txt)
            self.assertIn("120", txt)
            self.assertIn("Реклама", txt)

    def test_telegram429_is_not_retried(self):
        from urllib.error import HTTPError
        from io import BytesIO
        class FakeErr(HTTPError):
            def __init__(self):
                super().__init__("https://api.telegram.org", 429, "Too Many Requests", {},
                                 BytesIO(json.dumps({"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 18}}).encode()))
        with patch("urllib.request.urlopen", side_effect=FakeErr()) as fetch:
            with self.assertRaises(TelegramError) as cm:
                BotClient("123:abc").call("getMe")
            self.assertEqual(cm.exception.retry_after, 18)
            self.assertEqual(fetch.call_count, 1)

    def test_telegram_api_target_uses_bot_not_user_account(self):
        self.assertRaises(TelegramError, BotClient, "not-a-token")
        self.assertEqual(BotClient("123:abc").token, "123:abc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
