"""Transactional campaign approval and versioned templates."""
import json
from dataclasses import asdict
from uuid import uuid4

from .job_queue import QueueConflict, reserve, transaction
from .operator_workflow import DraftError, _packed
from .photos import load_photo
from .policies import Offer, publication_link, subscription_terms, telegram_offer_link
from .spintax import generate, parse


VARIABLES = {"channel_link": "https://t.me/example", "channel_name": "Название канала", "price": "250",
             "subscription_period": "30 дней", "campaign_name": "Кампания", "description": "Описание",
             "topic": "Тема", "platform": "Площадка"}


def campaign_list(db):
    return db.execute("""SELECT e.id,e.title,e.changed_at,count(j.id),
                         count(CASE WHEN j.state IN ('queued','claimed','dispatching','paused') THEN 1 END)
                         FROM campaign_editor e LEFT JOIN jobs j ON j.campaign_id=e.published_campaign
                         GROUP BY e.id ORDER BY e.changed_at DESC,e.id""").fetchall()


def load_draft(db, protector, draft_id):
    row = db.execute("SELECT payload,published_campaign FROM campaign_editor WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        raise DraftError("Черновик не найден.")
    return json.loads(protector.unprotect(row[0])), row[1]


def save_draft(db, protector, draft_id, data, now):
    with transaction(db):
        if db.execute("SELECT 1 FROM campaign_editor WHERE id=? AND published_campaign IS NOT NULL",
                      (draft_id,)).fetchone():
            raise DraftError("Кампания уже утверждена. Создайте новый черновик для изменений.")
        db.execute("""INSERT INTO campaign_editor(id,title,changed_at,payload) VALUES (?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title,changed_at=excluded.changed_at,
                   payload=excluded.payload""",
                   (draft_id, data["title"].strip() or "Без названия", now, _packed(protector, data)))
    return draft_id


def render(data, kind, venue_title=""):
    text = data.get("text", "").strip()
    if not text:
        raise DraftError("Введите текст объявления.")
    link = data.get("link", "").strip()
    if link and not publication_link(link):
        raise DraftError("Введите корректную ссылку HTTPS.")
    offer = None
    if data.get("paid"):
        if not telegram_offer_link(link) or not link.startswith(("https://t.me/+", "https://t.me/joinchat/")):
            raise DraftError("Для подписки нужна платная пригласительная ссылка Telegram.")
        if not data.get("description", "").strip():
            raise DraftError("Опишите реальные условия подписки.")
        price = data.get("price")
        if type(price) is not int or not 1 <= price <= 10000:
            raise DraftError("Укажите цену от 1 до 10000 Stars.")
        offer = Offer(link, price, 30, True)
    if data.get("spintax"):
        values = dict(channel_link=link, channel_name=data.get("channel", ""),
                      price=str(offer.price_stars) if offer else "",
                      subscription_period="30 дней" if offer else "", description=data.get("description", ""),
                      platform=venue_title, campaign_name=data.get("title", ""), topic=data.get("title", ""))
        text = generate(text, values, seed=0)[0].strip()
        if not text:
            raise DraftError("Шаблон сформировал пустое объявление.")
    use_button = bool(link and data.get("button") and kind == "bot")
    if offer:
        text += "\n\n" + data["description"].strip() + "\n" + subscription_terms(offer)
    if link and not use_button and link not in text.split():
        text += "\n\n" + link
    if len(text) > (1024 if data.get("photo") else 4096):
        raise DraftError("Текст с учётом ссылки и условий превышает лимит Telegram.")
    return text, link if use_button else None, offer


def validate_target(db, target, now):
    row = db.execute("""SELECT v.canonical_chat_id,COALESCE(d.title,''),a.kind,a.telegram_user_id
                       FROM venues v JOIN venue_directory d ON d.venue_id=v.id
                       JOIN accounts a ON a.id=? WHERE v.id=? AND d.excluded=0 AND d.decision='allowed'
                       AND a.disabled_at IS NULL AND a.status='ready'""",
                     (target["account"], target["venue"])).fetchone()
    if not row:
        raise DraftError("Нужны площадка с разрешением на рекламу и подключённый отправитель.")
    if not target.get("note", "").strip():
        raise DraftError("Укажите, кто согласовал содержание, отправителя и расписание.")
    for key in ("when", "until", "interval", "repeats"):
        if type(target.get(key)) is not int:
            raise DraftError("Проверьте параметры расписания.")
    when, until, interval, repeats = (target[key] for key in ("when", "until", "interval", "repeats"))
    if not (1 <= interval <= 10080 and 1 <= repeats <= 20 and
            now - 60 <= when and when + (repeats - 1) * interval * 60 < until and until > now):
        raise DraftError("Расписание или повторы выходят за срок согласия площадки.")
    return row


def approve(db, protector, draft_id, data, targets, proof, now, *, automatic=False):
    if not data.get("title", "").strip() or not targets or not data.get("confirmed"):
        raise DraftError("Введите название, подтвердите содержимое и выберите согласованные площадки.")
    with transaction(db):
        existing = db.execute("SELECT published_campaign FROM campaign_editor WHERE id=?", (draft_id,)).fetchone()
        if existing and existing[0]:
            return tuple(x[0] for x in db.execute("SELECT id FROM jobs WHERE campaign_id=? ORDER BY scheduled_utc,id",
                                                 (existing[0],)))
        if len({t["venue"] for t in targets}) != len(targets):
            raise DraftError("Выберите одного отправителя для каждой площадки.")
        photo = data.get("photo")
        if photo:
            load_photo(db, protector, photo)
        save_draft(db, protector, draft_id, data, now)
        run = db.execute("""INSERT INTO import_runs(source_kind,sha256,source_zone,run_at)
                           VALUES ('operator',?,'UTC',?)""", (draft_id, now)).lastrowid
        template = uuid4().hex
        db.execute("INSERT INTO campaigns(id,run_id,status,payload) VALUES (?,?,'draft',?)",
                   (draft_id, run, _packed(protector, data)))
        db.execute("INSERT INTO templates(id,run_id,syntax_version,payload) VALUES (?,?,2,?)",
                   (template, run, _packed(protector, {"body": data["text"]})))
        jobs = []
        for target in targets:
            chat, title, kind, telegram_id = validate_target(db, target, now)
            access = proof.get((target["venue"], target["account"]))
            if (not access or access.chat_id != chat or access.telegram_bot_id != telegram_id
                    or not access.can_publish or (photo and not access.can_publish_photo)):
                raise DraftError(f"Нет актуальной проверки прав: {title or chat}.")
            if db.execute("""SELECT 1 FROM bot_holds WHERE bot_account_id=?
                          AND (until_utc IS NULL OR until_utc>?)""", (target["account"], now)).fetchone():
                raise QueueConflict("Для выбранного отправителя действует ограничение Telegram.")
            text, button, offer = render(data, kind, title)
            permission = uuid4().hex
            db.execute("""INSERT INTO venue_permissions(id,venue_id,chat_id,bot_account_id,valid_from,
                       expires_at,allows_photo,evidence) VALUES (?,?,?,?,?,?,?,?)""",
                       (permission, target["venue"], chat, target["account"], now, target["until"],
                        int(bool(photo)), _packed(protector, target["note"])))
            for index in range(target["repeats"]):
                key = f"{draft_id}:{target['venue']}:{index}"
                payload = dict(client_key=key, permission_id=permission, text=text,
                               kind="subscription" if offer else "plain", offer=asdict(offer) if offer else None,
                               button_url=button, photo_digest=photo)
                jobs.append(reserve(db, client_key=key, campaign_id=draft_id, venue_id=target["venue"],
                                    template_id=template, chat_id=chat, bot_id=target["account"],
                                    scheduled=target["when"] + index * target["interval"] * 60,
                                    cooldown=target["interval"] * 60, encrypted_payload=_packed(protector, payload),
                                    price=offer.price_stars if offer else None, auto_approved=automatic))
        db.execute("UPDATE campaign_editor SET published_campaign=? WHERE id=?", (draft_id, draft_id))
        return tuple(jobs)


def template_list(db):
    return db.execute("""SELECT t.id,t.name,max(v.version) FROM editable_templates t
                         JOIN editable_template_versions v ON v.template_id=t.id WHERE t.archived=0
                         GROUP BY t.id ORDER BY t.name,t.id""").fetchall()


def template_versions(db, protector, template):
    return [(v, at, protector.unprotect(content).decode("utf-8")) for v, at, content in db.execute(
        """SELECT version,created_at,ciphertext FROM editable_template_versions
           WHERE template_id=? ORDER BY version DESC""", (template,))]


def save_template(db, protector, template, name, source, now):
    name = name.strip()
    if not name or not source.strip():
        raise DraftError("Укажите имя и текст шаблона.")
    parse(source)
    template = template or uuid4().hex
    with transaction(db):
        db.execute("""INSERT INTO editable_templates(id,name) VALUES (?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name WHERE archived=0""", (template, name))
        if db.execute("SELECT archived FROM editable_templates WHERE id=?", (template,)).fetchone()[0]:
            raise DraftError("Шаблон удалён из списка.")
        versions = template_versions(db, protector, template)
        if not versions or versions[0][2] != source:
            version = versions[0][0] + 1 if versions else 1
            db.execute("INSERT INTO editable_template_versions VALUES (?,?,?,?)",
                       (template, version, now, protector.protect(source.encode("utf-8"))))
    return template
