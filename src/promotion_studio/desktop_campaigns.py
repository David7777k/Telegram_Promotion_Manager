"""Single workspace campaign editor and revisioned template workbench."""
import sqlite3
import re
from html import escape
from pathlib import Path
from time import time
from uuid import uuid4

from PySide6.QtCore import QDateTime, QRegularExpression, Qt, QTimer
from PySide6.QtGui import QColor, QPixmap, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QSpinBox,
                               QSplitter, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from .campaigns import VARIABLES, render
from .desktop_directory import DECISIONS, fill, local_time, rows_selected, table
from .desktop_forms import confirm
from .desktop_style import label, page
from .spintax import generate


def box(value, low, high):
    field = QSpinBox()
    field.setRange(low, high)
    field.setValue(value)
    return field


def clock(epoch):
    field = QDateTimeEdit(QDateTime.fromSecsSinceEpoch(epoch))
    field.setCalendarPopup(True)
    field.setDisplayFormat("dd.MM.yyyy HH:mm:ss")
    return field


def plain(read_only=False):
    edit = QPlainTextEdit()
    edit.setReadOnly(read_only)
    return edit


def preview_message(widget, text):
    parts = re.split(r"(https://[^\s<>]+)", text)
    html = "".join(f'<a style="color:#b79dff" href="{escape(part)}">{escape(part)}</a>'
                   if index % 2 else escape(part).replace("\n", "<br>") for index, part in enumerate(parts))
    widget.setHtml(html)


class TargetDialog(QDialog):
    def __init__(self, parent, accounts, venue, existing):
        super().__init__(parent)
        self.setWindowTitle(venue["title"])
        self.resize(480, 310)
        form = QFormLayout(self)
        self.account = QComboBox()
        self.account.addItem("Выберите отправителя", None)
        permissions = {a[0]: a[2] for a in venue["access"]}
        for a in accounts:
            if a["status"] == "ready":
                rights = {1: "права известны", 0: "нет права", -1: "права не проверены"}.get(
                    permissions.get(a["id"]), "нет проверки")
                self.account.addItem(f"{a['name']} · {'бот' if a['kind']=='bot' else 'личный'} · {rights}", a["id"])
        self.account.setCurrentIndex(max(0, self.account.findData(existing.get("account"))))
        self.note = QLineEdit(existing.get("note", ""))
        self.note.setPlaceholderText("Кто и на каких условиях согласовал")
        now = int(time())
        self.when = clock(existing.get("when", now + 300))
        self.until = clock(existing.get("until", now + 86400))
        self.interval = box(existing.get("interval", 60), 1, 10080)
        self.repeats = box(existing.get("repeats", 1), 1, 20)
        for caption, widget in (("Отправитель", self.account), ("Согласование", self.note),
                                ("Первый пост", self.when), ("Согласие до", self.until),
                                ("Пауза / шаг повтора, мин", self.interval), ("Всего публикаций", self.repeats)):
            form.addRow(caption, widget)
        self.error = label("", "muted", wrap=True)
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def validate(self):
        if not self.account.currentData() or not self.note.text().strip():
            self.error.setText("Выберите отправителя и укажите согласование.")
            return
        self.accept()

    def values(self):
        return dict(account=self.account.currentData(), note=self.note.text().strip(),
                    when=self.when.dateTime().toSecsSinceEpoch(), until=self.until.dateTime().toSecsSinceEpoch(),
                    interval=self.interval.value(), repeats=self.repeats.value())


