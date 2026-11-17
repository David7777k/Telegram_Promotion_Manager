"""Pure checks used before a publication is eligible for scheduling."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from re import fullmatch
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Consent:
    chat_id: int
    bot_id: int
    valid_from: datetime
    expires_at: datetime | None = None
    thread_id: int | None = None
    revoked: bool = False


@dataclass(frozen=True)
class Offer:
    link: str
    price_stars: int
    period_days: int
    content_confirmed: bool


def subscription_terms(offer):
    return f"Подписка: {offer.price_stars} Stars каждые {offer.period_days} дней."


def telegram_offer_link(link):
    if not isinstance(link, str) or any(c.isspace() or ord(c) < 32 for c in link):
        return False
    try:
        parsed = urlsplit(link)
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.netloc.lower() in {"t.me", "telegram.me"}
            and not parsed.query and not parsed.fragment
            and fullmatch(r"/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+|[A-Za-z][A-Za-z0-9_]{3,})/?",
                          parsed.path) is not None)


def publication_link(link):
    if not isinstance(link, str) or any(c.isspace() or ord(c) < 32 for c in link):
        return False
    try:
        parsed = urlsplit(link)
        return parsed.scheme == 'https' and bool(parsed.hostname) and parsed.username is None
    except ValueError:
        return False


def publication_issues(*, chat_id, chat_type, bot_id, can_publish, consent, when,
                       offer, preview, button_url=None, thread_id=None,
                       last_sent=None, cooldown_seconds=0, in_allowed_window=True,
                       has_photo=False, allow_plain=False):
    issues = []
    if type(chat_id) is not int or chat_id >= 0 or chat_type not in {"group", "supergroup", "channel"}:
        issues.append("invalid_chat")
    if can_publish is not True:
        issues.append("missing_publish_rights")
    if (consent is None or consent.revoked or consent.chat_id != chat_id or consent.bot_id != bot_id
            or consent.thread_id != thread_id or when < consent.valid_from
            or (consent.expires_at is not None and when >= consent.expires_at)):
        issues.append("missing_consent")
    if not in_allowed_window:
        issues.append("outside_allowed_window")
    if last_sent is not None and when < last_sent + timedelta(seconds=cooldown_seconds):
        issues.append("cooldown")
    if len(preview) > (1024 if has_photo else 4096) or not preview.strip():
        issues.append("preview_too_long")
    if offer is None and allow_plain:
        if button_url is not None and not publication_link(button_url):
            issues.append("invalid_offer_link")
        return tuple(issues)
    if offer.content_confirmed is not True:
        issues.append("unconfirmed_content")
    if type(offer.price_stars) is not int or not 1 <= offer.price_stars <= 10000:
        issues.append("invalid_price")
    if type(offer.period_days) is not int or offer.period_days != 30:
        issues.append("invalid_period")
    if subscription_terms(offer) not in preview.splitlines():
        issues.append("missing_subscription_terms")
    if not telegram_offer_link(offer.link) or (button_url is not None and button_url != offer.link):
        issues.append("invalid_offer_link")
    elif button_url is None and offer.link not in preview.split():
        issues.append("missing_offer_link_in_preview")
    return tuple(issues)

