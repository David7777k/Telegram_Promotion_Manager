"""Operator workflows for explicit, consented publication."""
import json
import re
from dataclasses import asdict, dataclass
from uuid import uuid4

from .job_queue import QueueConflict, reserve, transaction
from .photos import load_photo
from .policies import Offer, subscription_terms
from .security import SecretStore


class DraftError(ValueError):
    """A draft validation error safe to show to the operator."""


@dataclass(frozen=True)
class PublicationDraft:
    name: str
    description: str
    channel_id: int
    target_chat_id: int
    bot_account_id: str
    link: str
    price: int
    text: str
    scheduled_at: int
    consent_note: str
    consent_until: int
    content_confirmed: bool
    photo_digest: str | None = None
    cooldown_minutes: int = 60

    @property
    def offer(self):
        return Offer(self.link, self.price, 30, self.content_confirmed)

    @property
    def preview(self):
        return self.text.strip() + "\n\n" + subscription_terms(self.offer)


def _packed(protector, data):
    return protector.protect(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def register_bot(db, protector, transport, token):
    token = token.strip()
    identity = transport.identity(token) if hasattr(transport, "identity") else {"id": transport.identify(token)}
    telegram_id = identity["id"]
    name = identity.get("first_name") or f"Бот {telegram_id}"
    with transaction(db):
        row = db.execute("SELECT id FROM accounts WHERE kind='bot' AND telegram_user_id=?",
                         (telegram_id,)).fetchone()
        account = row[0] if row else uuid4().hex
        if row:
            db.execute("UPDATE accounts SET status='ready',disabled_at=NULL,label=?,username=? WHERE id=?",
                       (name, identity.get("username"), account))
        else:
            db.execute("""INSERT INTO accounts(id,kind,telegram_user_id,label,username,status)
                          VALUES (?,'bot',?,?,?,'ready')""", (account, telegram_id, name, identity.get("username")))
        SecretStore(db, protector).put(account, "bot_token", token.encode("ascii"))
    return account


def list_bots(db):
    return db.execute("""SELECT id,label FROM accounts
                         WHERE kind='bot' AND disabled_at IS NULL AND telegram_user_id IS NOT NULL
                         ORDER BY label,id""").fetchall()


def enqueue_draft(db, protector, draft, now):
    if (not draft.name.strip() or not draft.description.strip() or not draft.text.strip()
            or not draft.consent_note.strip() or draft.content_confirmed is not True):
        raise DraftError("Заполните название, описание, текст публикации и основание согласия площадки.")
    if (any(type(chat) is not int or not -(2**63) < chat < 0
            for chat in (draft.channel_id, draft.target_chat_id))
            or type(draft.price) is not int or not 1 <= draft.price <= 10000
            or not re.fullmatch(r"https://t\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+", draft.link)
            or not now - 60 <= draft.scheduled_at < draft.consent_until
            or draft.consent_until <= now):
        raise DraftError("Проверьте числовые ID чатов, цену, платную ссылку и сроки согласия.")
    if type(draft.cooldown_minutes) is not int or not 1 <= draft.cooldown_minutes <= 10080:
        raise DraftError("Укажите согласованный интервал от 1 до 10080 минут.")
    preview = draft.preview
    preview.encode("utf-8")
    if len(preview) > (1024 if draft.photo_digest else 4096):
        raise DraftError("Предпросмотр превышает лимит текста Telegram.")
    if draft.photo_digest:
        load_photo(db, protector, draft.photo_digest)
    if not any(account == draft.bot_account_id for account, _ in list_bots(db)):
        raise QueueConflict("Сначала подключите бота.")
    campaign, template, permission, client_key = (uuid4().hex for _ in range(4))
    with transaction(db):
        run_id = db.execute("""INSERT INTO import_runs(source_kind,sha256,source_zone,run_at)
                               VALUES ('operator',?,'UTC',?)""", (campaign, now)).lastrowid
        row = db.execute("SELECT id FROM venues WHERE canonical_chat_id=?",
                         (draft.target_chat_id,)).fetchone()
        venue = row[0] if row else uuid4().hex
        if not row:
            db.execute("""INSERT INTO venues(id,run_id,consent,legacy_last_sent_at,payload,canonical_chat_id)
                          VALUES (?,?,'recheck',NULL,?,?)""",
                       (venue, run_id, _packed(protector, {"chat_id": draft.target_chat_id}), draft.target_chat_id))
        db.execute("INSERT INTO campaigns(id,run_id,status,payload) VALUES (?,?,'draft',?)",
                   (campaign, run_id, _packed(protector, {"name": draft.name, "description": draft.description,
                                                 "channel_chat_id": draft.channel_id, "price": draft.price})))
        db.execute("INSERT INTO templates(id,run_id,syntax_version,payload) VALUES (?,?,2,?)",
                   (template, run_id, _packed(protector, {"body": draft.text})))
        db.execute("""INSERT INTO venue_permissions(id,venue_id,chat_id,bot_account_id,valid_from,
                      expires_at,allows_photo,evidence) VALUES (?,?,?,?,?,?,?,?)""",
                   (permission, venue, draft.target_chat_id, draft.bot_account_id, now,
                    draft.consent_until, int(bool(draft.photo_digest)), _packed(protector, draft.consent_note)))
        snapshot = dict(client_key=client_key, permission_id=permission, text=preview,
                        offer=asdict(draft.offer), button_url=draft.link, photo_digest=draft.photo_digest)
        return reserve(db, client_key=client_key, campaign_id=campaign, venue_id=venue, template_id=template,
                       chat_id=draft.target_chat_id, bot_id=draft.bot_account_id, scheduled=draft.scheduled_at,
                       cooldown=draft.cooldown_minutes * 60, encrypted_payload=_packed(protector, snapshot),
                       price=draft.price)

