"""PySide6 view for the local publication queue."""
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Event
from time import time

from PySide6.QtCore import QDateTime, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDateTimeEdit, QDialog, QDialogButtonBox,
                               QButtonGroup, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QStackedWidget, QVBoxLayout, QWidget)

from .desktop_service import DesktopService
from .desktop_forms import confirm, get_bot_token
from .desktop_campaigns import CampaignPage, TemplatesPage
from .desktop_directory import AccountsPage, VenuesPage
from .backup import BackupError, create_backup, restore_backup
from .desktop_home import Dashboard
from .desktop_panels import WorkspacePanels
from .desktop_style import STYLE, apply_app_theme, card, empty_state, icon, label, page, polish_table
from .executor import TransportRejected, TransportUnavailable
from .job_queue import QueueConflict
from .operator_workflow import DraftError
from .photos import PhotoError
from .security import SecretError
from .storage import MigrationError, ProfileBusy


STATE_LABELS = {
    "queued": "В очереди", "claimed": "Проверка", "dispatching": "Отправляется",
    "paused": "На паузе", "needs_review": "Нужна сверка", "sent": "Отправлено",
    "cancelled": "Отменено", "created": "Создана", "rejected": "Отклонена", "closed": "Закрыта",
}
JOB_ACTIONS = {
    "queued": {"pause", "cancel", "dispatch"},
    "paused": {"resume", "cancel"},
    "needs_review": {"receipt", "close_review"},
}
REASON_LABELS = {
    "missing_consent": "Согласие площадки отозвано, истекло или не подходит для этой публикации.",
    "missing_publish_rights": "У бота нет права публиковать в этом чате.",
    "missing_photo_consent": "Согласие площадки не распространяется на фото.",
    "missing_photo_rights": "У бота нет права отправлять фото в этом чате.",
    "telegram_rate_limited": "Telegram ограничил запросы бота.",
    "telegram_unauthorized": "Telegram отклонил токен. Подключите бота заново.",
    "telegram_forbidden": "Telegram запретил операцию. Проверьте доступ бота к площадке.",
    "conflicting_publication": "Интервал между публикациями ещё не прошёл или есть отправка на сверке.",
    "outside_allowed_window": "Текущее время вне согласованного окна площадки.",
    "rights_unavailable": "Проверить права бота сейчас не удалось.",
    "identity_mismatch": "Проверенный бот или чат не совпал с утверждённым заданием.",
    "invalid_snapshot": "Снимок публикации или её вложение недоступны.",
    "operator_stopped": "Отправка остановлена оператором.",
}
EXPECTED_ERRORS = (OSError, sqlite3.Error, MigrationError, ProfileBusy, QueueConflict, PhotoError, SecretError, BackupError,
                   OverflowError, ValueError)

class NetworkAction(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, parent, action):
        super().__init__(parent)
        self.action = action

    def run(self):
        try:
            result = self.action()
        except (ValueError, QueueConflict) as error:
            self.failed.emit(str(error))
        except TransportRejected as error:
            self.failed.emit(REASON_LABELS.get(error.reason, "Telegram отклонил запрос."))
        except (*EXPECTED_ERRORS, TransportUnavailable):
            self.failed.emit("Не удалось завершить операцию. Проверьте доступ к Telegram или локальному профилю.")
        except Exception:
            self.failed.emit("Операция завершилась внутренней ошибкой.")
        else:
            self.completed.emit(result)
        finally:
            self.action = None


def _local_time(epoch):
    if epoch is None:
        return "—"
    try:
        return datetime.fromtimestamp(epoch).astimezone().strftime("%d.%m.%Y %H:%M %Z")
    except (ValueError, OSError, OverflowError):
        return "Некорректное время"


