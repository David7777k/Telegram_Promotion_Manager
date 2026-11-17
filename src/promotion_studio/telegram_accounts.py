"""Telegram account identity, directory and user publisher."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO

from .executor import Access, TransportRejected, TransportUnavailable
from .job_queue import QueueConflict


@dataclass(frozen=True)
class Identity:
    telegram_id: int
    name: str
    username: str | None = None
    avatar: bytes | None = None


@dataclass(frozen=True)
class Chat:
    id: int
    title: str
    username: str | None
    kind: str
    participants: int | None = None


class LoginError(ValueError):
    """An actionable login error that contains no credentials."""


def _telethon():
    try:
        from telethon import TelegramClient, errors, functions, types, utils
        from telethon.sessions import StringSession
    except ImportError:
        raise LoginError("Для личных аккаунтов требуется Telethon. Установите зависимости Studio.") from None
    return TelegramClient, StringSession, errors, functions, types, utils


def _client(api_id, api_hash, session=""):
    cls, string_session, *_ = _telethon()
    return cls(string_session(session), api_id, api_hash, timeout=10, request_retries=0,
               connection_retries=0, auto_reconnect=False, flood_sleep_threshold=0,
               raise_last_call_error=True)


def _identity(user, avatar=None):
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    return Identity(user.id, name or f"Аккаунт {user.id}", user.username, avatar)


async def _avatar(client, user):
    if not user.photo:
        return None
    try:
        avatar = await asyncio.wait_for(client.download_profile_photo(user, file=bytes), 5)
        return avatar if avatar and len(avatar) <= 512_000 else None
    except (OSError, TimeoutError, ValueError):
        return None
    except _telethon()[2].RPCError:
        return None


def login(api_id, api_hash, method, phone, prompt, present_qr, *, cancel=None):
    """Keep QR, code and password in memory while a cancellable dialog is open."""
    if type(api_id) is not int or not 0 < api_id < 2**31 or len(api_hash) != 32 or any(
            c not in "0123456789abcdefABCDEF" for c in api_hash):
        raise LoginError("Укажите API ID и API Hash из my.telegram.org в разделе API development tools.")
    if method not in {"phone", "qr"} or (method == "phone" and not (
            phone.startswith("+") and phone[1:].isascii() and phone[1:].isdigit())):
        raise LoginError("Введите номер в международном формате.")

    async def authenticate():
        _, _, errors, *_ = _telethon()
        client = _client(api_id, api_hash)
        try:
            await client.connect()
            try:
                if method == "qr":
                    qr = await client.qr_login()
                    present_qr(qr.url, qr.expires)
                    await qr.wait()
                else:
                    sent = await client.send_code_request(phone)
                    code = await prompt("code")
                    await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            except errors.SessionPasswordNeededError:
                await client.sign_in(password=await prompt("password"))
            user = await client.get_me()
            if user is None or user.bot:
                raise LoginError("Telegram не подтвердил личный аккаунт.")
            return _identity(user, await _avatar(client, user)), client.session.save()
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
            raise LoginError("Код неверный или истёк. Начните вход заново.") from None
        except errors.PasswordHashInvalidError:
            raise LoginError("Неверный пароль двухэтапной проверки.") from None
        except errors.FloodWaitError as error:
            raise LoginError(f"Telegram попросил подождать {error.seconds} сек. до следующего входа.") from None
        except TimeoutError:
            raise LoginError("Время ожидания истекло. Начните вход заново.") from None
        except (OSError, errors.RPCError, ValueError) as error:
            if isinstance(error, LoginError):
                raise
            raise LoginError("Telegram отклонил вход. Проверьте интернет, номер и данные приложения.") from None
        finally:
            await client.disconnect()

    async def run():
        task = asyncio.create_task(authenticate())
        try:
            deadline = asyncio.get_running_loop().time() + 300
            while not task.done():
                if cancel is not None and cancel.is_set():
                    raise LoginError("Вход отменён.")
                if asyncio.get_running_loop().time() >= deadline:
                    raise LoginError("Время ожидания истекло. Начните вход заново.")
                await asyncio.wait({task}, timeout=0.1)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return asyncio.run(run())


def _chat(entity):
    _, _, _, _, types, utils = _telethon()
    if isinstance(entity, types.Channel):
        kind = "channel" if entity.broadcast else "supergroup"
    elif isinstance(entity, types.Chat) and not entity.deactivated and not entity.migrated_to:
        kind = "group"
    else:
        return None
    return Chat(utils.get_peer_id(entity), entity.title, getattr(entity, "username", None), kind,
                getattr(entity, "participants_count", None))


class UserGateway:
    """Resolve and publish using the same explicitly selected session."""

    def __init__(self, api_id, api_hash, session, telegram_id):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.telegram_id = telegram_id
        self._checked = None

    @asynccontextmanager
    async def _connection(self):
        _, _, errors, *_ = _telethon()
        try:
            client = _client(self.api_id, self.api_hash, self.session)
        except ValueError:
            raise TransportRejected(status=401) from None
        try:
            async with asyncio.timeout(45):
                await client.connect()
                user = await client.get_me()
                if user is None or user.bot or user.id != self.telegram_id:
                    raise TransportRejected(status=401)
                yield client, user
        except (errors.FloodWaitError, errors.FloodPremiumWaitError, errors.SlowModeWaitError) as error:
            raise TransportRejected(rate_limited=True, retry_after=max(1, error.seconds)) from None
        except errors.PeerFloodError:
            raise TransportRejected(rate_limited=True) from None
        except (errors.UnauthorizedError, errors.AuthKeyError):
            raise TransportRejected(status=401) from None
        except errors.ForbiddenError:
            raise TransportRejected(status=403) from None
        except errors.BadRequestError:
            raise TransportRejected(status=400) from None
        except (errors.RPCError, OSError, ValueError, TimeoutError):
            raise TransportUnavailable("Telegram не подтвердил результат операции.") from None
        finally:
            await client.disconnect()

    async def _entity(self, client, reference):
        if isinstance(reference, str):
            return await client.get_entity(reference)
        async for dialog in client.iter_dialogs():
            if dialog.id == reference:
                return dialog.entity
        raise TransportRejected(status=403)

    def identify(self):
        async def identify():
            async with self._connection() as (client, user):
                return _identity(user, await _avatar(client, user))
        return asyncio.run(identify())

    def dialogs(self):
        async def dialogs():
            async with self._connection() as (client, _):
                return [chat async for dialog in client.iter_dialogs() if (chat := _chat(dialog.entity))]
        return asyncio.run(dialogs())

    def resolve(self, reference):
        async def resolve():
            async with self._connection() as (client, _):
                return _chat(await self._entity(client, reference))
        return asyncio.run(resolve())

    def search(self, query):
        async def search():
            async with self._connection() as (client, _):
                functions = _telethon()[3]
                result = await client(functions.contacts.SearchRequest(q=query, limit=40))
                return [chat for entity in result.chats if (chat := _chat(entity))]
        return asyncio.run(search())

    def inspect(self, account, chat_id, thread_id):
        self._checked = None

        async def inspect():
            async with self._connection() as (client, user):
                entity = await self._entity(client, chat_id)
                chat = _chat(entity)
                if chat is None or chat.id != chat_id or thread_id is not None:
                    return Access(chat_id, "", user.id, False)
                if getattr(entity, "left", False) or getattr(entity, "kicked", False):
                    return Access(chat_id, chat.kind, user.id, False)
                if chat.kind == "channel":
                    rights = getattr(entity, "admin_rights", None)
                    can_text = bool(entity.creator or (rights and rights.post_messages))
                    can_photo = can_text
                elif entity.creator or getattr(entity, "admin_rights", None):
                    can_text = can_photo = True
                else:
                    restrictions = (getattr(entity, "default_banned_rights", None),
                                    getattr(entity, "banned_rights", None))
                    can_text = not any(r and (r.view_messages or r.send_messages or r.send_plain)
                                       for r in restrictions)
                    can_photo = can_text and not any(r and (r.send_media or r.send_photos)
                                                     for r in restrictions)
                if can_text:
                    self._checked = account, chat_id, await client.get_input_entity(entity), can_photo
                return Access(chat_id, chat.kind, user.id, can_text, can_photo)
        return asyncio.run(inspect())

    def send(self, account, chat_id, thread_id, text, button_url, *, photo=None):
        checked, self._checked = self._checked, None
        if (not checked or checked[:2] != (account, chat_id) or thread_id is not None
                or button_url is not None or (photo is not None and not checked[3])):
            raise TransportRejected(status=403)

        async def send():
            async with self._connection() as (client, _):
                if photo is None:
                    message = await client.send_message(checked[2], text, parse_mode=None, link_preview=False)
                else:
                    attachment = BytesIO(photo.content)
                    attachment.name = "publication.png" if photo.mime == "image/png" else "publication.jpg"
                    message = await client.send_file(checked[2], attachment, caption=text, parse_mode=None)
                if message.chat_id != chat_id:
                    raise TransportUnavailable("Telegram вернул сообщение для другого чата.")
                return message.id
        return asyncio.run(send())


def user_gateway(db, secrets, account, *, factory=UserGateway):
    row = db.execute("""SELECT api_id,telegram_user_id FROM accounts
                        WHERE id=? AND kind='user' AND disabled_at IS NULL""", (account,)).fetchone()
    api_hash = secrets.get(account, "api_hash")
    session = secrets.get(account, "mtproto_session")
    if row is None or not row[0] or not api_hash or not session:
        raise QueueConflict("Личный аккаунт нужно подключить заново.")
    return factory(row[0], api_hash.decode("ascii"), session.decode("ascii"), row[1])
