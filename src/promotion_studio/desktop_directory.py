"""Account management and the authorized destination catalog."""
import asyncio
import csv
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from time import time

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from .desktop_forms import confirm
from .desktop_style import label, page, polish_table
from .executor import TransportRejected, TransportUnavailable
from .job_queue import QueueConflict
from .telegram_accounts import LoginError


STATES = dict(ready="Подключён", disabled="Отключён", removed="Сессия удалена", unauthorized="Нужен повторный вход")
DECISIONS = dict(review="На проверке", allowed="Разрешено", denied="Запрещено")
KINDS = dict(channel="Канал", group="Группа", supergroup="Супергруппа")


def local_time(value):
    return datetime.fromtimestamp(value).astimezone().strftime("%d.%m %H:%M") if value else "—"


def table(headers, *, multi=False):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if multi
                            else QAbstractItemView.SelectionMode.SingleSelection)
    polish_table(widget)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return widget


def fill(widget, rows):
    widget.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value) if value is not None else "—")
            widget.setItem(row, column, item)


def rows_selected(widget, rows):
    return [rows[index.row()] for index in widget.selectionModel().selectedRows()]


def account_choice(parent, accounts, *, users_only=False, optional=False):
    active = [a for a in accounts if a["status"] == "ready" and (not users_only or a["kind"] == "user")]
    if not active:
        if not optional:
            QMessageBox.information(parent, "Отправитель", "Сначала подключите подходящий аккаунт в разделе «Аккаунты».")
        return None
    options = [f"{a['name']} · {a['username'] or a['id'][:6]}" for a in active]
    if optional:
        options.insert(0, "Без аккаунта · только числовые ID")
    selection, ok = QInputDialog.getItem(parent, "Аккаунт Telegram", "Выберите аккаунт", options, 0, False)
    if not ok:
        return False
    index = options.index(selection) - int(optional)
    return active[index]["id"] if index >= 0 else None


class LoginWorker(QThread):
    question = Signal(str)
    qr = Signal(str)
    result = Signal(str)
    failure = Signal(str)

    def __init__(self, parent, service, setup):
        super().__init__(parent)
        self.service, self.setup = service, setup
        self.cancel = Event()
        self.answers = Queue()

    async def ask(self, kind):
        self.question.emit(kind)
        while not self.cancel.is_set():
            try:
                return self.answers.get_nowait()
            except Empty:
                await asyncio.sleep(0.1)
        raise LoginError("Вход отменён.")

    def run(self):
        try:
            account = self.service.add_user(*self.setup, self.ask, lambda url, _: self.qr.emit(url),
                                            cancel=self.cancel)
            if account:
                self.result.emit(account)
        except (LoginError, QueueConflict) as error:
            self.failure.emit(str(error))
        except Exception:
            self.failure.emit("Не удалось завершить вход или сохранить защищённую сессию.")
        finally:
            self.setup = None


class LoginDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.service = window.service
        self.worker = None
        self.succeeded = False
        self.setWindowTitle("Добавить личный аккаунт")
        self.resize(440, 330)
        layout = QVBoxLayout(self)
        info = label("", "muted", wrap=True)
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setOpenExternalLinks(True)
        info.setText('Откройте <a href="https://my.telegram.org">my.telegram.org</a>, затем API development tools. '
                     'Скопируйте API ID и API Hash своего приложения.')
        layout.addWidget(info)
        form = QFormLayout()
        layout.addLayout(form)
        self.api_id, self.api_hash, self.phone = QLineEdit(), QLineEdit(), QLineEdit()
        self.api_id.setPlaceholderText("Число из API development tools")
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.phone.setPlaceholderText("+380…")
        self.method = QComboBox()
        self.method.addItems(["QR в приложении Telegram", "Номер телефона"])
        for caption, widget in (("API ID", self.api_id), ("API Hash", self.api_hash),
                                ("Способ входа", self.method), ("Номер", self.phone)):
            form.addRow(caption, widget)
        self.phone.setEnabled(False)
        self.method.currentIndexChanged.connect(lambda index: self.phone.setEnabled(bool(index)))
        self.banner = label("", "muted", wrap=True)
        self.image = label("")
        self.answer = QLineEdit()
        self.answer.setEchoMode(QLineEdit.EchoMode.Password)
        self.answer.hide()
        layout.addWidget(self.banner)
        layout.addWidget(self.image, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.answer)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.proceed = buttons.addButton("Подключить", QDialogButtonBox.ButtonRole.ActionRole)
        self.proceed.setProperty("variant", "primary")
        self.proceed.clicked.connect(self.advance)
        self.answer.returnPressed.connect(self.advance)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def advance(self):
        if self.worker:
            if self.answer.isVisible() and self.answer.text():
                self.worker.answers.put(self.answer.text().strip() if self.answer.placeholderText() == "Код Telegram"
                                        else self.answer.text())
                self.answer.clear()
                self.answer.hide()
                self.proceed.setEnabled(False)
            return
        try:
            api_id = int(self.api_id.text())
        except ValueError:
            self.banner.setText("API ID должен быть числом.")
            return
        setup = (api_id, self.api_hash.text().strip(), "phone" if self.method.currentIndex() else "qr",
                 self.phone.text().strip())
        self.api_hash.clear()
        self.worker = LoginWorker(self, self.service, setup)
        self.worker.qr.connect(self.show_qr)
        self.worker.question.connect(self.ask)
        self.worker.failure.connect(self.banner.setText)
        self.worker.result.connect(self.accept_result)
        self.worker.finished.connect(self.finished_worker)
        self.proceed.setEnabled(False)
        self.banner.setText("Подключение к Telegram…")
        self.worker.start()

    def show_qr(self, url):
        import qrcode
        code = qrcode.QRCode(box_size=1, border=4)
        code.add_data(url)
        matrix = code.get_matrix()
        image = QImage(len(matrix), len(matrix), QImage.Format.Format_RGB32)
        for y, row in enumerate(matrix):
            for x, bit in enumerate(row):
                image.setPixelColor(x, y, QColor("black" if bit else "white"))
        self.image.setPixmap(QPixmap.fromImage(image).scaled(204, 204))
        self.banner.setText("В Telegram на телефоне откройте Настройки → Устройства → Подключить устройство.")

    def ask(self, kind):
        self.image.clear()
        self.answer.setPlaceholderText("Код Telegram" if kind == "code" else "Пароль двухэтапной проверки")
        self.banner.setText("Введите код из Telegram." if kind == "code" else "Введите пароль двухэтапной проверки.")
        self.answer.show()
        self.answer.setFocus()
        self.proceed.setText("Продолжить")
        self.proceed.setEnabled(True)

    def accept_result(self, _):
        self.succeeded = True

    def finished_worker(self):
        cancelled = self.worker.cancel.is_set()
        self.worker.deleteLater()
        self.worker = None
        self.image.clear()
        self.answer.clear()
        self.answer.hide()
        self.proceed.setText("Подключить")
        self.proceed.setEnabled(True)
        if cancelled:
            super().reject()
        elif self.succeeded:
            super().accept()

    def reject(self):
        if self.worker:
            self.banner.setText("Отмена входа…")
            self.worker.cancel.set()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.worker:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)


