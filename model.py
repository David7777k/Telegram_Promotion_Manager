"""Persistent model & pure promotion planning logic. Standard library only.

No automation of personal accounts, no scraping, no unsolicited DM capability.
"""
from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

APP_DIR = Path.home() / ".telegram_promotion_manager_v2"
STATE_FILE = APP_DIR / "state.json"
OLD_FILE = Path.home() / ".telegram_promotion_manager" / "settings.json"
URL_RE = re.compile(r"^https://(?:t\.me|telegram\.me)/[^\s]+$", re.I)
CHAT_RE = re.compile(r"^(?:@[A-Za-z][A-Za-z0-9_]{4,}|-?\d+)$")
ISO_FMT = "%Y-%m-%d %H:%M"
MONTH_DAYS = 30
PRICE_MIN, PRICE_MAX = 1, 10000
DEFAULT_TEMPLATES = [
    {"name": "Короткий · по делу", "body": "{channel}\n{description}\nПодписка: {price} Stars за 30 дней.\nВступить: {link}"},
    {"name": "Акцент на ценности", "body": "Что внутри «{channel}»?\n{description}\nСтоимость доступа — {price} Stars каждые 30 дней.\nПодробности и вступление: {link}"},
    {"name": "Для тематической площадки", "body": "Если вам интересна тема «{topic}», посмотрите «{channel}».\n{description}\nДоступ: {price} Stars / 30 дней.\n{link}"},
]


def uid() -> str:
    return uuid.uuid4().hex[:12]


def now_str() -> str:
    return datetime.now().strftime(ISO_FMT)


def make_default() -> dict:
    templates = [{"id": uid(), **item} for item in DEFAULT_TEMPLATES]
    return {
        "version": 2,
        "campaigns": [],
        "targets": [],
        "templates": templates,
        "posts": [],
        "activity": [],
        "settings": {"dry_run": True, "auto_schedule": False, "pacing_sec": 3, "default_cooldown_days": 7},
        "selected_campaign": "",
    }


