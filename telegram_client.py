"""Small dependency-free wrapper for permitted Bot API operations."""
from __future__ import annotations
import json
import urllib.error
import urllib.request

class TelegramError(RuntimeError):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


class BotClient:
    def __init__(self, token: str):
        self.token = token.strip()
        if not self.token or ":" not in self.token:
            raise TelegramError("Введите корректный токен бота")

    def call(self, method: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise TelegramError(f"Telegram HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise TelegramError("Ошибка подключения к Telegram") from exc
        if not data.get("ok"):
            description = str(data.get("description", "Неизвестная ошибка Telegram"))
            retry_after = int((data.get("parameters") or {}).get("retry_after", 0))
            if retry_after:
                raise TelegramError(f"Telegram ограничил отправку. Повтор не ранее чем через {retry_after} сек.; рассылка остановлена.", retry_after)
            raise TelegramError(description)
        return data.get("result", {})

    def get_me(self):
        return self.call("getMe")

    def permission_check(self, chat_id: str, bot_id: int) -> dict:
        chat = self.call("getChat", {"chat_id": chat_id})
        member = self.call("getChatMember", {"chat_id": chat_id, "user_id": bot_id})
        if member.get("status") not in ("administrator", "creator"):
            raise TelegramError("Бот не является администратором площадки")
        if chat.get("type") == "channel" and member.get("can_post_messages") is False:
            raise TelegramError("Боту запрещено публиковать посты в этом канале")
        return {"chat_type": chat.get("type", "?"), "chat_title": chat.get("title", "?"), "role": member.get("status", "?")}

    def post(self, chat_id: str, text: str) -> dict:
        return self.call("sendMessage", {"chat_id": chat_id, "text": text,
                                         "link_preview_options": {"is_disabled": True}})

    def create_subscription_link(self, own_channel_id: str, price: int, name: str) -> str:
        # 30 days fixed by Telegram Bot API, not arbitrary period.
        info = self.call("createChatSubscriptionInviteLink", {
            "chat_id": own_channel_id, "name": name[:32],
            "subscription_period": 2592000, "subscription_price": price,
        })
        link = info.get("invite_link", "")
        if not link:
            raise TelegramError("Telegram не вернул ссылку")
        return link