class AccountsPage:
    def __init__(self, window):
        self.window, self.service = window, window.service
        self.rows = []
        actions = QHBoxLayout()
        window._button(actions, "Добавить бота", window.connect_bot)
        window._button(actions, "Добавить аккаунт", self.add_user, variant="primary")
        self.widget, layout = page("Аккаунты", "Подключённые отправители и доступ к Telegram.", actions=actions)
        self.table = table(["Аккаунт", "Тип", "Состояние", "Чаты", "Синхронизация", "Ограничение"])
        layout.addWidget(self.table, 1)
        controls = QHBoxLayout()
        layout.addLayout(controls)
        for caption, handler in (("Проверить соединение", self.check), ("Синхронизировать", self.sync),
                                 ("Показать площадки", self.show_venues), ("Отключить", self.disable),
                                 ("Удалить локальную сессию", self.erase)):
            window._button(controls, caption, handler)
        controls.addStretch(1)
        self.message = label("", "muted")
        layout.addWidget(self.message)

    def refresh(self):
        self.rows = self.service.account_rows()
        fill(self.table, [
            (f"{a['name']}  @{a['username']}" if a["username"] else a["name"],
             "Бот" if a["kind"] == "bot" else "Личный", STATES.get(a["status"], a["status"]),
             a["venues"], local_time(a["synced"]),
             (local_time(a["until"]) if a["until"] else "Нужна проверка")
             if a["held"] and (not a["until"] or a["until"] > int(time())) else "—") for a in self.rows])
        for row, account in enumerate(self.rows):
            if account["avatar"]:
                pixmap = QPixmap()
                if pixmap.loadFromData(account["avatar"]):
                    self.table.item(row, 0).setIcon(QIcon(pixmap))
        self.message.setText("" if self.rows else "Подключите личный аккаунт для синхронизации чатов или добавьте бота.")

    def selected(self):
        rows = rows_selected(self.table, self.rows)
        if rows:
            return rows[0]
        self.message.setText("Выберите аккаунт в таблице.")
        return None

    def add_user(self):
        if self.window.worker is None and LoginDialog(self.window).exec() == QDialog.DialogCode.Accepted:
            self.window.refresh_all()

    def check(self):
        if account := self.selected():
            self.window._run_network(lambda: self.service.check_account(account["id"]),
                                     lambda _: self.message.setText("Соединение подтверждено."))

    def sync(self):
        if account := self.selected():
            if account["kind"] != "user":
                self.message.setText("У Bot API нет списка диалогов. Добавьте известную боту площадку по ссылке.")
            else:
                self.window.venues.sync(account=account["id"])

    def show_venues(self):
        if account := self.selected():
            self.window.venues.account_filter = account["id"]
            self.window.navigate("catalog")

    def disable(self):
        self.change(False)

    def erase(self):
        self.change(True)

    def change(self, erase):
        if (account := self.selected()) and confirm(
                self.window, "Удалить локальную сессию?" if erase else "Отключить аккаунт?",
                "Задания отправителя перейдут на паузу. Для отзыва сеанса в Telegram откройте «Устройства»."
                if erase else "Задания отправителя перейдут на паузу."):
            self.window._apply(lambda: self.service.disable_account(account["id"], erase))
            self.window.refresh_all()


def import_rows(text, is_csv=False):
    if len(text.encode("utf-8")) > 1_000_000:
        raise ValueError("Импорт ограничен 1 МБ.")
    if is_csv:
        reader = csv.DictReader(text.splitlines(), strict=True)
        column = next((name for name in ("chat_id", "link", "username") if name in (reader.fieldnames or ())), None)
        if not column:
            raise ValueError("В CSV нужна колонка chat_id, link или username.")
        rows = [((r.get(column) or "").strip(), r.get("name") or "", r.get("category") or "") for r in reader]
    else:
        rows = [(line.strip(), "", "") for line in text.splitlines() if line.strip()]
    if len(rows) > 500:
        raise ValueError("За один импорт можно обработать до 500 записей.")
    return rows


