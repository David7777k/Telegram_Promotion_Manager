"""Parse chat identifiers from Telegram channel names and message links."""
import re
from urllib.parse import urlsplit


_USERNAME = r"[A-Za-z][A-Za-z0-9_]{3,31}"
_RESERVED = {"joinchat", "share", "proxy", "socks", "addstickers", "addemoji", "login",
             "confirmphone", "iv", "setlanguage", "boost", "contact", "invoice", "giftcode"}


def parse_chat_reference(value):
    value = value.strip()
    if re.fullmatch(r"-[0-9]{1,19}", value):
        identifier = int(value)
        if -(2**63) < identifier < 0:
            return identifier
    if re.fullmatch("@" + _USERNAME, value):
        return value
    if value.lower().startswith(("t.me/", "telegram.me/")):
        value = "https://" + value
    if not any(character.isspace() or ord(character) < 32 for character in value):
        try:
            url = urlsplit(value)
        except ValueError:
            url = None
        if url and url.scheme == "https" and url.netloc.lower() in {"t.me", "telegram.me"}:
            path = url.path.rstrip("/")
            private = re.fullmatch(r"/c/([0-9]{1,12})/[0-9]+(?:/[0-9]+)?", path)
            if private and 1 <= int(private[1]) <= 997_852_516_352:
                return -(1_000_000_000_000 + int(private[1]))
            public = re.fullmatch(rf"/(?:s/)?({_USERNAME})(?:/[0-9]+){{0,2}}", path)
            if public and public[1].lower() not in _RESERVED:
                return "@" + public[1]
    raise ValueError("Укажите @имя, числовой ID или ссылку на пост. Пригласительная ссылка сюда не подходит.")