class CampaignPage:
    def __init__(self, window):
        self.window, self.service = window, window.service
        self.draft = uuid4().hex
        self.targets, self.current_rows = {}, []
        self.queued, self.loading = False, False
        self.photo = None
        actions = QHBoxLayout()
        window._button(actions, "Открыть черновик", self.open)
        window._button(actions, "Новый черновик", self.new, variant="primary")
        self.widget, layout = page("Кампании", "Контент, площадки и график в одном редакторе.", actions=actions)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        editor = QWidget()
        self.tabs.addTab(editor, "Редактор")
        editor_layout = QVBoxLayout(editor)
        main = QSplitter()
        editor_layout.addWidget(main, 1)
        fields = QWidget()
        form = QFormLayout(fields)
        main.addWidget(fields)
        self.title, self.link = QLineEdit(), QLineEdit()
        self.link.setPlaceholderText("https://…")
        self.text = plain()
        self.text.setPlaceholderText("Текст разрешённого рекламного объявления")
        Highlighter(self.text.document())
        self.spintax, self.button, self.paid = QCheckBox("Spintax"), QCheckBox("Кнопка для бота"), QCheckBox("Подписка Stars")
        switches = QHBoxLayout()
        for field in (self.spintax, self.button, self.paid):
            switches.addWidget(field)
        self.description, self.channel = QLineEdit(), QLineEdit()
        self.description.setPlaceholderText("Что получит подписчик")
        self.channel.setPlaceholderText("Название рекламируемого канала · необязательно")
        self.price = box(250, 1, 10000)
        form.addRow("Кампания", self.title)
        form.addRow("Ссылка", self.link)
        form.addRow("Сообщение", self.text)
        form.addRow(switches)
        self.photo_label = label("Без фото", "muted")
        photos = QHBoxLayout()
        window._button(photos, "Фото", self.choose_photo)
        window._button(photos, "Убрать", self.remove_photo)
        photos.addWidget(self.photo_label, 1)
        window._button(photos, "Из шаблона", self.use_template)
        form.addRow(photos)
        self.offer = QWidget()
        offer = QFormLayout(self.offer)
        offer.addRow("Канал", self.channel)
        offer.addRow("Условия", self.description)
        offer.addRow("Stars за 30 дней", self.price)
        self.offer.hide()
        form.addRow(self.offer)
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(8, 0, 0, 0)
        preview_layout.addWidget(label("Предпросмотр", "sectionTitle"))
        self.preview_kind = QComboBox()
        self.preview_kind.addItem("Личный аккаунт", "user")
        self.preview_kind.addItem("Бот", "bot")
        preview_layout.addWidget(self.preview_kind)
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview_photo = label("")
        self.preview_photo.hide()
        preview_layout.addWidget(self.preview_photo)
        preview_layout.addWidget(self.preview, 1)
        self.preview_hint = label("", "muted", wrap=True)
        preview_layout.addWidget(self.preview_hint)
        main.addWidget(preview)
        main.setSizes([560, 360])

        placements = QWidget()
        placements_layout = QVBoxLayout(placements)
        self.tabs.addTab(placements, "Площадки и расписание")
        target_actions = QHBoxLayout()
        placements_layout.addLayout(target_actions)
        window._button(target_actions, "Выбрать площадки", self.choose_venues, variant="primary")
        window._button(target_actions, "Отправитель и время", self.configure)
        window._button(target_actions, "Убрать из кампании", self.remove)
        target_actions.addStretch(1)
        self.target_table = table(["Площадка", "Разрешение", "Отправитель", "Время", "Повторы", "Проверка"], multi=True)
        self.target_table.doubleClicked.connect(self.configure)
        placements_layout.addWidget(self.target_table, 1)
        self.queue_tabs = QTabWidget()
        self.tabs.addTab(self.queue_tabs, "Очередь и календарь")

        launch = QHBoxLayout()
        layout.addLayout(launch)
        self.confirmed = QCheckBox("Содержимое и условия проверены")
        self.auto = QCheckBox("Разрешить отправку по расписанию")
        launch.addWidget(self.confirmed)
        launch.addWidget(self.auto)
        launch.addStretch(1)
        window._button(launch, "Проверить", lambda: self.approve(preflight=True))
        window._button(launch, "Утвердить кампанию", self.approve, variant="primary")
        self.status = label("", "muted", wrap=True)
        layout.addWidget(self.status)
        self.campaign_actions = QWidget()
        controls = QHBoxLayout(self.campaign_actions)
        controls.setContentsMargins(0, 0, 0, 0)
        for action, caption in (("pause", "Пауза кампании"), ("resume", "Возобновить кампанию"),
                                ("cancel", "Отменить ожидающие задания")):
            window._button(controls, caption, lambda _, a=action: self.control(a))
        controls.addStretch(1)
        layout.addWidget(self.campaign_actions)
        self.campaign_actions.hide()
        self.save_timer = QTimer(window)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save)
        for line in (self.title, self.link, self.description, self.channel):
            line.textChanged.connect(self.changed)
        self.text.textChanged.connect(self.changed)
        for checkbox in (self.spintax, self.button, self.paid, self.confirmed, self.auto):
            checkbox.toggled.connect(self.changed)
        self.paid.toggled.connect(self.offer.setVisible)
        self.price.valueChanged.connect(self.changed)
        self.preview_kind.currentIndexChanged.connect(self.render_preview)
        self.render_preview()

    def add_queue(self, queue, calendar, subscriptions):
        self.queue_tabs.addTab(queue, "Очередь")
        self.queue_tabs.addTab(calendar, "Календарь")
        self.tabs.addTab(subscriptions, "Предложения")

    def data(self):
        return dict(title=self.title.text(), link=self.link.text(), text=self.text.toPlainText(),
                    description=self.description.text(), channel=self.channel.text(), price=self.price.value(),
                    paid=self.paid.isChecked(), button=self.button.isChecked(), spintax=self.spintax.isChecked(),
                    confirmed=self.confirmed.isChecked(), automatic=self.auto.isChecked(), photo=self.photo,
                    targets=self.targets)

    def changed(self):
        if not self.loading:
            self.render_preview()
            if not self.queued:
                self.save_timer.start(500)

    def render_preview(self):
        try:
            text, button, _ = render(self.data(), self.preview_kind.currentData(), "Площадка")
        except ValueError as error:
            self.preview.setPlainText("")
            self.preview_hint.setText(str(error))
        else:
            preview_message(self.preview, text)
            self.preview_hint.setText("Кнопка · " + button if button else "Фото приложено" if self.photo else "")

    def save(self):
        if self.window.worker is not None:
            self.save_timer.start(700)
            return
        if not self.queued:
            try:
                self.service.save_campaign(self.draft, self.data())
            except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
                self.status.setText(f"Не удалось сохранить черновик: {error}")

    def restore(self, draft, data, approved):
        self.save_timer.stop()
        self.loading = True
        self.draft, self.queued = draft, approved
        self.campaign_actions.setVisible(approved)
        self.tabs.widget(0).setEnabled(not approved)
        for key, item in (("title", self.title), ("link", self.link), ("description", self.description),
                          ("channel", self.channel)):
            item.setText(data.get(key, ""))
        self.text.setPlainText(data.get("text", ""))
        for key, item in (("paid", self.paid), ("button", self.button), ("spintax", self.spintax),
                          ("confirmed", self.confirmed), ("automatic", self.auto)):
            item.setChecked(bool(data.get(key)))
        self.price.setValue(data.get("price", 250))
        self.photo = data.get("photo")
        self.photo_label.setText("Фото приложено" if self.photo else "Без фото")
        self.show_photo()
        self.targets = data.get("targets", {})
        self.status.setText("Кампания утверждена. Для изменений создайте отдельный черновик." if approved else "")
        self.loading = False
        self.render_preview()
        self.refresh()

    def new(self):
        if self.window.worker is not None:
            return
        if self.save_timer.isActive():
            self.save()
        self.restore(uuid4().hex, {}, False)
        self.tabs.setCurrentIndex(0)
        self.title.setFocus()

    def open(self):
        if self.window.worker is not None:
            return
        if self.save_timer.isActive():
            self.save()
        rows = self.service.campaign_list()
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Кампании и черновики")
        dialog.resize(580, 420)
        layout = QVBoxLayout(dialog)
        grid = table(["Кампания", "Изменено", "Заданий", "Активно"])
        fill(grid, [(name, local_time(at), count, active) for _, name, at, count, active in rows])
        layout.addWidget(grid)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        grid.doubleClicked.connect(dialog.accept)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and (selected := rows_selected(grid, rows)):
            draft = selected[0][0]
            data, approved = self.service.load_campaign(draft)
            self.restore(draft, data, bool(approved))

    def choose_photo(self):
        filename, _ = QFileDialog.getOpenFileName(self.window, "Фото", "", "Изображения (*.jpg *.jpeg *.png)")
        if filename:
            if digest := self.window._apply(lambda: self.service.import_photo(Path(filename))):
                self.photo = digest
                self.photo_label.setText(Path(filename).name)
                self.show_photo()
                self.changed()

    def remove_photo(self):
        self.photo = None
        self.photo_label.setText("Без фото")
        self.show_photo()
        self.changed()

    def show_photo(self):
        self.preview_photo.clear()
        self.preview_photo.hide()
        if not self.photo:
            return
        try:
            data = self.service.preview_photo(self.photo)
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            self.photo_label.setText("Предпросмотр фото недоступен")
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.preview_photo.setPixmap(pixmap.scaled(300, 126, Qt.AspectRatioMode.KeepAspectRatio,
                                                       Qt.TransformationMode.SmoothTransformation))
            self.preview_photo.show()

    def use_template(self):
        rows = self.service.template_list()
        if not rows:
            self.status.setText("Сохраните шаблон в разделе «Шаблоны».")
            return
        from PySide6.QtWidgets import QInputDialog
        options = [f"{name} · {identifier[:6]}" for identifier, name, _ in rows]
        chosen, ok = QInputDialog.getItem(self.window, "Шаблон", "Использовать версию", options, 0, False)
        if ok:
            self.text.setPlainText(self.service.template_versions(rows[options.index(chosen)][0])[0][2])
            self.spintax.setChecked(True)

    def choose_venues(self):
        if self.window.worker is not None:
            return
        rows = [r for r in self.service.venue_rows() if not r["excluded"]]
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Выбрать площадки")
        dialog.resize(640, 440)
        layout = QVBoxLayout(dialog)
        grid = table(["Площадка", "Категория", "Реклама"], multi=True)
        fill(grid, [(r["title"], r["category"], DECISIONS[r["decision"]]) for r in rows])
        layout.addWidget(grid)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_venues(rows_selected(grid, rows))

    def add_venues(self, rows):
        if self.queued:
            self.new()
        for row in rows:
            self.targets.setdefault(row["id"], {"venue": row["id"]})
        self.tabs.setCurrentIndex(1)
        self.refresh()
        self.changed()

    def configure(self):
        selected = rows_selected(self.target_table, self.current_rows)
        if not selected or self.queued or self.window.worker is not None:
            return
        venue = selected[0]
        dialog = TargetDialog(self.window, self.service.account_rows(), venue, self.targets[venue["id"]])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.targets[venue["id"]].update(dialog.values())
            self.refresh()
            self.changed()

    def remove(self):
        if not self.queued:
            for row in rows_selected(self.target_table, self.current_rows):
                self.targets.pop(row["id"], None)
            self.refresh()
            self.changed()

    def refresh(self):
        if self.window.worker is not None:
            return
        self.current_rows = [r for r in self.service.venue_rows() if r["id"] in self.targets]
        accounts = {r["id"]: r for r in self.service.account_rows()}
        rows = []
        for r in self.current_rows:
            target = self.targets[r["id"]]
            account = accounts.get(target.get("account"))
            rights = next((a for a in r["access"] if account and a[0] == account["id"]), None)
            reason = ("Площадка исключена" if r["excluded"] else "Нужно согласие" if r["decision"] != "allowed"
                      else "Нет отправителя" if not account else "Аккаунт отключён" if account["status"] != "ready"
                      else "Нет права" if rights and (rights[2] == 0 or (self.photo and rights[3] == 0))
                      else "Права подтверждались" if rights and rights[2] == 1 else "Нужна проверка прав")
            rows.append((r["title"], DECISIONS[r["decision"]], account["name"] if account else None,
                         local_time(target.get("when")), target.get("repeats"), reason))
        fill(self.target_table, rows)

    def approve(self, _=False, *, preflight=False):
        if self.window.worker is not None or self.queued:
            return
        data = self.data()
        targets = tuple(dict(t) for t in self.targets.values())
        if not targets or not data["title"].strip() or not data["confirmed"] or any(not t.get("account") for t in targets):
            self.status.setText("Заполните название, подтвердите содержимое и настройте отправителя для каждой площадки.")
            return
        try:
            accounts = {a["id"]: a for a in self.service.account_rows()}
            venues = {r["id"]: r["title"] for r in self.current_rows}
            previews = []
            for target in targets:
                title = venues.get(target["venue"], "Площадка")
                text, button, _ = render(data, accounts[target["account"]]["kind"], title)
                previews.append(f"{title} · {local_time(target['when'])} · {target['repeats']} шт.\n{text}"
                                + (f"\nКнопка: {button}" if button else ""))
        except (ValueError, KeyError) as error:
            self.status.setText(str(error))
            return
        automatic = self.auto.isChecked()
        if not preflight and not confirm(self.window, "Утвердить публикации?",
                                          ("Автоматическое выполнение нужно включить отдельно.\n\n"
                                           if automatic else "Публикации останутся для ручного подтверждения.\n\n")
                                          + "\n\n".join(previews)):
            return
        self.save_timer.stop()
        draft = self.draft
        self.window._run_network(
            lambda: self.service.approve_campaign(draft, data, targets, automatic, preflight_only=preflight),
            lambda result: self.done(result, preflight))

    def done(self, result, preflight):
        if preflight:
            self.status.setText(f"Проверены права для площадок: {result}.")
        else:
            self.queued = True
            self.campaign_actions.show()
            self.tabs.widget(0).setEnabled(False)
            self.status.setText(f"Создано заданий: {len(result)}.")
            self.tabs.setCurrentIndex(2)
            self.queue_tabs.setCurrentIndex(0)

    def control(self, action):
        if not self.queued or self.window.worker is not None:
            return
        if action == "cancel" and not confirm(self.window, "Отменить задания кампании?",
                                               "Отправленные публикации и записи на сверке сохранятся."):
            return
        result = self.window._apply(lambda: self.service.control_campaign(self.draft, action))
        if result is not None:
            changed, blocked = result
            self.status.setText(f"Заданий изменено: {changed}. Требуют отдельной проверки: {blocked}.")
            self.window.refresh_all()


class Highlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        for pattern, color in ((r"\{\{[a-z_]+\}\}", "#b79dff"), (r"[{}|]", "#7cbfff"),
                               (r"https://[^\s{}|]+", "#7ae1c6")):
            style = QTextCharFormat()
            style.setForeground(QColor(color))
            matches = QRegularExpression(pattern).globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), style)


class TemplatesPage:
    def __init__(self, window):
        self.window, self.service = window, window.service
        self.template = None
        self.rows = []
        actions = QHBoxLayout()
        window._button(actions, "Новый шаблон", self.new)
        window._button(actions, "Дублировать", self.duplicate)
        window._button(actions, "Сохранить версию", self.save, variant="primary")
        self.widget, layout = page("Шаблоны", "Редактирование и сравнение формулировок.", actions=actions)
        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        left, center, right = QWidget(), QWidget(), QWidget()
        for column in (left, center, right):
            splitter.addWidget(column)
        listing, editor, preview = QVBoxLayout(left), QVBoxLayout(center), QVBoxLayout(right)
        for l in (listing, editor, preview):
            l.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.itemClicked.connect(self.open)
        listing.addWidget(self.list, 1)
        window._button(listing, "Удалить из списка", self.delete)
        self.name, self.source, self.sample = QLineEdit(), plain(), QTextBrowser()
        self.sample.setOpenLinks(False)
        self.name.setPlaceholderText("Название шаблона")
        self.source.setPlaceholderText("{Вариант 1|Вариант 2} {{campaign_name}}")
        Highlighter(self.source.document())
        self.version = QComboBox()
        self.version.activated.connect(self.use_version)
        editor.addWidget(self.name)
        editor.addWidget(self.version)
        editor.addWidget(self.source, 1)
        window._button(editor, "Переменные", self.variable)
        self.error = label("", "muted", wrap=True)
        preview.addWidget(self.error)
        preview.addWidget(self.sample, 1)
        window._button(preview, "Показать до 10 вариантов", lambda: self.preview(10))
        self.source.textChanged.connect(self.preview)
        splitter.setSizes([165, 360, 360])

    def refresh(self):
        if self.window.worker is not None:
            return
        self.rows = self.service.template_list()
        self.list.clear()
        for identifier, name, version in self.rows:
            item = QListWidgetItem(f"{name} · v{version}")
            item.setData(Qt.ItemDataRole.UserRole, (identifier, name))
            self.list.addItem(item)

    def new(self):
        self.template = None
        self.name.clear()
        self.source.clear()
        self.version.clear()

    def duplicate(self):
        self.template = None
        self.version.clear()
        self.name.setText(self.name.text() + " (копия)")

    def preview(self, limit=1):
        if not self.source.toPlainText().strip():
            self.sample.clear()
            self.error.setText("Предпросмотр")
            return
        try:
            variants = generate(self.source.toPlainText(), VARIABLES, limit=limit, seed=0)
        except ValueError as error:
            self.error.setText(str(error))
            self.sample.clear()
        else:
            self.error.setText(f"Вариантов: {len(variants)} · подстановки демонстрационные")
            preview_message(self.sample, "\n\n──────────\n\n".join(variants))

    def variable(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getItem(self.window, "Переменная", "Подставляется при подготовке кампании",
                                        list(VARIABLES), 0, False)
        if ok:
            self.source.insertPlainText("{{" + name + "}}")

    def save(self):
        result = self.window._apply(lambda: self.service.save_template(self.template, self.name.text(),
                                                                        self.source.toPlainText()))
        if result:
            self.template = result
            self.load_versions()
            self.refresh()

    def open(self, item):
        self.template, name = item.data(Qt.ItemDataRole.UserRole)
        self.name.setText(name)
        self.load_versions()
        self.use_version(0)

    def load_versions(self):
        self.version.clear()
        for version, at, source in self.service.template_versions(self.template):
            self.version.addItem(f"v{version} · {local_time(at)}", source)

    def use_version(self, index):
        if index >= 0:
            self.source.setPlainText(self.version.itemData(index))

    def delete(self):
        if self.template and confirm(self.window, "Удалить шаблон из списка?", "Утверждённые публикации сохранят текст."):
            self.window._apply(lambda: self.service.archive_template(self.template))
            self.new()
            self.refresh()