class VenuesPage:
    def __init__(self, window):
        self.window, self.service = window, window.service
        self.rows, self.account_filter = [], None
        actions = QHBoxLayout()
        window._button(actions, "Импорт", self.import_dialog)
        window._button(actions, "Мои чаты", self.sync)
        window._button(actions, "Добавить", self.add, variant="primary")
        self.widget, layout = page("Площадки", "Доступ Telegram и согласие на рекламу проверяются отдельно.", actions=actions)
        filters = QHBoxLayout()
        layout.addLayout(filters)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Название, username, ID или категория")
        self.search.textChanged.connect(self.refresh)
        filters.addWidget(self.search, 1)
        self.filter = QComboBox()
        self.filter.addItems(["Все площадки", "Разрешено", "На проверке", "Запрещено", "Избранное", "Исключённые"])
        self.filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.filter)
        window._button(filters, "Сбросить", self.reset)
        window._button(filters, "Поиск в Telegram", self.public_search)
        self.table = table(["Площадка", "Username", "Тип", "Категория", "Участники", "Доступные аккаунты",
                            "Права", "Размещение", "Последний пост", "Статус"], multi=True)
        layout.addWidget(self.table, 1)
        controls = QHBoxLayout()
        layout.addLayout(controls)
        for name, handler in (("Условия", self.edit), ("Проверить права", self.check),
                              ("Открыть в Telegram", self.open_chat), ("В кампанию", self.to_campaign)):
            window._button(controls, name, handler)
        controls.addStretch(1)
        self.message = label("", "muted", wrap=True)
        layout.addWidget(self.message)

    def reset(self):
        self.account_filter = None
        self.search.clear()
        self.filter.setCurrentIndex(0)
        self.refresh()

    def refresh(self):
        if self.window.worker is not None:
            return
        query = self.search.text().strip().casefold()
        mode = self.filter.currentIndex()
        self.rows = []
        for row in self.service.venue_rows():
            if query and not any(query in str(row[key] or "").casefold()
                                 for key in ("title", "username", "chat", "category")):
                continue
            if ((mode in (1, 2, 3) and row["decision"] != ("allowed", "review", "denied")[mode - 1])
                    or (mode == 4 and not row["favorite"]) or (mode == 5 and not row["excluded"])
                    or (self.account_filter and not any(a[0] == self.account_filter for a in row["access"]))):
                continue
            self.rows.append(row)
        fill(self.table, [(r["title"], r["username"], KINDS.get(r["kind"], "—"), r["category"], r["count"],
                           ", ".join(a[1] for a in r["access"]) or "—",
                           ", ".join(f"{a[1]}: " + ("Текст и фото" if a[3] == 1 else "Текст"
                                     if a[2] == 1 else "Нет" if a[2] == 0 else "Не проверены")
                                     for a in r["access"]) or "Не проверены",
                           DECISIONS[r["decision"]], local_time(r["sent"]),
                           "Исключена" if r["excluded"] else "Избранная" if r["favorite"] else "В каталоге")
                          for r in self.rows])
        self.message.setText(f"Найдено: {len(self.rows)}" + (" · фильтр по аккаунту" if self.account_filter else ""))

    def _pick(self, account, items, errors=(), *, member=False):
        if not items:
            self.message.setText("\n".join(errors[:5]) or "Telegram не вернул площадки.")
            return
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Сохранить площадки")
        dialog.resize(650, 430)
        layout = QVBoxLayout(dialog)
        if errors:
            layout.addWidget(label("\n".join(errors[:5]) + (f"\nВсего замечаний: {len(errors)}" if len(errors) > 5 else ""),
                                   "muted", wrap=True))
        grid = table(["Площадка", "Username", "Тип", "ID"], multi=True)
        fill(grid, [(c.title or c.id, c.username, KINDS.get(c.kind, "—"), c.id) for c, _ in items])
        grid.selectAll()
        layout.addWidget(grid)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = rows_selected(grid, items)
            if selected:
                self.window._run_network(lambda: self.service.save_chat_selection(account, selected, member=member),
                                         lambda _: None)

    def sync(self, _=False, *, account=None):
        if self.window.worker is not None:
            return
        account = account or account_choice(self.window, self.service.account_rows(), users_only=True)
        if account:
            self.window._run_network(lambda: self.service.live_chats(account),
                                     lambda chats: self._pick(account, [(c, "") for c in chats], member=True))

    def public_search(self):
        if self.window.worker is not None:
            return
        account = account_choice(self.window, self.service.account_rows(), users_only=True)
        if not account:
            return
        query, ok = QInputDialog.getText(self.window, "Поиск в Telegram", "Ключевое слово. Выдача Telegram ограничена.")
        if ok and query.strip():
            self.window._run_network(lambda: self.service.live_chats(account, query.strip()),
                                     lambda chats: self._pick(account, [(c, "") for c in chats]))

    def add(self):
        entry, ok = QInputDialog.getText(self.window, "Добавить площадку", "@username, ссылка на пост или ID")
        if ok and entry.strip():
            self.lookup([(entry.strip(), "", "")])

    def import_dialog(self):
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Импорт площадок")
        dialog.resize(550, 390)
        layout = QVBoxLayout(dialog)
        source = QPlainTextEdit()
        source.setPlaceholderText("Одна ссылка, @username или числовой ID на строку")
        layout.addWidget(source)
        file = QPushButton("Открыть TXT или CSV")
        layout.addWidget(file)
        is_csv = False

        def load():
            nonlocal is_csv
            filename, _ = QFileDialog.getOpenFileName(dialog, "Открыть список", "", "*.txt *.csv")
            if filename:
                try:
                    path = Path(filename)
                    if path.stat().st_size > 1_000_000:
                        raise ValueError("Файл больше 1 МБ.")
                    source.setPlainText(path.read_text(encoding="utf-8-sig"))
                    is_csv = path.suffix.lower() == ".csv"
                except (OSError, ValueError) as error:
                    QMessageBox.warning(dialog, "Импорт", str(error))

        file.clicked.connect(load)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.lookup(import_rows(source.toPlainText(), is_csv))
            except (ValueError, csv.Error) as error:
                self.message.setText(str(error))

    def lookup(self, rows):
        if not rows or self.window.worker is not None:
            return
        account = account_choice(self.window, self.service.account_rows(), optional=True)
        if account is not False:
            self.window._run_network(lambda: self.service.resolve_chats(account, rows),
                                     lambda result: self._pick(account, *result))

    def edit(self):
        selected = rows_selected(self.table, self.rows)
        if not selected:
            return
        venue = selected[0]
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Условия площадки")
        form = QFormLayout(dialog)
        category = QLineEdit(venue["category"])
        decision = QComboBox()
        decision.addItems(list(DECISIONS.values()))
        decision.setCurrentIndex(list(DECISIONS).index(venue["decision"]))
        favorite, excluded = QCheckBox(), QCheckBox()
        favorite.setChecked(venue["favorite"])
        excluded.setChecked(venue["excluded"])
        for caption, field in (("Категория", category), ("Реклама", decision), ("Избранная", favorite),
                               ("Исключена", excluded)):
            form.addRow(caption, field)
        form.addRow(label("Для запуска потребуется указать согласовавшего и срок согласия.", "muted"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.window._apply(lambda: self.service.set_venue(
                venue["id"], category=category.text().strip(), decision=list(DECISIONS)[decision.currentIndex()],
                favorite=favorite.isChecked(), excluded=excluded.isChecked()))
            self.refresh()

    def check(self):
        selected = rows_selected(self.table, self.rows)
        if selected and self.window.worker is None:
            account = account_choice(self.window, self.service.account_rows())
            if account:
                def operation():
                    return [self.service.inspect_venue(account, r["id"]) for r in selected]
                self.window._run_network(operation, lambda hits: self.message.setText(
                    f"Права подтверждены на {sum(a.can_publish for a in hits)} из {len(hits)} площадок."))

    def open_chat(self):
        if selected := rows_selected(self.table, self.rows):
            username = selected[0]["username"]
            if username:
                QDesktopServices.openUrl(QUrl(f"https://t.me/{username}"))
            else:
                self.message.setText("У площадки нет публичного имени. Откройте её из диалогов Telegram.")

    def to_campaign(self):
        if selected := rows_selected(self.table, self.rows):
            self.window.campaigns.add_venues(selected)
            self.window.navigate("campaigns")
