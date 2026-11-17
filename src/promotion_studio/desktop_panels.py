"""Qt panels for local venue discovery, calendar and reports."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import QCalendarWidget, QFileDialog, QHBoxLayout, QListWidget

from .desktop_directory import fill, local_time, table
from .desktop_style import card, label, page


class WorkspacePanels:
    def __init__(self, window, add_page):
        self.window = window
        self.service = window.service
        self.calendar = QCalendarWidget()
        self.calendar.setLocale(QLocale(QLocale.Language.Russian))
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.selectionChanged.connect(self.show_date)
        self.slots = QListWidget()
        calendar_page, calendar_layout = page("Календарь", "Выберите дату, чтобы увидеть задания на этот день.")
        schedule = QHBoxLayout()
        schedule.setSpacing(14)
        calendar_layout.addLayout(schedule, 1)
        calendar_card, calendar_card_layout = card(margin=12)
        calendar_card_layout.addWidget(self.calendar)
        schedule.addWidget(calendar_card, 1)
        day_card, day_layout = card(margin=14)
        self.day_title = label("Задания на день", "sectionTitle")
        day_layout.addWidget(self.day_title)
        day_layout.addWidget(self.slots, 1)
        schedule.addWidget(day_card, 1)
        add_page("calendar", calendar_page)

        report_page, report_layout = page("Аналитика", "Состояния публикаций, подтверждённые расходы и журнал попыток.")
        controls = QHBoxLayout()
        controls.addStretch(1)
        window._button(controls, "Обновить", self.show_report)
        window._button(controls, "Экспорт CSV", self.export, variant="primary")
        report_layout.addLayout(controls)
        metrics = QHBoxLayout()
        self.report_values = {}
        for key, title in (("total", "Всего публикаций"), ("sent", "Отправлено"), ("cost", "Известные расходы")):
            metric, metric_layout = card(margin=15)
            metric_layout.addWidget(label(title, "muted"))
            self.report_values[key] = label("—", "metricValue", wrap=True)
            metric_layout.addWidget(self.report_values[key])
            metrics.addWidget(metric, 1)
        report_layout.addLayout(metrics)
        details, details_layout = card()
        details_layout.addWidget(label("По состояниям", "sectionTitle"))
        self.totals = label("", "muted", wrap=True)
        details_layout.addWidget(self.totals)
        report_layout.addWidget(details)
        report_layout.addWidget(label("Последние попытки", "sectionTitle"))
        self.journal = table(["Время", "Задание", "Исход", "Причина"])
        report_layout.addWidget(self.journal, 1)
        add_page("report", report_page)

    def show_date(self):
        from .desktop import STATE_LABELS
        selected = self.calendar.selectedDate()
        date = selected.toPython()
        self.day_title.setText(f"Задания · {selected.toString('dd.MM.yyyy')}")
        self.slots.clear()
        for record in self.window.records["jobs"]:
            epoch = record["scheduled_utc"]
            if epoch is None:
                continue
            try:
                scheduled = datetime.fromtimestamp(epoch).astimezone()
            except (ValueError, OverflowError, OSError):
                continue
            if scheduled.date() == date:
                state = STATE_LABELS.get(record["state"], record["state"])
                campaign = record.get("campaign") or record['id'][:12]
                venue = record.get("venue") or record.get("chat_id") or "Площадка неизвестна"
                self.slots.addItem(f"{scheduled:%H:%M} · {campaign} · {venue} · {state}")
        if not self.slots.count():
            self.slots.addItem("На эту дату заданий нет")

    def show_report(self):
        from .desktop import REASON_LABELS, STATE_LABELS
        report = self.service.report()
        if report is None:
            return
        lines = [f"{STATE_LABELS.get(state, state)}: {count}" for state, count in report["states"].items()]
        minor = report["known_cost_uah_minor"]
        self.report_values["total"].setText(str(sum(report["states"].values())))
        self.report_values["sent"].setText(str(report["states"].get("sent", 0)))
        self.report_values["cost"].setText(f"{minor // 100}.{minor % 100:02} UAH" if minor is not None else "—")
        self.totals.setText("   ·   ".join(lines) if lines else "Публикаций пока нет.")
        fill(self.journal, [(local_time(at), job[:12], STATE_LABELS.get(state, state or "Выполняется"),
                             REASON_LABELS.get(reason, reason)) for at, state, reason, job in self.service.journal()])

    def export(self):
        filename, _ = QFileDialog.getSaveFileName(self.window, "Экспорт CSV", "queue-report.csv", "CSV (*.csv)")
        if filename:
            self.window._apply(lambda: self.service.export_report(Path(filename)))