def migrate_old(state: dict) -> dict:
    if not OLD_FILE.exists():
        return state
    try:
        old = json.loads(OLD_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return state
    if not isinstance(old, dict):
        return state
    if old.get("channel") or old.get("link"):
        camp = new_campaign("Импорт из первой версии", channel=old.get("channel", ""),
                            link=old.get("link", ""), price=old.get("price", 250))
        camp["description"] = ""  # must be filled, no misleading marketing
        state["campaigns"].append(camp)
        state["selected_campaign"] = camp["id"]
    for target in old.get("targets", []):
        if not isinstance(target, dict):
            continue
        converted = new_target(target.get("label", "Площадка"), target.get("chat_id", ""),
                               approved=bool(target.get("approved", False)),
                               approval_note="Импорт подтверждения из v1 — перепроверь разрешение перед отправкой" if target.get("approved") else "")
        stamp = (old.get("last_sent") or {}).get(target.get("id"))
        if stamp:
            try:
                converted["last_sent_at"] = datetime.fromtimestamp(float(stamp)).strftime(ISO_FMT)
            except (ValueError, OverflowError, OSError, TypeError):
                pass
        state["targets"].append(converted)
    for name, body in old.get("templates", {}).items():
        if isinstance(name, str) and isinstance(body, str):
            state["templates"].append({"id": uid(), "name": name + " · импорт", "body": body})
    state["settings"]["dry_run"] = True
    state["settings"]["auto_schedule"] = False
    return state


def load_state(path: Path = STATE_FILE) -> dict:
    base = make_default()
    if not path.exists():
        if path == STATE_FILE:
            base = migrate_old(base)
        return base
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return base
        for k in ("campaigns", "targets", "templates", "posts", "activity"):
            if isinstance(obj.get(k), list):
                base[k] = obj[k]
        if isinstance(obj.get("settings"), dict):
            base["settings"].update({k: v for k, v in obj["settings"].items() if k in base["settings"]})
        base["selected_campaign"] = obj.get("selected_campaign", "")
        # Never silently resurrect auto publishing after restarting.
        base["settings"]["auto_schedule"] = False
        return base
    except (OSError, json.JSONDecodeError):
        return base


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = deepcopy(state)
    clean["settings"]["auto_schedule"] = False
    # Constrain log growth without deleting posted records.
    clean["activity"] = clean.get("activity", [])[-600:]
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(clean, out, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def new_campaign(name: str, **kw) -> dict:
    item = {"id": uid(), "name": name, "channel": "", "channel_chat_id": "", "description": "",
            "topic": "", "link": "", "price": 250, "budget_uah": 0, "active": True,
            "created_at": now_str(), "content_ready": False}
    item.update(kw)
    return item


def new_target(name: str, chat_id: str, **kw) -> dict:
    item = {"id": uid(), "name": name, "chat_id": chat_id, "topic": "", "audience": 0,
            "contact": "", "cost_uah": 0, "approved": False, "approval_note": "",
            "active": True, "cooldown_days": 7, "last_sent_at": "", "type": "Партнёрский канал"}
    item.update(kw)
    return item


def new_post(campaign_id: str, target_id: str, template_id: str, scheduled_at: str, **kw) -> dict:
    item = {"id": uid(), "campaign_id": campaign_id, "target_id": target_id,
            "template_id": template_id, "scheduled_at": scheduled_at, "status": "planned",
            "sent_at": "", "message_id": "", "error": "", "source_link": "",
            "views": 0, "clicks": 0, "paid": 0, "actual_cost_uah": 0,
            "created_at": now_str(), "rendered_text": "", "price_at_send": 0}
    item.update(kw)
    return item


def by_id(items: list[dict], identifier: str) -> dict | None:
    return next((item for item in items if item.get("id") == identifier), None)


def display_price(price: Any) -> str:
    try:
        return str(int(price))
    except (ValueError, TypeError):
        return "?"


def render_text(body: str, camp: dict, target: dict | None = None, *, link: str | None = None) -> str:
    target = target or {}
    # Deliberately literal substitution (no eval or string.format execution).
    variables = {
        "{channel}": camp.get("channel", ""),
        "{description}": camp.get("description", ""),
        "{topic}": camp.get("topic", ""),
        "{price}": display_price(camp.get("price", "")),
        "{link}": link if link is not None else camp.get("link", ""),
        "{platform}": target.get("name", ""),
    }
    result = str(body)
    for token, replacement in variables.items():
        result = result.replace(token, str(replacement))
    return result


def validate_campaign(camp: dict) -> list[str]:
    errs = []
    if not str(camp.get("name", "")).strip():
        errs.append("Укажи название кампании.")
    if not str(camp.get("channel", "")).strip():
        errs.append("Укажи название рекламируемого канала.")
    if not str(camp.get("description", "")).strip():
        errs.append("Укажи реальное содержание/ценность канала.")
    link = str(camp.get("link", "")).strip()
    if not URL_RE.fullmatch(link):
        errs.append("Нужна действующая ссылка https://t.me/…")
    try:
        price = int(camp.get("price", 0))
        if not (PRICE_MIN <= price <= PRICE_MAX):
            errs.append("Цена Stars должна быть от 1 до 10 000.")
    except (TypeError, ValueError):
        errs.append("Неверная цена Stars.")
    return errs


def audit_creative(text: str, camp: dict, *, ads: bool = False) -> list[tuple[str, str]]:
    notes: list[tuple[str, str]] = []
    if len(text) > (160 if ads else 4096):
        notes.append(("error", "Слишком длинный текст для выбранного формата."))
    if not text.strip():
        notes.append(("error", "Сообщение пустое."))
    if any(p in text.lower() for p in ("[впиши", "[укажи", "[описание", "todo", "lorem ipsum")):
        notes.append(("error", "В тексте осталась заглушка, публикация запрещена."))
    if not ("star" in text.lower() or "звёз" in text.lower() or "зір" in text.lower()):
        notes.append(("error", "Укажи стоимость в Stars."))
    if "30" not in text:
        notes.append(("error", "Укажи, что подписка продлевается каждые 30 дней."))
    if not URL_RE.search(text.strip().splitlines()[-1]) and "t.me/" not in text:
        notes.append(("error", "Добавь ссылку на подписку."))
    if not str(camp.get("description", "")).strip():
        notes.append(("error", "Нет честного описания содержимого канала."))
    if re.search(r"(?i)(100% гаранти|гарантирую заработ|только сегодня|последний шанс)", text):
        notes.append(("warn", "Проверь навязчивые или необоснованные обещания."))
    if ads:
        if "\n" in text:
            notes.append(("warn", "Telegram Ads не допускает списки и разрывы строк в тексте объявления."))
        if "t.me/" in text and text.count("t.me/") > 1:
            notes.append(("warn", "В Telegram Ads допустима не более одной ссылки внутри текста."))
    return notes


def parse_local_datetime(value: str) -> datetime:
    return datetime.strptime(value.strip(), ISO_FMT)


def target_cooldown(state: dict, target: dict, at: datetime,
                    *, excluding_post_id: str = "") -> tuple[bool, str]:
    """Check last actual send & other reserved planned slots; avoids self-conflict."""
    if not target.get("active"):
        return False, "Площадка отключена"
    if not target.get("approved"):
        return False, "Не подтверждено разрешение администратора"
    cooldown = max(1, int(target.get("cooldown_days", 7)))
    last = target.get("last_sent_at", "")
    if last:
        try:
            recent = parse_local_datetime(last)
            if abs((at - recent).total_seconds()) < cooldown * 86400 and at >= recent:
                return False, f"Интервал {cooldown} дн. после предыдущей публикации"
        except ValueError:
            pass
    for post in state.get("posts", []):
        if post.get("id") == excluding_post_id or post.get("target_id") != target["id"]:
            continue
        if post.get("status") != "planned":
            continue
        try:
            existing = parse_local_datetime(post["scheduled_at"])
            if abs((at - existing).total_seconds()) < cooldown * 86400:
                return False, f"Конфликт с размещением {existing.strftime(ISO_FMT)}"
        except ValueError:
            continue
    return True, "Готово"


def may_send(state: dict, post: dict, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    camp = by_id(state["campaigns"], post["campaign_id"])
    target = by_id(state["targets"], post["target_id"])
    tmpl = by_id(state["templates"], post["template_id"])
    if post.get("status") != "planned":
        return False, "Размещение уже обработано"
    if not camp or not target or not tmpl:
        return False, "Кампания, площадка или шаблон удалены"
    if not camp.get("active"):
        return False, "Кампания отключена"
    if not camp.get("content_ready"):
        return False, "Сначала размести обещанный контент в канале и подтверди это в кампании"
    errs = validate_campaign(camp)
    if errs:
        return False, errs[0]
    ready, reason = target_cooldown(state, target, now, excluding_post_id=post["id"])
    if not ready:
        return False, reason
    text = render_text(tmpl["body"], camp, target, link=post.get("source_link") or camp["link"])
    risks = audit_creative(text, camp)
    for level, reason in risks:
        if level == "error":
            return False, reason
    return True, "Готово"


def summarize(state: dict) -> dict:
    posts = state["posts"]
    sent = [p for p in posts if p.get("status") == "sent"]
    paid = sum(max(0, int(p.get("paid", 0))) for p in sent)
    stars = 0
    for post in sent:
        camp = by_id(state["campaigns"], post.get("campaign_id", ""))
        if camp:
            stars += max(0, int(post.get("paid", 0))) * int(post.get("price_at_send") or camp.get("price", 0))
    return {
        "campaigns": len([c for c in state["campaigns"] if c.get("active")]),
        "targets": len([t for t in state["targets"] if t.get("active") and t.get("approved")]),
        "sent": len(sent), "planned": len([p for p in posts if p.get("status") == "planned"]),
        "paid": paid, "stars_gross": stars,
        "spent_uah": sum(max(0, float(p.get("actual_cost_uah", 0))) for p in sent),
        "views": sum(max(0, int(p.get("views", 0))) for p in sent),
        "clicks": sum(max(0, int(p.get("clicks", 0))) for p in sent),
    }


def add_log(state: dict, level: str, message: str) -> None:
    state["activity"].append({"at": now_str(), "level": level, "message": str(message)[:400]})
    state["activity"] = state["activity"][-600:]


def export_posts_csv(state: dict, path: Path) -> None:
    fields = ["id", "campaign", "target", "scheduled_at", "sent_at", "status", "views", "clicks", "paid", "stars_gross", "actual_cost_uah", "source_link", "message_id"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
        writer.writeheader()
        for p in state["posts"]:
            camp = by_id(state["campaigns"], p["campaign_id"]) or {}
            target = by_id(state["targets"], p["target_id"]) or {}
            writer.writerow({"id": p["id"], "campaign": camp.get("name", "(удалена)"),
                             "target": target.get("name", "(удалена)"),
                             "scheduled_at": p.get("scheduled_at"), "sent_at": p.get("sent_at"),
                             "status": p.get("status"), "views": p.get("views", 0),
                             "clicks": p.get("clicks", 0), "paid": p.get("paid", 0),
                             "stars_gross": int(p.get("paid", 0)) * int(p.get("price_at_send") or camp.get("price", 0)),
                             "actual_cost_uah": p.get("actual_cost_uah", 0),
                             "source_link": p.get("source_link", ""), "message_id": p.get("message_id", "")})
