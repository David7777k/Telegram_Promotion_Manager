"""Telegram Bot API transport for one preflighted publication."""
import http.client
import json
import re
import urllib.error
import urllib.request
from uuid import uuid4

from .executor import Access, TransportRejected, TransportUnavailable
from .photos import MAX_PHOTO_BYTES, Photo
from .policies import publication_link
from .security import SecretError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decode(stream):
    try:
        raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("Response exceeds limit")
        data = json.loads(raw)
    except (ValueError, OSError, http.client.HTTPException):
        raise TransportUnavailable("Unusable Bot API response") from None
    if not isinstance(data, dict):
        raise TransportUnavailable("Unusable Bot API response")
    return data


def _multipart(payload, photo):
    encoded = [(name, (json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value))
                .encode("utf-8")) for name, value in payload.items()]
    while True:
        boundary = uuid4().hex
        marker = boundary.encode("ascii")
        if marker not in photo.content and all(marker not in data for _, data in encoded):
            break
    parts = []
    for name, data in encoded:
        parts.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                      data, b"\r\n"))
    filename = "photo.png" if photo.mime == "image/png" else "photo.jpg"
    parts.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
                  f'Content-Type: {photo.mime}\r\n\r\n'.encode(),
                  photo.content, f'\r\n--{boundary}--\r\n'.encode()))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class BotAPITransport:
    def __init__(self, secret_store, *, opener=None):
        self._secrets = secret_store
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._checked = None

    def _token(self, account):
        try:
            token = self._secrets.get(account, "bot_token")
            token = token.decode("ascii") if token is not None else ""
        except (SecretError, UnicodeError):
            raise TransportUnavailable("Bot credentials unavailable") from None
        return self.validate_token(token)

    @staticmethod
    def validate_token(token):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise TransportUnavailable("Bot credentials unavailable")
        return token

    def identify(self, token):
        return self.identity(token)["id"]

    def identity(self, token):
        result = self._call(self.validate_token(token), "getMe")
        identifier = result.get("id")
        if type(identifier) is not int or not 0 < identifier < 2**63 or result.get("is_bot") is not True:
            raise TransportUnavailable("Bot identity unavailable")
        return result

    def account_identity(self, account):
        return self.identity(self._token(account))

    def lookup_chat(self, account, reference):
        chat = self._call(self._token(account), "getChat", chat_id=reference)
        identifier, kind = chat.get("id"), chat.get("type")
        if (type(identifier) is not int or not -(2**63) < identifier < 0
                or kind not in ("channel", "group", "supergroup")
                or (type(reference) is int and reference != identifier)):
            raise TransportUnavailable("Chat identity unavailable")
        title = chat.get("title")
        return identifier, title if isinstance(title, str) else "", kind

    def _call(self, token, method, *, photo=None, **payload):
        if photo is None:
            body, content_type = json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
        else:
            body, content_type = _multipart(payload, photo)
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                data = _decode(response)
        except urllib.error.HTTPError as error:
            with error:
                if error.code == 429:
                    try:
                        data = _decode(error)
                    except TransportUnavailable:
                        data = {}
                    params = data.get("parameters")
                    wait = params.get("retry_after") if isinstance(params, dict) else None
                    wait = wait if type(wait) is int and 0 < wait < 2**31 else None
                    raise TransportRejected(rate_limited=True, retry_after=wait) from None
                if error.code in (401, 403):
                    raise TransportRejected(status=error.code) from None
                data = _decode(error)
            code = data.get("error_code")
            if data.get("ok") is False and type(code) is int and code == error.code and 400 <= code < 500:
                raise TransportRejected(status=code) from None
            raise TransportUnavailable("Uncertain Bot API result") from None
        except (OSError, http.client.HTTPException):
            raise TransportUnavailable("Uncertain Bot API result") from None
        result = data.get("result")
        if data.get("ok") is not True or not isinstance(result, dict):
            raise TransportUnavailable("Unusable Bot API response")
        return result

    def inspect(self, account, chat_id, thread_id):
        self._checked = None
        token = self._token(account)
        bot = self._call(token, "getMe")
        bot_id = bot.get("id")
        if type(bot_id) is not int or bot_id <= 0 or bot.get("is_bot") is not True:
            raise TransportUnavailable("Bot identity unavailable")
        chat = self._call(token, "getChat", chat_id=chat_id)
        kind = chat.get("type")
        if chat.get("id") != chat_id or kind not in ("channel", "group", "supergroup"):
            raise TransportUnavailable("Chat identity unavailable")
        member = self._call(token, "getChatMember", chat_id=chat_id, user_id=bot_id)
        user = member.get("user")
        if not isinstance(user, dict) or user.get("id") != bot_id:
            raise TransportUnavailable("Chat membership unavailable")
        status = member.get("status")
        if kind == "channel":
            allowed = status == "creator" or (status == "administrator" and member.get("can_post_messages") is True)
            photo_allowed = allowed
        else:
            permissions = chat.get("permissions")
            allowed = (status in ("creator", "administrator")
                       or (status == "restricted" and member.get("is_member") is True
                           and member.get("can_send_messages") is True)
                       or (status == "member" and isinstance(permissions, dict)
                           and permissions.get("can_send_messages") is True))
            photo_allowed = allowed and (
                status in ("creator", "administrator")
                or (status == "restricted" and member.get("can_send_photos") is True)
                or (status == "member" and isinstance(permissions, dict)
                    and permissions.get("can_send_photos") is True))
        if thread_id is not None and (kind != "supergroup" or chat.get("is_forum") is not True):
            allowed = photo_allowed = False
        if allowed:
            self._checked = account, chat_id, thread_id, token, photo_allowed
        return Access(chat_id, kind, bot_id, allowed, photo_allowed)

    def send(self, account, chat_id, thread_id, text, button_url, *, photo=None):
        checked, self._checked = self._checked, None
        if checked is None or checked[:3] != (account, chat_id, thread_id):
            raise TransportRejected()
        if photo is None:
            method = "sendMessage"
            payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        else:
            if (not checked[4] or not isinstance(photo, Photo)
                    or photo.mime not in ("image/png", "image/jpeg")
                    or not isinstance(photo.content, bytes) or not 0 < len(photo.content) <= MAX_PHOTO_BYTES
                    or len(text) > 1024):
                raise TransportRejected()
            method = "sendPhoto"
            payload = {"chat_id": chat_id, "caption": text}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if button_url is not None:
            if not publication_link(button_url):
                raise TransportRejected()
            title = "Открыть подписку" if button_url.startswith(("https://t.me/+", "https://t.me/joinchat/")) else "Открыть ссылку"
            payload["reply_markup"] = {"inline_keyboard": [[{"text": title, "url": button_url}]]}
        result = self._call(checked[3], method, photo=photo, **payload)
        chat = result.get("chat")
        if not isinstance(chat, dict) or chat.get("id") != chat_id:
            raise TransportUnavailable("Receipt chat mismatch")
        return result.get("message_id")

    def create_paid_invite(self, account, chat_id, expected_bot_id, name, price, period):
        token = self._token(account)
        bot = self._call(token, "getMe")
        if bot.get("id") != expected_bot_id or bot.get("is_bot") is not True:
            raise TransportRejected()
        chat = self._call(token, "getChat", chat_id=chat_id)
        if chat.get("id") != chat_id or chat.get("type") != "channel":
            raise TransportRejected()
        member = self._call(token, "getChatMember", chat_id=chat_id, user_id=expected_bot_id)
        user = member.get("user")
        if (not isinstance(user, dict) or user.get("id") != expected_bot_id
                or not (member.get("status") == "creator" or
                        (member.get("status") == "administrator" and member.get("can_invite_users") is True))):
            raise TransportRejected()
        return self._call(token, "createChatSubscriptionInviteLink", chat_id=chat_id, name=name,
                          subscription_period=period, subscription_price=price)

