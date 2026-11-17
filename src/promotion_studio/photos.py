"""Immutable, encrypted photo bytes for publication snapshots."""
import hashlib
import re
from dataclasses import dataclass

MAX_PHOTO_BYTES = 10_000_000


class PhotoError(ValueError):
    pass


@dataclass(frozen=True)
class Photo:
    mime: str
    content: bytes


def _mime(content):
    if not 0 < len(content) <= MAX_PHOTO_BYTES:
        raise PhotoError("Недопустимый размер фото")
    if content.startswith(b"\x89PNG\r\n\x1a\n") and content[12:16] == b"IHDR":
        return "image/png"
    if content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9"):
        return "image/jpeg"
    raise PhotoError("Поддерживаются фото PNG и JPEG")


def store_photo(db, protector, source):
    with source.open("rb") as stream:
        content = stream.read(MAX_PHOTO_BYTES + 1)
    mime = _mime(content)
    digest = hashlib.sha256(content).hexdigest()
    if db.execute("SELECT 1 FROM publication_photos WHERE digest=?", (digest,)).fetchone() is None:
        db.execute("""INSERT INTO publication_photos(digest,mime,size,ciphertext) VALUES (?,?,?,?)
                      ON CONFLICT(digest) DO NOTHING""",
                   (digest, mime, len(content), protector.protect(content)))
    load_photo(db, protector, digest)
    return digest


def load_photo(db, protector, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PhotoError("Некорректный идентификатор фото")
    row = db.execute("SELECT mime,size,ciphertext FROM publication_photos WHERE digest=?", (digest,)).fetchone()
    if row is None:
        raise PhotoError("Фото отсутствует в профиле")
    mime, size, ciphertext = row
    content = protector.unprotect(ciphertext)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest or _mime(content) != mime:
        raise PhotoError("Не совпадает контрольная сумма или формат фото")
    return Photo(mime, content)