def _get_receipt(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Сверка публикации")
    form = QFormLayout(dialog)
    chat = QLineEdit()
    message = QLineEdit()
    sent = QDateTimeEdit(QDateTime.currentDateTime())
    sent.setDisplayFormat("dd.MM.yyyy HH:mm:ss")
    sent.setCalendarPopup(True)
    form.addRow("ID чата", chat)
    form.addRow("ID сообщения", message)
    form.addRow("Время отправки (местное)", sent)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return int(chat.text()), int(message.text()), sent.dateTime().toSecsSinceEpoch()


class StudioWindow(QMainWindow):
    def __init__(self, service):
        super().__init__()
        apply_app_theme(QApplication.instance())
        self.service = service
        self.records = {"jobs": [], "subscriptions": []}
        self.buttons = {}
        self.worker = None
        self.auto_active = False
        self.stop_event = Event()
        self.pause_pending = False
        self.network_result = None
        self.network_error = None
        self.setWindowTitle("Telegram Promotion Studio")
        self.resize(1240, 790)
        self.setMinimumSize(900, 625)
        self.setStyleSheet(STYLE)

        root = QWidget()
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(174)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(15, 21, 15, 16)
        sidebar_layout.setSpacing(5)
        shell.addWidget(sidebar)

        brand = QHBoxLayout()
        brand.setContentsMargins(4, 0, 0, 0)
        mark = QLabel()
        mark.setPixmap(icon("brand").pixmap(24, 24))
        brand.addWidget(mark)
        brand.addWidget(label("Studio", "brand"))
        brand.addStretch(1)
        sidebar_layout.addLayout(brand)
        sidebar_layout.addSpacing(23)

        self.stack = QStackedWidget()
        self.stack.setObjectName("workspace")
        shell.addWidget(self.stack, 1)
        self.pages = {}
        self.nav_buttons = {}
        self.nav_group = QButtonGroup(self)
        for key, title, graphic in (("home", "Главная", "home"), ("campaigns", "Кампании", "jobs"),
                                    ("catalog", "Площадки", "catalog"), ("templates", "Шаблоны", "templates"),
                                    ("accounts", "Аккаунты", "accounts"), ("report", "Аналитика", "report"),
                                    ("settings", "Настройки", "settings")):
            button = self._button(sidebar_layout, title, lambda _, destination=key: self.navigate(destination),
                                  variant="nav", graphic=graphic)
            button.setCheckable(True)
            self.nav_group.addButton(button)
            self.nav_buttons[key] = button

        sidebar_layout.addStretch(1)
        self.auto_button = self._button(sidebar_layout, "Расписание", self.arm_scheduler)
        self.stop_button = self._button(sidebar_layout, "Стоп отправки", self.emergency_stop, variant="danger")
        self.stop_button.setToolTip("Выключить расписание и поставить автоматические задания на паузу")
        self.auto_status = label("Расписание выключено", "muted", wrap=True)
        sidebar_layout.addWidget(self.auto_status)

        self.dashboard = Dashboard(self)
        self._add_page("home", self.dashboard.widget)
        self.tables = {}
        self.queue_views = {}
        self.action_bars = {}
        self.empty_ctas = {}
        self._create_queue_page("jobs", "Публикации", "Подготовленные объявления и история отправки.",
                                "Запланировано", "Публикаций пока нет",
                                "Создайте объявление, чтобы проверить его и добавить в очередь.")
        self._create_queue_page("subscriptions", "Платные ссылки", "Статусы создания и ручной сверки ссылок.",
                                "Запрошено", "Запросов на ссылки пока нет",
                                "Здесь появятся запросы на платные ссылки и результаты их сверки.")
        self.campaigns = CampaignPage(self)
        self._add_page("campaigns", self.campaigns.widget)
        self.accounts = AccountsPage(self)
        self._add_page("accounts", self.accounts.widget)
        self.venues = VenuesPage(self)
        self._add_page("catalog", self.venues.widget)
        self.templates = TemplatesPage(self)
        self._add_page("templates", self.templates.widget)
        self.panels = WorkspacePanels(self, self._add_page)
        for key in ("jobs", "calendar", "subscriptions"):
            self.stack.removeWidget(self.pages[key])
        self.campaigns.add_queue(self.pages["jobs"], self.pages["calendar"], self.pages["subscriptions"])
        self._create_settings()
        self.navigate("home")
        self.refresh_all()
        self.availability_timer = QTimer(self)
        self.availability_timer.timeout.connect(self.scheduler_tick)
        self.availability_timer.start(3000)

    @staticmethod
    def _button(layout, text, handler, *, variant=None, graphic=None):
        button = QPushButton(text)
        if variant:
            button.setProperty("variant", variant)
        if graphic:
            button.setIcon(icon(graphic))
        button.clicked.connect(handler)
        layout.addWidget(button)
        return button

    def _add_page(self, key, widget):
        self.pages[key] = widget
        self.stack.addWidget(widget)

    def navigate(self, key):
        sections = ("jobs", "calendar", "subscriptions")
        parent = "campaigns" if key in sections else key
        self.nav_buttons[parent].setChecked(True)
        self.stack.setCurrentWidget(self.pages[parent])
        if key == "subscriptions":
            self.campaigns.tabs.setCurrentIndex(3)
        elif key in sections:
            self.campaigns.tabs.setCurrentIndex(2)
            self.campaigns.queue_tabs.setCurrentIndex(sections.index(key))
        if self.worker is not None:
            return
        try:
            if key == "catalog":
                self.venues.refresh()
            elif key == "report":
                self.panels.show_report()
            elif key == "accounts":
                self.accounts.refresh()
            elif key == "templates":
                self.templates.refresh()
            elif key == "campaigns":
                self.campaigns.refresh()
        except EXPECTED_ERRORS:
            self.statusBar().showMessage("Не удалось обновить раздел. Проверьте рабочий профиль.")

    def _create_queue_page(self, key, title, subtitle, date_label, empty_title, empty_body):
        tools = QHBoxLayout()
        self._button(tools, "Обновить", self.refresh)
        if key == "jobs":
            self.new_job_button = self._button(tools, "Новая публикация", self.new_publication, variant="primary")
        widget, layout = page(title, subtitle, actions=tools)

        views = QStackedWidget()
        self.queue_views[key] = views
        layout.addWidget(views, 1)
        placeholder, empty_layout = empty_state(empty_title, empty_body)
        if key == "jobs":
            row = QHBoxLayout()
            row.addStretch(1)
            self.empty_ctas[key] = self._button(row, "Новая публикация", self.dashboard.next_step, variant="primary")
            row.addStretch(1)
            empty_layout.addLayout(row)
        empty_layout.addStretch(1)
        views.addWidget(placeholder)

        columns = ["Кампания", "Площадка", "Отправитель", date_label, "Состояние", "Результат"] if key == "jobs" else ["Запись", "Состояние", date_label]
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        polish_table(table)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(columns)):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tables[key] = table
        views.addWidget(table)

        bar, bar_layout = card(margin=9)
        controls = QHBoxLayout()
        bar_layout.addLayout(controls)
        controls.addWidget(label("Выбранная запись", "muted"))
        controls.addStretch(1)
        if key == "jobs":
            for action, text in (("pause", "Пауза"), ("resume", "Возобновить"), ("cancel", "Отменить"),
                                 ("dispatch", "Отправить"), ("receipt", "Указать сообщение"),
                                 ("close_review", "Закрыть после сверки"), ("revoke", "Отозвать согласие")):
                self.buttons[action] = self._button(controls, text, lambda _, a=action: self.job_action(a),
                                                    variant="primary" if action == "dispatch" else None)
        else:
            self.buttons["close_subscription"] = self._button(controls, "Закрыть после сверки",
                                                               self.close_subscription)
        self.action_bars[key] = bar
        bar.hide()
        layout.addWidget(bar)
        self._add_page(key, widget)
        table.itemSelectionChanged.connect(self._update_actions)

    def _selected(self, key):
        selected = self.tables[key].selectionModel().selectedRows()
        row = selected[0].row() if selected else -1
        records = self.records[key]
        return records[row] if 0 <= row < len(records) else None

    def _update_actions(self):
        job = self._selected("jobs")
        available = JOB_ACTIONS.get(job["state"], ()) if job else ()
        for action in ("pause", "resume", "cancel", "dispatch", "receipt", "close_review", "revoke"):
            if action in self.buttons:
                visible = action in available or (action == "revoke" and bool(job and job.get("chat_id")))
                blocked = self._job_block(job, action) if visible else None
                self.buttons[action].setEnabled(visible and not blocked)
                self.buttons[action].setVisible(visible)
                self.buttons[action].setToolTip(blocked or "")
        if "jobs" in self.action_bars:
            self.action_bars["jobs"].setVisible(bool(available or (job and job.get("chat_id"))))
        subscription = self._selected("subscriptions")
        if "close_subscription" in self.buttons:
            can_review = bool(subscription and subscription["state"] == "needs_review")
            self.buttons["close_subscription"].setEnabled(can_review)
            self.action_bars["subscriptions"].setVisible(can_review)

    @staticmethod
    def _job_block(job, action):
        if action not in ("dispatch", "resume"):
            return None
        now = int(time())
        if job.get("bot_disabled"):
            return "Переподключите отправителя."
        if job.get("hold_at") is not None:
            until = job.get("hold_until")
            if until is None:
                return "Ограничение Telegram без срока. Нужна ручная проверка."
            if until > now:
                return f"Ограничение Telegram до {_local_time(until)}."
        when = job.get("scheduled_utc")
        if when is None or when < now - 3600:
            return "Время публикации истекло. Подготовьте новое задание."
        if action == "dispatch" and when > now:
            return f"Отправка доступна с {_local_time(when)}."
        if action == "dispatch" and (job.get("not_before") or 0) > now:
            return f"Согласованный интервал действует до {_local_time(job['not_before'])}."
        return None

    def refresh(self):
        try:
            fresh = self.service.status()
        except EXPECTED_ERRORS:
            self.statusBar().showMessage("Не удалось прочитать очередь. Проверьте доступ к профилю и обновите окно.")
            return
        for key, date_key in (("jobs", "scheduled_utc"), ("subscriptions", "started_at")):
            table = self.tables[key]
            selected = self._selected(key)
            table.clearSelection()
            self.records[key] = fresh[key]
            table.setRowCount(len(fresh[key]))
            for row, record in enumerate(fresh[key]):
                state = record["state"]
                identifier = record["id"]
                status = STATE_LABELS.get(state, state)
                if key == "jobs" and state == "queued":
                    status = ("Запланировано" if record[date_key] and record[date_key] > time() else
                              "Ожидает интервала" if (record.get("not_before") or 0) > time() else "Ожидает")
                reason = REASON_LABELS.get(record.get("reason"), "")
                values = ((record.get("campaign") or f"#{identifier[:10]}",
                           record.get("venue") or str(record.get("chat_id") or "—"), record.get("sender") or "—",
                           _local_time(record[date_key]), status,
                           f"Пост {record['message_id']}" if record.get("message_id") else reason or "—") if key == "jobs"
                          else (f"#{identifier[:12]}", status, _local_time(record[date_key])))
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setToolTip(identifier)
                    if column == (4 if key == "jobs" else 1):
                        item.setToolTip(reason)
                        color = {"sent": "#79ddb0", "created": "#79ddb0", "needs_review": "#ffbb82",
                                 "queued": "#bca5ff", "paused": "#b0aabc"}.get(state)
                        if color:
                            item.setForeground(QColor(color))
                    table.setItem(row, column, item)
                if selected and selected["id"] == identifier:
                    table.selectRow(row)
            self.queue_views[key].setCurrentIndex(int(bool(fresh[key])))
        self._update_actions()
        self.panels.show_date()
        self.statusBar().showMessage(
            f"Публикации: {len(fresh['jobs'])} · Платные ссылки: {len(fresh['subscriptions'])}")

    def refresh_all(self):
        if self.worker is not None:
            return
        self.refresh()
        try:
            self.dashboard.refresh(self.records, _local_time)
            self.accounts.refresh()
            self.venues.refresh()
            self.templates.refresh()
            self.campaigns.refresh()
            self.panels.show_report()
        except EXPECTED_ERRORS:
            self.statusBar().showMessage("Рабочий профиль временно недоступен.")

    def _apply(self, operation):
        try:
            result = operation()
        except (QueueConflict, DraftError) as error:
            QMessageBox.warning(self, "Изменение отклонено", str(error))
            self.refresh()
            return None
        except EXPECTED_ERRORS:
            QMessageBox.warning(self, "Операция не выполнена", "Проверьте доступ к профилю и параметры операции.")
            return None
        self.refresh()
        return result

    def job_action(self, action):
        record = self._selected("jobs")
        if action == "revoke":
            if record and record.get("chat_id") and confirm(
                    self, "Отозвать согласия площадки?",
                    f"Действующие согласия для чата {record['chat_id']} будут отозваны. "
                    "Задания в очереди и на паузе будут отменены."):
                self._apply(lambda: self.service.revoke_consent(record["id"]))
            return
        if not record or action not in JOB_ACTIONS.get(record["state"], ()):
            return
        if self._job_block(record, action):
            return
        job_id = record["id"]
        if action == "dispatch":
            try:
                chat, snapshot = self.service.preview_job(job_id)
                detail = f"Площадка {chat}\n\n{snapshot['text']}"
                if snapshot.get("button_url"):
                    detail += f"\n\nСсылка кнопки: {snapshot['button_url']}"
                if snapshot.get("photo_digest"):
                    detail += "\nВложение: фото"
            except (*EXPECTED_ERRORS, KeyError, TypeError):
                QMessageBox.warning(self, "Предпросмотр", "Не удалось прочитать утверждённый снимок.")
                return
            if confirm(self, "Отправить одну публикацию в Telegram?", detail):
                self.stop_event.clear()
                self._run_network(lambda: self.service.dispatch(job_id, stop=self.stop_event), self._dispatch_result)
        elif action == "receipt":
            try:
                receipt = _get_receipt(self)
            except ValueError:
                QMessageBox.warning(self, "Сверка публикации", "ID чата и сообщения должны быть целыми числами.")
                return
            if receipt is not None:
                self._apply(lambda: self.service.review_job(job_id, receipt))
        elif action == "close_review":
            if QMessageBox.question(self, "Закрыть задание", "Закрыть после ручной проверки Telegram?") == QMessageBox.StandardButton.Yes:
                self._apply(lambda: self.service.review_job(job_id))
        else:
            if action == "cancel" and QMessageBox.question(
                    self, "Отмена задания", "Отменить выбранное задание?") != QMessageBox.StandardButton.Yes:
                return
            self._apply(lambda: self.service.change_job(job_id, action))

    def _run_network(self, action, on_result):
        if self.worker is not None:
            return
        self.stack.setEnabled(False)
        self.statusBar().showMessage("Запрос к Telegram…")
        worker = NetworkAction(self, action)
        self.worker = worker
        self.result_handler = on_result
        self.network_result, self.network_error = None, None
        worker.completed.connect(self._receive_result)
        worker.failed.connect(self._network_failed)
        worker.finished.connect(self._network_finished)
        worker.start()

    def _receive_result(self, result):
        self.network_result = result

    def _network_failed(self, reason):
        self.network_error = reason

    def _network_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.stack.setEnabled(True)
        handler, result, error = self.result_handler, self.network_result, self.network_error
        self.network_result, self.network_error = None, None
        if self.pause_pending:
            self.pause_pending = False
            self._apply(self.service.pause_automatic)
        self.refresh_all()
        if error:
            QMessageBox.warning(self, "Операция не завершена", error)
        else:
            handler(result)

    def _dispatch_result(self, result):
        if result is None:
            QMessageBox.information(self, "Публикация", "Задание пока недоступно для отправки.")
        else:
            job_id, state = result
            message = STATE_LABELS.get(state, state)
            try:
                record = next((job for job in self.service.status()["jobs"] if job["id"] == job_id), {})
            except EXPECTED_ERRORS:
                record = {}
            reason = REASON_LABELS.get(record.get("reason"))
            if reason:
                message += "\n\n" + reason
            QMessageBox.information(self, "Публикация", message)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            if self.campaigns.save_timer.isActive():
                self.campaigns.save_timer.stop()
                self.campaigns.save()
            super().closeEvent(event)

    def connect_bot(self):
        token = get_bot_token(self)
        if token:
            self._run_network(lambda: self.service.connect_bot(token),
                              lambda _: QMessageBox.information(self, "Бот", "Подключение проверено."))

    def new_publication(self):
        self.dashboard.next_step()

    def arm_scheduler(self):
        if self.auto_active or self.worker is not None:
            return
        if confirm(self, "Включить отправку по расписанию?",
                   "В этом сеансе будут отправляться только задания, для которых вы разрешили автоматическое выполнение."):
            self.stop_event.clear()
            self.auto_active = True
            self.auto_button.setEnabled(False)
            self.auto_status.setText("Расписание включено")

    def scheduler_tick(self):
        self._update_actions()
        if not self.auto_active or self.worker is not None:
            return
        due = any(j["state"] == "queued" and j.get("auto") and not self._job_block(j, "dispatch")
                  for j in self.records["jobs"])
        if due:
            self._run_network(lambda: self.service.dispatch(auto_only=True, stop=self.stop_event),
                              lambda _: None)

    def emergency_stop(self):
        self.auto_active = False
        self.stop_event.set()
        self.auto_button.setEnabled(True)
        self.auto_status.setText("Отправка остановлена")
        if self.worker is not None:
            self.pause_pending = True
        else:
            self._apply(self.service.pause_automatic)

    def _create_settings(self):
        widget, layout = page("Настройки", "Данные хранятся в локальном профиле Windows.")
        frame, contents = card(margin=14)
        contents.addWidget(label("Рабочий профиль", "sectionTitle"))
        contents.addWidget(label(str(self.service.profile), "muted", wrap=True))
        controls = QHBoxLayout()
        contents.addLayout(controls)
        self._button(controls, "Открыть папку", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.service.profile))))
        self._button(controls, "Резервная копия", self.backup)
        self._button(controls, "Восстановить в новый профиль", self.restore_backup)
        controls.addStretch(1)
        layout.addWidget(frame)
        layout.addWidget(label("Копия для переноса не содержит токенов и сессий. Восстановление создаёт отдельный профиль.", "muted", wrap=True))
        layout.addStretch(1)
        self._add_page("settings", widget)

    def backup(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Резервная копия", "studio.sqlite3", "SQLite (*.sqlite3)")
        if filename:
            self._apply(lambda: create_backup(self.service.profile, Path(filename)))

    def restore_backup(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Открыть копию", "", "SQLite (*.sqlite3)")
        if filename:
            folder = QFileDialog.getExistingDirectory(self, "Выберите пустую папку для нового профиля")
            if folder:
                self._apply(lambda: restore_backup(Path(filename), Path(folder)))

    def close_subscription(self):
        record = self._selected("subscriptions")
        if not record or record["state"] != "needs_review":
            return
        answer = QMessageBox.question(self, "Сверка ссылки",
                                      "Закрыть запрос после проверки, что активной ссылки в Telegram нет?")
        if answer == QMessageBox.StandardButton.Yes:
            self._apply(lambda: self.service.close_subscription_review(record["id"]))

    def import_photo(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Импорт фото", "", "Фото (*.png *.jpg *.jpeg)")
        if filename:
            digest = self._apply(lambda: self.service.import_photo(Path(filename)))
            if digest:
                QMessageBox.information(self, "Фото импортировано", f"SHA-256\n{digest}")


def run_desktop(profile):
    app = QApplication.instance() or QApplication([])
    service = DesktopService(profile)
    try:
        service.initialize()
    except EXPECTED_ERRORS:
        QMessageBox.critical(None, "Telegram Promotion Studio", "Не удалось открыть рабочий профиль.")
        return 2
    window = StudioWindow(service)
    window.show()
    return app.exec()

