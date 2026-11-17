"""Compact operational overview."""
from datetime import datetime

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from .desktop_directory import STATES, fill, table
from .desktop_style import card, label, page


class Dashboard:
    def __init__(self, window):
        self.window = window
        actions = QHBoxLayout()
        window._button(actions, "Добавить аккаунт", lambda: window.navigate("accounts"))
        window._button(actions, "Синхронизировать", lambda: window.venues.sync())
        self.cta = window._button(actions, "Создать кампанию", self.next_step, variant="primary")
        self.widget, layout = page("Главная", "Состояние публикаций и аккаунтов.", actions=actions)
        strip = QHBoxLayout()
        layout.addLayout(strip)
        self.values = {}
        for key, title in (("accounts", "Аккаунты"), ("venues", "Площадки"), ("active", "Кампании"),
                           ("queued", "В очереди"), ("review", "Требуют внимания")):
            frame, content = card(margin=11)
            content.addWidget(label(title, "muted"))
            value = label("0", "metricValue")
            self.values[key] = value
            content.addWidget(value)
            strip.addWidget(frame, 1)
        grid = QGridLayout()
        layout.addLayout(grid)
        cells = (("upcoming", "Ближайшие публикации", ["Кампания", "Площадка", "Время"]),
                 ("campaigns", "Активные кампании", ["Кампания", "Ожидают"]),
                 ("accounts", "Аккаунты", ["Имя", "Состояние"]),
                 ("recent", "Последние результаты", ["Площадка", "Результат"]))
        self.summary = {}
        for i, (key, title, columns) in enumerate(cells):
            widget = QWidget()
            column = QVBoxLayout(widget)
            column.setContentsMargins(0, 0, 0, 0)
            column.addWidget(label(title, "sectionTitle"))
            listing = table(columns)
            listing.setMinimumHeight(125)
            column.addWidget(listing, 1)
            empty = label({"upcoming": "Утвердите кампанию, чтобы добавить задания.",
                           "campaigns": "Нет кампаний с ожидающими заданиями.",
                           "accounts": "Добавьте личный аккаунт или бота.",
                           "recent": "Результаты появятся после отправки."}[key], "muted", wrap=True)
            column.addWidget(empty)
            self.summary[key] = (listing, empty)
            grid.addWidget(widget, i // 2, i % 2)
        layout.addStretch(1)

    def next_step(self):
        self.window.campaigns.new()
        self.window.navigate("campaigns")

    def refresh(self, records, local_time):
        service = self.window.service
        accounts, venues, campaigns = service.account_rows(), service.venue_rows(), service.campaign_list()
        jobs = records["jobs"]
        queued = [r for r in jobs if r["state"] == "queued"]
        review = [r for r in jobs if r["state"] in ("needs_review", "paused")]
        active = [(name, count) for _, name, _, _, count in campaigns if count]
        metrics = dict(accounts=sum(a["status"] == "ready" for a in accounts), venues=len(venues),
                       active=len(active), queued=len(queued), review=len(review))
        for key, value in metrics.items():
            self.values[key].setText(str(value))
        recent = sorted((r for r in jobs if r["state"] in ("sent", "needs_review", "cancelled")),
                        key=lambda r: r.get("sent_at") or 0, reverse=True)
        for key, rows in (
            ("upcoming", [(r.get("campaign"), r.get("venue"), local_time(r["scheduled_utc"])) for r in queued[:6]]),
            ("campaigns", active[:6]),
            ("accounts", [(a["name"], STATES.get(a["status"], a["status"])) for a in accounts[:6]]),
            ("recent", [(r.get("venue"), {"sent": "Отправлено", "needs_review": "Нужна сверка",
                                         "cancelled": "Отменено"}[r["state"]]) for r in recent[:6]]),
        ):
            listing, empty = self.summary[key]
            fill(listing, rows)
            listing.setVisible(bool(rows))
            empty.setVisible(not rows)
