"""Local application operations shared by the Windows desktop screens."""
from dataclasses import dataclass
from pathlib import Path
from time import time

from .bot_transport import BotAPITransport
from . import campaigns, directory
from .chat_reference import parse_chat_reference
from .catalog import import_catalog, search_catalog
from .diagnostics import read_queue_status
from .executor import TransportRejected, TransportUnavailable, execute_one
from .job_queue import QueueConflict, control_job, reconcile, record_bot_hold, recover, revoke_venue, transaction
from .operator_workflow import enqueue_draft, list_bots, register_bot
from .photos import load_photo, store_photo
from .reports import export_report, queue_report
from .security import DPAPI, SecretStore
from .storage import open_profile
from .subscription_links import reconcile_subscription
from .telegram_accounts import Chat, UserGateway, login, user_gateway


@dataclass(frozen=True)
class DesktopService:
    profile: Path
    user_factory: object = UserGateway
    login_flow: object = login
    bot_factory: object = BotAPITransport

    def initialize(self):
        with open_profile(self.profile) as db:
            recover(db, int(time()), restart=True)

    def status(self):
        return read_queue_status(self.profile, operator_details=True)

    def revoke_consent(self, job_id):
        with open_profile(self.profile) as db:
            return revoke_venue(db, job_id, int(time()))

    def change_job(self, job_id, action):
        with open_profile(self.profile) as db:
            return control_job(db, job_id, action, int(time()))

    def review_job(self, job_id, receipt=None):
        with open_profile(self.profile) as db:
            return reconcile(db, job_id, int(time()), receipt=receipt)

    def close_subscription_review(self, request_id):
        with open_profile(self.profile) as db:
            return reconcile_subscription(db, DPAPI(), request_id, int(time()))

    def import_photo(self, source):
        with open_profile(self.profile) as db:
            return store_photo(db, DPAPI(), source)

    def preview_photo(self, digest):
        with open_profile(self.profile) as db:
            return load_photo(db, DPAPI(), digest).content

    def bots(self):
        with open_profile(self.profile) as db:
            return list_bots(db)

    def connect_bot(self, token):
        with open_profile(self.profile) as db:
            protector = DPAPI()
            return register_bot(db, protector, self.bot_factory(SecretStore(db, protector)), token)

    def lookup_chat(self, bot, reference):
        with open_profile(self.profile) as db:
            now = int(time())
            enabled = db.execute("""SELECT 1 FROM accounts
                                    WHERE id=? AND kind='bot' AND disabled_at IS NULL""", (bot,)).fetchone()
            if not enabled:
                raise QueueConflict("Сначала переподключите бота.")
            hold = db.execute("""SELECT 1 FROM bot_holds WHERE bot_account_id=?
                                 AND (until_utc IS NULL OR until_utc>?)""", (bot, now)).fetchone()
            if hold:
                raise QueueConflict("Для бота действует ограничение Telegram. Повторите после его окончания.")
            protector = DPAPI()
            transport = BotAPITransport(SecretStore(db, protector))
            try:
                return transport.lookup_chat(bot, reference)
            except TransportRejected as error:
                with transaction(db):
                    if error.reason == "telegram_rate_limited":
                        record_bot_hold(db, bot, int(time()), error.retry_after)
                    elif error.reason == "telegram_unauthorized":
                        db.execute("UPDATE accounts SET disabled_at=?,status='unauthorized' WHERE id=?",
                                   (int(time()), bot))
                raise

    def enqueue(self, draft):
        with open_profile(self.profile) as db:
            return enqueue_draft(db, DPAPI(), draft, int(time()))

    def dispatch(self, job_id=None, *, auto_only=False, stop=None):
        with open_profile(self.profile) as db:
            protector = DPAPI()
            transport = _AssignedPublisher(self, db, protector)
            return execute_one(db, protector, transport, lambda: int(time()), job_id=job_id,
                               auto_only=auto_only, stop=stop)

    def preview_job(self, job_id):
        import json

        with open_profile(self.profile) as db:
            row = db.execute("SELECT canonical_chat_id,payload FROM jobs WHERE id=? AND state='queued'",
                             (job_id,)).fetchone()
            if row is None:
                raise ValueError("Задание больше не в очереди.")
            return row[0], json.loads(DPAPI().unprotect(row[1]))

    def import_candidates(self, candidates):
        with open_profile(self.profile) as db:
            return import_catalog(db, DPAPI(), candidates, int(time()))

    def search_venues(self, query):
        with open_profile(self.profile) as db:
            return search_catalog(db, DPAPI(), query)

    def report(self):
        with open_profile(self.profile) as db:
            return queue_report(db)

    def export_report(self, destination):
        with open_profile(self.profile) as db:
            return export_report(db, destination)

    def account_rows(self):
        with open_profile(self.profile) as db:
            return directory.account_rows(db)

    def add_user(self, api_id, api_hash, method, phone, prompt, present_qr, *, cancel=None):
        identity, session = self.login_flow(api_id, api_hash, method, phone, prompt, present_qr, cancel=cancel)
        if cancel is not None and cancel.is_set():
            return None
        with open_profile(self.profile) as db:
            return directory.store_user(db, SecretStore(db, DPAPI()), identity, api_id, api_hash, session)

    def disable_account(self, account, erase=False):
        with open_profile(self.profile) as db:
            if erase:
                db.execute("PRAGMA secure_delete=ON")
            directory.disable(db, SecretStore(db, DPAPI()), account, int(time()), erase=erase)

    def _publisher(self, db, protector, account):
        row = db.execute("SELECT kind FROM accounts WHERE id=? AND disabled_at IS NULL", (account,)).fetchone()
        if row is None:
            raise QueueConflict("Подключите выбранного отправителя заново.")
        secrets = SecretStore(db, protector)
        if row[0] == "bot":
            return self.bot_factory(secrets)
        return user_gateway(db, secrets, account, factory=self.user_factory)

    def _with_publisher(self, db, protector, account, operation):
        directory.check_hold(db, account, int(time()))
        try:
            return operation(self._publisher(db, protector, account))
        except TransportRejected as error:
            directory.record_rejection(db, account, error, int(time()))
            raise

    def check_account(self, account):
        with open_profile(self.profile) as db:
            def check(transport):
                if hasattr(transport, "account_identity"):
                    result = transport.account_identity(account)
                    expected = db.execute("SELECT telegram_user_id FROM accounts WHERE id=?", (account,)).fetchone()[0]
                    if result["id"] != expected:
                        raise TransportRejected(status=401)
                    db.execute("UPDATE accounts SET label=?,username=? WHERE id=?",
                               (result.get("first_name") or f"Бот {expected}", result.get("username"), account))
                else:
                    identity = transport.identify()
                    expected = db.execute("SELECT telegram_user_id FROM accounts WHERE id=?", (account,)).fetchone()[0]
                    if identity.telegram_id != expected:
                        raise TransportRejected(status=401)
                    db.execute("UPDATE accounts SET label=?,username=?,avatar=? WHERE id=?",
                               (identity.name, identity.username, identity.avatar, account))
            self._with_publisher(db, DPAPI(), account, check)
            return True

    def live_chats(self, account, query=None):
        with open_profile(self.profile) as db:
            def lookup(transport):
                if not hasattr(transport, "dialogs"):
                    raise QueueConflict("Список диалогов Telegram доступен для личного аккаунта.")
                return transport.dialogs() if query is None else transport.search(query)
            rows = self._with_publisher(db, DPAPI(), account, lookup)
            if query is None:
                db.execute("UPDATE accounts SET last_sync_at=?,dialog_count=? WHERE id=?",
                           (int(time()), len(rows), account))
            return rows

    def resolve_chats(self, account, inputs):
        results, errors, seen = [], [], set()
        with open_profile(self.profile) as db:
            protector = DPAPI()
            for raw, name, category in inputs:
                try:
                    ref = parse_chat_reference(raw)
                    if isinstance(ref, int):
                        chat = Chat(ref, name, None, None)
                    elif not account:
                        raise QueueConflict("Для ссылки или имени выберите подключённый аккаунт.")
                    else:
                        chat = self._with_publisher(db, protector, account,
                                                    lambda p: directory.resolve(p, account, ref))
                    if chat.id in seen:
                        errors.append(f"{raw}: дубликат")
                        continue
                    seen.add(chat.id)
                    if name and not chat.title:
                        chat = Chat(chat.id, name, chat.username, chat.kind, chat.participants)
                    results.append((chat, category))
                except TransportRejected as error:
                    if error.reason in ("telegram_rate_limited", "telegram_unauthorized"):
                        raise
                    errors.append(f"{raw}: Telegram не предоставил доступ")
                except (ValueError, QueueConflict, TransportUnavailable) as error:
                    errors.append(f"{raw}: {error}")
        return results, errors

    def save_chat_selection(self, account, items, *, member=False):
        with open_profile(self.profile) as db, transaction(db):
            protector, now = DPAPI(), int(time())
            for chat, category in items:
                venue = directory.save_chat(db, protector, chat, now, category=category)
                if account and member and not db.execute(
                        "SELECT 1 FROM venue_access WHERE venue_id=? AND account_id=?", (venue, account)).fetchone():
                    directory.observe(db, venue, account, None, now)

    def venue_rows(self):
        with open_profile(self.profile) as db:
            return directory.venue_rows(db, DPAPI())

    def set_venue(self, venue, **options):
        with open_profile(self.profile) as db:
            directory.set_venue(db, venue, now=int(time()), **options)

    def inspect_venue(self, account, venue):
        with open_profile(self.profile) as db:
            chat = db.execute("SELECT canonical_chat_id FROM venues WHERE id=?", (venue,)).fetchone()
            if not chat or chat[0] is None:
                raise QueueConflict("Нужен подтверждённый ID площадки.")
            access = self._with_publisher(db, DPAPI(), account, lambda p: p.inspect(account, chat[0], None))
            directory.observe(db, venue, account, access, int(time()))
            return access

    def campaign_list(self):
        with open_profile(self.profile) as db:
            return campaigns.campaign_list(db)

    def save_campaign(self, draft, data):
        with open_profile(self.profile) as db:
            return campaigns.save_draft(db, DPAPI(), draft, data, int(time()))

    def load_campaign(self, draft):
        with open_profile(self.profile) as db:
            return campaigns.load_draft(db, DPAPI(), draft)

    def control_campaign(self, draft, action):
        allowed = {"pause": ("queued",), "resume": ("paused",), "cancel": ("queued", "paused")}
        if action not in allowed:
            raise ValueError("Неизвестное действие кампании.")
        with open_profile(self.profile) as db, transaction(db):
            jobs = db.execute("""SELECT j.id,j.state FROM jobs j JOIN campaign_editor e
                               ON e.published_campaign=j.campaign_id WHERE e.id=?""", (draft,)).fetchall()
            changed, blocked = 0, 0
            for job, state in jobs:
                if state in allowed[action]:
                    try:
                        control_job(db, job, action, int(time()))
                    except QueueConflict:
                        blocked += 1
                    else:
                        changed += 1
            return changed, blocked

    def approve_campaign(self, draft, data, targets, automatic, *, preflight_only=False):
        with open_profile(self.profile) as db:
            protector, proof, failures = DPAPI(), {}, []
            for target in targets:
                account, venue = target["account"], target["venue"]
                name = db.execute("SELECT title FROM venue_directory WHERE venue_id=?", (venue,)).fetchone()
                name = name[0] if name and name[0] else "Площадка"
                try:
                    chat, title, kind, identity = campaigns.validate_target(db, target, int(time()))
                    campaigns.render(data, kind, title)
                    access = self._with_publisher(db, protector, account, lambda p: p.inspect(account, chat, None))
                    directory.observe(db, venue, account, access, int(time()))
                    if (access.chat_id != chat or access.telegram_bot_id != identity or not access.can_publish
                            or (data.get("photo") and not access.can_publish_photo)):
                        raise QueueConflict("Недостаточно прав для публикации.")
                    proof[venue, account] = access
                except (ValueError, QueueConflict) as error:
                    failures.append(f"{name}: {error}")
                except TransportRejected as error:
                    reason = {"telegram_rate_limited": "действует ограничение Telegram",
                              "telegram_unauthorized": "нужен повторный вход",
                              "telegram_forbidden": "нет доступа к площадке"}.get(error.reason, "запрос отклонён")
                    failures.append(f"{name}: {reason}.")
                except TransportUnavailable:
                    failures.append(f"{name}: Telegram не подтвердил права.")
            if failures:
                raise QueueConflict("\n".join(failures[:12]) + (f"\nВсего проблем: {len(failures)}." if len(failures) > 12 else ""))
            if preflight_only:
                return len(proof)
            return campaigns.approve(db, protector, draft, data, targets, proof, int(time()), automatic=automatic)

    def template_list(self):
        with open_profile(self.profile) as db:
            return campaigns.template_list(db)

    def template_versions(self, template):
        with open_profile(self.profile) as db:
            return campaigns.template_versions(db, DPAPI(), template)

    def save_template(self, template, label, text):
        with open_profile(self.profile) as db:
            return campaigns.save_template(db, DPAPI(), template, label, text, int(time()))

    def archive_template(self, template):
        with open_profile(self.profile) as db:
            db.execute("UPDATE editable_templates SET archived=1 WHERE id=?", (template,))

    def pause_automatic(self):
        with open_profile(self.profile) as db, transaction(db):
            return db.execute("UPDATE jobs SET state='paused' WHERE auto_approved=1 AND state='queued'").rowcount

    def journal(self):
        with open_profile(self.profile) as db:
            return db.execute("""SELECT COALESCE(finished_at,claimed_at),outcome,reason_code,job_id
                                 FROM job_attempts ORDER BY claimed_at DESC LIMIT 100""").fetchall()


class _AssignedPublisher:
    def __init__(self, service, db, protector):
        self.service, self.db, self.protector = service, db, protector
        self.transport = None

    def inspect(self, account, chat_id, thread_id):
        self.transport = self.service._publisher(self.db, self.protector, account)
        return self.transport.inspect(account, chat_id, thread_id)

    def send(self, account, chat_id, thread_id, text, button_url, **options):
        return self.transport.send(account, chat_id, thread_id, text, button_url, **options)

