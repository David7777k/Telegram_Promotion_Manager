#!/usr/bin/env python3
"""Telegram Promotion Studio v2 — dark desktop UI for permission-based posts.

Requires only Python 3.10+ with tkinter. No user-account automation.
"""
from __future__ import annotations

import csv
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from model import (
    APP_DIR, STATE_FILE, ISO_FMT, URL_RE, CHAT_RE, load_state, save_state,
    new_campaign, new_target, new_post, uid, by_id, now_str, parse_local_datetime,
    render_text, validate_campaign, audit_creative, may_send, target_cooldown,
    summarize, add_log, export_posts_csv,
)
from telegram_client import BotClient, TelegramError

C = {
    "bg": "#0B1120", "nav": "#10182A", "panel": "#141E30", "panel2": "#19253A",
    "field": "#0E1729", "border": "#2A3651", "text": "#EFF4FF", "muted": "#95A4C0",
    "hint": "#657793", "accent": "#8C7BFA", "accent_hover": "#A599FF",
    "teal": "#3CDABF", "green": "#7DE3AB", "amber": "#FBCB79", "red": "#FF8293", "white": "#FFFFFF",
}
FONT = "Segoe UI"
PAGES = [
    ("overview", "⌂", "Обзор"), ("campaigns", "◈", "Кампании"),
    ("targets", "▦", "Площадки"), ("creatives", "✎", "Креативы"),
    ("planner", "◷", "Планировщик"), ("analytics", "▤", "Аналитика"),
    ("journal", "☷", "Журнал"), ("settings", "⚙", "Настройки"),
]
STATUS = {"planned": "Запланировано", "sending": "Проверить вручную", "sent": "Опубликовано", "failed": "Ошибка", "blocked": "Ограничено"}


def pretty_num(num):
    if isinstance(num, str):
        return num
    if isinstance(num, float) and num.is_integer():
        num = int(num)
    return f"{num:,}".replace(",", " ")


class PromoStudio(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Telegram Promotion Studio 2.0")
        if __import__("sys").platform == "win32":
            try:
                self.iconbitmap(str(Path(__file__).with_name("promotion_studio.ico")))
            except tk.TclError:
                pass
        self.geometry("1370x860")
        self.minsize(1070, 690)
        self.configure(bg=C["bg"])
        self.state_data = load_state()
        self.token = tk.StringVar(value="")
        self.dry_run = tk.BooleanVar(value=bool(self.state_data["settings"].get("dry_run", True)))
        self.auto_schedule = False
        self.busy = False
        self.last_start = 0.0
        self.events = queue.Queue()
        self.current_page = "overview"
        self.sidebar_buttons = {}
        self.live_status = tk.StringVar(value="● ДЕМО-РЕЖИМ")
        self._init_styles()
        self._layout()
        self._navigate("overview")
        self.after(250, self._process_events)
        self.after(3000, self._schedule_tick)
        self.protocol("WM_DELETE_WINDOW", self._exit)
        if self.state_data.get("campaigns"):
            self._save()

    def _init_styles(self):
        theme = ttk.Style(self)
        theme.theme_use("clam")
        theme.configure("Studio.Treeview", background=C["panel"], fieldbackground=C["panel"],
                        foreground=C["text"], borderwidth=0, rowheight=39,
                        font=(FONT, 10), relief="flat")
        theme.configure("Studio.Treeview.Heading", background=C["panel2"], foreground=C["muted"],
                        font=(FONT, 10, "bold"), relief="flat", padding=(12, 11))
        theme.map("Studio.Treeview", background=[("selected", "#343E64")],
                  foreground=[("selected", C["white"])])
        theme.map("Studio.Treeview.Heading", background=[("active", C["panel2"])])
        theme.configure("Studio.Vertical.TScrollbar", background=C["panel2"],
                        troughcolor=C["bg"], arrowsize=13)
        theme.configure("Studio.TCombobox", padding=9, foreground=C["text"],
                        fieldbackground=C["field"], background=C["panel2"])
        self.option_add("*TCombobox*Listbox.background", C["panel2"])
        self.option_add("*TCombobox*Listbox.foreground", C["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", "#363F68")

    def _layout(self):
        self.sidebar = tk.Frame(self, bg=C["nav"], width=232)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        logo = tk.Frame(self.sidebar, bg=C["nav"])
        logo.pack(fill="x", padx=20, pady=(30, 25))
        tk.Label(logo, text="◈", bg=C["accent"], fg="white", font=(FONT, 20, "bold"), width=2,
                 padx=4, pady=5).pack(side="left")
        text_frame = tk.Frame(logo, bg=C["nav"])
        text_frame.pack(side="left", padx=(12, 0))
        self._label(text_frame, "PROMOTION", 12, bold=True, color=C["text"], bg=C["nav"]).pack(anchor="w")
        self._label(text_frame, "STUDIO  /  2.0", 9, bold=True, color=C["accent"], bg=C["nav"]).pack(anchor="w")
        self._label(self.sidebar, "РАБОЧЕЕ ПРОСТРАНСТВО", 9, bold=True,
                    color=C["hint"], bg=C["nav"]).pack(anchor="w", padx=24, pady=(0, 11))
        for key, icon, name in PAGES:
            frame = tk.Frame(self.sidebar, bg=C["nav"])
            frame.pack(fill="x", padx=12, pady=3)
            btn = tk.Button(frame, text=f"{icon}    {name}", anchor="w", relief="flat", bd=0, highlightthickness=0,
                            padx=17, pady=12, font=(FONT, 11), bg=C["nav"], fg=C["muted"],
                            activebackground=C["panel2"], activeforeground=C["text"], cursor="hand2",
                            command=lambda tab=key: self._navigate(tab))
            btn.pack(fill="x")
            self.sidebar_buttons[key] = btn
        tk.Frame(self.sidebar, bg=C["nav"]).pack(fill="both", expand=True)
        footer = tk.Frame(self.sidebar, bg=C["panel"], padx=13, pady=13)
        footer.pack(fill="x", padx=13, pady=20)
        self._label(footer, "БЕЗОПАСНЫЙ ПОДХОД", 9, bold=True, color=C["teal"], bg=C["panel"]).pack(anchor="w")
        self._label(footer, "Только согласованные\nрекламные размещения", 10, color=C["muted"],
                    bg=C["panel"], justify="left").pack(anchor="w", pady=(6, 0))
        self.right = tk.Frame(self, bg=C["bg"])
        self.right.pack(side="left", fill="both", expand=True)
        self.topbar = tk.Frame(self.right, bg=C["bg"], height=74)
        self.topbar.pack(fill="x", padx=34, pady=(18, 5))
        self.topbar.pack_propagate(False)
        self.breadcrumb = self._label(self.topbar, "РАБОЧАЯ ПАНЕЛЬ   /   ОБЗОР", 10,
                                       bold=True, color=C["muted"])
        self.breadcrumb.pack(side="left", pady=18)
        self.status_chip = tk.Label(self.topbar, textvariable=self.live_status,
                                    font=(FONT, 10, "bold"), fg=C["amber"], bg=C["panel"],
                                    padx=17, pady=11)
        self.status_chip.pack(side="right", pady=8)
        self.outer = tk.Frame(self.right, bg=C["bg"])
        self.outer.pack(fill="both", expand=True, padx=(20, 22), pady=(0, 15))
        self.scroll_canvas = tk.Canvas(self.outer, bg=C["bg"], highlightthickness=0, bd=0)
        self.scroll_canvas.pack(side="left", fill="both", expand=True)
        self.vbar = ttk.Scrollbar(self.outer, orient="vertical", command=self.scroll_canvas.yview,
                                   style="Studio.Vertical.TScrollbar")
        self.vbar.pack(side="right", fill="y")
        self.scroll_canvas.configure(yscrollcommand=self.vbar.set)
        self.page_body = tk.Frame(self.scroll_canvas, bg=C["bg"])
        self.page_window = self.scroll_canvas.create_window((0, 0), window=self.page_body, anchor="nw")
        self.page_body.bind("<Configure>", lambda _e: self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all")))
        self.scroll_canvas.bind("<Configure>", lambda e: self.scroll_canvas.itemconfigure(self.page_window, width=e.width))
        self.scroll_canvas.bind("<Enter>", lambda _e: self.bind_all("<MouseWheel>", self._mousewheel))
        self.scroll_canvas.bind("<Leave>", lambda _e: self.unbind_all("<MouseWheel>"))

    def _mousewheel(self, event):
        self.scroll_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _label(self, parent, text, size=11, bold=False, color=None, bg=None, **kwargs):
        return tk.Label(parent, text=text, font=(FONT, size, "bold" if bold else "normal"),
                        fg=color or C["text"], bg=bg or C["bg"], **kwargs)

    def _button(self, parent, text, command, kind="secondary", pad=10, **kwargs):
        colors = {"primary": (C["accent"], C["white"], C["accent_hover"]),
                  "secondary": (C["panel2"], C["text"], "#263652"),
                  "outline": (C["panel"], C["muted"], C["panel2"]),
                  "danger": ("#502A43", C["red"], "#66344F"),
                  "success": ("#1B3D3C", C["teal"], "#265451")}
        bg, fg, hover = colors[kind]
        btn = tk.Button(parent, text=text, command=command, relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                        bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                        padx=17, pady=pad, font=(FONT, 10, "bold"), **kwargs)
        btn.bind("<Enter>", lambda _e: btn.configure(bg=hover))
        btn.bind("<Leave>", lambda _e: btn.configure(bg=bg))
        return btn

    def _panel(self, parent, *, padx=22, pady=20, bg=None):
        return tk.Frame(parent, bg=bg or C["panel"], highlightthickness=1,
                        highlightbackground=C["border"], padx=padx, pady=pady)

    def _page_heading(self, title, sub, action=None):
        row = tk.Frame(self.page_body, bg=C["bg"])
        row.pack(fill="x", padx=13, pady=(7, 23))
        left = tk.Frame(row, bg=C["bg"])
        left.pack(side="left", fill="x", expand=True)
        self._label(left, title, 24, bold=True).pack(anchor="w")
        self._label(left, sub, 10, color=C["muted"]).pack(anchor="w", pady=(7, 0))
        if action:
            text, callback = action
            self._button(row, text, callback, "primary").pack(side="right")

    def _section(self, parent, title, sub="", **kwargs):
        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x", pady=(17, 10), **kwargs)
        self._label(row, title, 15, True).pack(side="left")
        if sub:
            self._label(row, sub, 10, color=C["muted"]).pack(side="right")
        return row

    def _empty(self, parent, title, sub, action=None):
        box = self._panel(parent, padx=24, pady=34)
        box.pack(fill="x", pady=8)
        self._label(box, "◈", 25, True, C["accent"], C["panel"]).pack()
        self._label(box, title, 15, True, bg=C["panel"]).pack(pady=(9, 4))
        self._label(box, sub, 10, color=C["muted"], bg=C["panel"], wraplength=600, justify="center").pack()
        if action:
            self._button(box, action[0], action[1], "primary").pack(pady=(16, 0))

    def _new_table(self, parent, cols, *, height=9):
        frame = self._panel(parent, padx=1, pady=1)
        frame.pack(fill="both", expand=True, pady=(0, 12))
        keys = [col[0] for col in cols]
        tree = ttk.Treeview(frame, columns=keys, show="headings", selectmode="browse",
                            height=height, style="Studio.Treeview")
        for key, name, width in cols:
            tree.heading(key, text=name, anchor="w")
            tree.column(key, width=width, minwidth=65, stretch=True, anchor="w")
        tree.tag_configure("odd", background="#172238")
        tree.tag_configure("even", background=C["panel"])
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview,
                               style="Studio.Vertical.TScrollbar")
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def _toolbar(self, parent, items):
        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x", pady=(0, 15))
        for text, command, kind in items:
            self._button(row, text, command, kind, pad=9).pack(side="left", padx=(0, 8))
        return row

    def _navigate(self, page):
        self.current_page = page
        for key, _icon, label in PAGES:
            active = key == page
            b = self.sidebar_buttons[key]
            b.configure(bg=C["panel2"] if active else C["nav"],
                        fg=C["text"] if active else C["muted"])
        self.breadcrumb.configure(text=f"РАБОЧАЯ ПАНЕЛЬ   /   {next(name.upper() for code, _ic, name in PAGES if code==page)}")
        for child in list(self.page_body.winfo_children()):
            child.destroy()
        self.scroll_canvas.yview_moveto(0)
        {"overview": self._overview, "campaigns": self._campaigns,
         "targets": self._targets, "creatives": self._creatives,
         "planner": self._planner, "analytics": self._analytics,
         "journal": self._journal, "settings": self._settings}[page]()
        self._sync_status()

    def _sync_status(self):
        live = not self.dry_run.get()
        self.live_status.set("● РЕАЛЬНАЯ ОТПРАВКА" if live else "● ДЕМО-РЕЖИМ")
        self.status_chip.configure(fg=C["teal"] if live else C["amber"])

    def _refresh(self):
        self._navigate(self.current_page)

    def _save(self):
        self.state_data["settings"]["dry_run"] = bool(self.dry_run.get())
        save_state(self.state_data)

    def _log(self, level, text):
        add_log(self.state_data, level, text)
        self._save()

    def _chosen(self, tree):
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Выбор", "Выдели строку в таблице.", parent=self)
            return None
        return sel[0]

    # --- Overview ---
    def _metric(self, parent, heading, number, footnote, tint):
        box = self._panel(parent, padx=18, pady=18)
        box.pack(side="left", expand=True, fill="both", padx=(0, 10))
        self._label(box, heading.upper(), 9, bold=True, color=C["muted"], bg=C["panel"]).pack(anchor="w")
        self._label(box, pretty_num(number), 29, bold=True, color=tint, bg=C["panel"]).pack(anchor="w", pady=(8, 4))
        self._label(box, footnote, 9, color=C["hint"], bg=C["panel"]).pack(anchor="w")

    def _overview(self):
        self._page_heading("Обзор продвижения", "Кампании, размещения и результат — всё в одном месте.",
                           ("+ Новая кампания", self._campaign_add))
        hero = self._panel(self.page_body, padx=26, pady=23, bg="#232447")
        hero.pack(fill="x", padx=13, pady=(0, 18))
        self._label(hero, "ПРОДВИЖЕНИЕ С ПОНЯТНЫМ РЕЗУЛЬТАТОМ", 10, True,
                    C["teal"], "#232447").pack(anchor="w")
        self._label(hero, "Не просто отправляй ссылки.\nПонимай, какие площадки приводят подписчиков.",
                    18, True, C["text"], "#232447", justify="left").pack(anchor="w", pady=(9, 8))
        self._label(hero, "Только разрешённые публикации · платные ссылки по источникам · A/B тексты · ручная аналитика",
                    10, color="#B6C3E3", bg="#232447").pack(anchor="w")
        row = tk.Frame(self.page_body, bg=C["bg"])
        row.pack(fill="x", padx=13, pady=(0, 8))
        s = summarize(self.state_data)
        self._metric(row, "Кампании", s["campaigns"], "активных", C["text"])
        self._metric(row, "Площадки", s["targets"], "с разрешением", C["teal"])
        self._metric(row, "Публикации", s["sent"], f"{s['planned']} в плане", C["accent_hover"])
        self._metric(row, "Оплачено", s["paid"], "внесено вручную", C["amber"])
        columns = tk.Frame(self.page_body, bg=C["bg"])
        columns.pack(fill="x", padx=13, pady=(5, 4))
        left = self._panel(columns, padx=20, pady=19)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self._label(left, "Готовность к запуску", 14, True, bg=C["panel"]).pack(anchor="w", pady=(0, 15))
        checks = [
            (bool(self.state_data["campaigns"]), "Создана рекламная кампания"),
            (any(c.get("description") and c.get("link") for c in self.state_data["campaigns"]), "Указаны содержание и стоимость подписки"),
            (bool(self.state_data["targets"]), "Добавлены рекламные площадки"),
            (any(t.get("approved") and t.get("active") for t in self.state_data["targets"]), "Получены разрешения на публикации"),
            (bool(self.state_data["posts"]), "Есть запланированные размещения"),
        ]
        for ready, title in checks:
            self._label(left, ("●  " if ready else "○  ") + title, 10,
                        color=C["teal"] if ready else C["muted"], bg=C["panel"]).pack(anchor="w", pady=5)
        self._button(left, "Открыть планировщик  →", lambda: self._navigate("planner"), "outline").pack(anchor="w", pady=(17, 0))
        right = self._panel(columns, padx=20, pady=19)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._label(right, "Сводная экономика", 14, True, bg=C["panel"]).pack(anchor="w", pady=(0, 16))
        self._econ_row(right, "Валовые поступления", f"{pretty_num(s['stars_gross'])} Stars", C["amber"])
        self._econ_row(right, "Расходы на размещения", f"{pretty_num(s['spent_uah'])} ₴", C["text"])
        self._econ_row(right, "Переходы / просмотры", f"{pretty_num(s['clicks'])} / {pretty_num(s['views'])}", C["text"])
        self._label(right, "Это разные валюты: Stars и гривны не суммируются.\nВсе просмотры, переходы и оплаты здесь вводятся вручную.",
                    9, color=C["muted"], bg=C["panel"], justify="left").pack(anchor="w", pady=(15, 0))
        self._section(self.page_body, "Последние действия")
        events = list(reversed(self.state_data["activity"][-5:]))
        panel = self._panel(self.page_body)
        panel.pack(fill="x", padx=13, pady=(0, 22))
        if not events:
            self._label(panel, "Пока пусто. Добавь первую кампанию, чтобы начать.", 10,
                        color=C["muted"], bg=C["panel"]).pack(anchor="w")
        for event in events:
            item = tk.Frame(panel, bg=C["panel"])
            item.pack(fill="x", pady=7)
            self._label(item, event["at"], 9, color=C["hint"], bg=C["panel"]).pack(side="left", padx=(0, 17))
            self._label(item, event["message"][:135], 10,
                        color=C["red"] if event["level"] == "error" else C["muted"], bg=C["panel"]).pack(side="left")

    def _econ_row(self, parent, label, value, color):
        row = tk.Frame(parent, bg=C["panel"])
        row.pack(fill="x", pady=7)
        self._label(row, label, 10, color=C["muted"], bg=C["panel"]).pack(side="left")
        self._label(row, value, 12, bold=True, color=color, bg=C["panel"]).pack(side="right")

    # --- Reusable form dialog ---
    def _form(self, title, fields, initial, done, hint=""):
        popup = tk.Toplevel(self)
        popup.title(title)
        popup.configure(bg=C["bg"])
        popup.geometry("690x750")
        popup.minsize(580, 570)
        popup.transient(self)
        popup.grab_set()
        heading = tk.Frame(popup, bg=C["bg"])
        heading.pack(fill="x", padx=30, pady=(24, 12))
        self._label(heading, title, 20, True).pack(anchor="w")
        if hint:
            self._label(heading, hint, 10, color=C["muted"], wraplength=600,
                        justify="left").pack(anchor="w", pady=(7, 0))
        area = tk.Frame(popup, bg=C["bg"])
        area.pack(fill="both", expand=True, padx=28)
        canvas = tk.Canvas(area, bg=C["bg"], highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(area, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)
        host = tk.Frame(canvas, bg=C["bg"])
        host_id = canvas.create_window((0, 0), window=host, anchor="nw")
        host.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(host_id, width=e.width))
        controls = {}
        for field in fields:
            key, label, kind = field[:3]
            choices = field[3] if len(field) > 3 else []
            item = tk.Frame(host, bg=C["bg"])
            item.pack(fill="x", pady=8, padx=(2, 15))
            self._label(item, label, 10, True, color=C["muted"]).pack(anchor="w", pady=(0, 6))
            if kind == "bool":
                var = tk.BooleanVar(value=bool(initial.get(key, False)))
                tk.Checkbutton(item, text="Включено / подтверждаю", variable=var,
                               bg=C["bg"], fg=C["text"], activebackground=C["bg"],
                               activeforeground=C["text"], selectcolor=C["panel2"],
                               font=(FONT, 10)).pack(anchor="w")
                controls[key] = (kind, var, choices)
            elif kind == "long":
                t = tk.Text(item, height=5, wrap="word", font=(FONT, 11),
                            bg=C["field"], fg=C["text"], insertbackground=C["text"],
                            selectbackground="#434D83", relief="flat", padx=11, pady=10,
                            highlightthickness=1, highlightbackground=C["border"])
                t.pack(fill="x")
                t.insert("1.0", str(initial.get(key, "")))
                controls[key] = (kind, t, choices)
            elif kind in ("choice", "select"):
                labels = [x[0] if kind == "select" else str(x) for x in choices]
                default_id = initial.get(key, "")
                chosen = next((x[0] for x in choices if kind == "select" and x[1] == default_id), None)
                var = tk.StringVar(value=chosen or (str(default_id) if str(default_id) in labels else (labels[0] if labels else "")))
                comb = ttk.Combobox(item, textvariable=var, values=labels, state="readonly",
                                    font=(FONT, 10), style="Studio.TCombobox")
                comb.pack(fill="x")
                controls[key] = (kind, var, choices)
            else:
                var = tk.StringVar(value=str(initial.get(key, "")))
                entry = tk.Entry(item, textvariable=var, font=(FONT, 11),
                                 bg=C["field"], fg=C["text"], insertbackground=C["text"],
                                 relief="flat", highlightthickness=1,
                                 highlightbackground=C["border"], highlightcolor=C["accent"])
                entry.pack(fill="x", ipady=12, ipadx=12)
                controls[key] = (kind, var, choices)
        footer = tk.Frame(popup, bg=C["bg"])
        footer.pack(fill="x", padx=28, pady=21)
        self._button(footer, "Отмена", popup.destroy, "secondary").pack(side="right", padx=(10, 0))

        def save():
            result = dict(initial)
            try:
                for key, (kind, widget, choices) in controls.items():
                    if kind == "long":
                        result[key] = widget.get("1.0", "end-1c").strip()
                    elif kind == "bool":
                        result[key] = bool(widget.get())
                    elif kind == "select":
                        shown = widget.get()
                        result[key] = next((value for label, value in choices if label == shown), "")
                    else:
                        value = widget.get().strip()
                        if kind == "int":
                            result[key] = int(value)
                        elif kind == "money":
                            result[key] = float(value.replace(",", "."))
                        else:
                            result[key] = value
            except (ValueError, TypeError):
                messagebox.showerror("Проверка", "Ожидалось число в числовом поле.", parent=popup)
                return
            try:
                if done(result) is False:
                    return
            except Exception as exc:
                messagebox.showerror("Ошибка сохранения", str(exc), parent=popup)
                return
            popup.destroy()
            self._refresh()
        self._button(footer, "Сохранить изменения", save, "primary").pack(side="right")
        popup.bind("<Escape>", lambda _e: popup.destroy())
        return popup

    def _validate_campaign_editor(self, data):
        if data.get("budget_uah", 0) < 0:
            messagebox.showerror("Бюджет", "Бюджет не может быть отрицательным.", parent=self)
            return False
        if data.get("link") and not URL_RE.fullmatch(data["link"]):
            messagebox.showerror("Ссылка", "Нужна Telegram-ссылка https://t.me/...", parent=self)
            return False
        try:
            if not (1 <= int(data.get("price", 0)) <= 10000):
                raise ValueError()
        except (ValueError, TypeError):
            messagebox.showerror("Цена", "Цена должна быть от 1 до 10 000 Stars.", parent=self)
            return False
        if data.get("channel_chat_id") and not CHAT_RE.fullmatch(data["channel_chat_id"]):
            messagebox.showerror("ID канала", "Укажи @username или числовой ID канала.", parent=self)
            return False
        if not data.get("name", "").strip():
            messagebox.showerror("Название", "Название кампании обязательно.", parent=self)
            return False
        return True

    def _campaign_fields(self):
        return [
            ("name", "Название кампании", "text"),
            ("channel", "Название рекламируемого Telegram-канала", "text"),
            ("channel_chat_id", "ID своего канала (@username / -100...), для платных ссылок", "text"),
            ("topic", "Тематика и интересы целевой аудитории", "text"),
            ("description", "Что человек реально получит за подписку?", "long"),
            ("link", "Платная ссылка t.me (обязательно для публикаций)", "text"),
            ("price", "Цена подписки Stars / каждые 30 дней", "int"),
            ("budget_uah", "Рекламный бюджет, грн (учёт отдельно от Stars)", "money"),
            ("content_ready", "В канале уже размещён обещанный контент", "bool"),
            ("active", "Кампания активна", "bool"),
        ]

    # --- Campaigns ---
    def _campaigns(self):
        self._page_heading("Кампании", "Предложение, цена, аудитория и источник подписки.",
                           ("+ Создать кампанию", self._campaign_add))
        self._toolbar(self.page_body, [("Изменить", self._campaign_edit, "secondary"),
                                        ("Выключить / включить", self._campaign_toggle, "outline"),
                                        ("Удалить", self._campaign_delete, "danger")])
        self.campaign_table = self._new_table(self.page_body, [
            ("name", "КАМПАНИЯ", 210), ("topic", "ТЕМАТИКА", 190),
            ("price", "СТОИМОСТЬ", 155), ("budget", "БЮДЖЕТ", 140),
            ("status", "СТАТУС", 145)], height=12)
        for i, c in enumerate(self.state_data["campaigns"]):
            self.campaign_table.insert("", "end", iid=c["id"], tags=("even" if i % 2 == 0 else "odd",),
                                       values=(c.get("name"), c.get("topic", "—") or "—",
                                               f"{c.get('price', '?')} Stars / 30 дн.",
                                               f"{pretty_num(c.get('budget_uah', 0))} ₴",
                                               "● Активна" if c.get("active") else "○ Отключена"))
        self.campaign_table.bind("<Double-1>", lambda _e: self._campaign_edit())
        info = self._panel(self.page_body)
        info.pack(fill="x", padx=13, pady=(10, 15))
        self._label(info, "✓ Принцип эффективного объявления", 12, True, C["teal"], C["panel"]).pack(anchor="w")
        self._label(info, "Сначала объясни ценность контента и кому он нужен. Обязательно указывай: 250 Stars — это плата за каждые 30 дней, а не разовый вход.",
                    10, color=C["muted"], bg=C["panel"], wraplength=850, justify="left").pack(anchor="w", pady=(8, 0))
        if not self.state_data["campaigns"]:
            self._empty(self.page_body, "Пока нет кампаний", "Начни с честного описания платного канала.",
                        ("Создать первую", self._campaign_add))

    def _campaign_add(self):
        draft = new_campaign("Новая кампания")
        draft["content_ready"] = False
        def finish(data):
            if not self._validate_campaign_editor(data): return False
            self.state_data["campaigns"].append(data)
            self.state_data["selected_campaign"] = data["id"]
            self._log("info", f"Создана кампания «{data['name']}»")
        self._form("Новая кампания", self._campaign_fields(), draft, finish,
                   "Укажи реальные условия: подписка в Stars повторяется каждые 30 дней.")

    def _campaign_edit(self):
        id_ = self._chosen(self.campaign_table)
        if not id_: return
        camp = by_id(self.state_data["campaigns"], id_)
        def finish(data):
            if not self._validate_campaign_editor(data): return False
            camp.update(data)
            self._log("info", f"Обновлена кампания «{camp['name']}»")
        self._form("Редактировать кампанию", self._campaign_fields(), camp, finish)

    def _campaign_toggle(self):
        id_ = self._chosen(self.campaign_table)
        if not id_: return
        camp = by_id(self.state_data["campaigns"], id_)
        camp["active"] = not camp.get("active", True)
        self._log("info", f"Кампания «{camp['name']}»: {'активна' if camp['active'] else 'приостановлена'}")
        self._refresh()

    def _campaign_delete(self):
        id_ = self._chosen(self.campaign_table)
        if not id_: return
        if any(p["campaign_id"] == id_ for p in self.state_data["posts"]):
            messagebox.showwarning("Нельзя удалить", "Кампания связана с размещениями. Отключи её, чтобы сохранить статистику.", parent=self)
            return
        if messagebox.askyesno("Удалить", "Удалить кампанию без возможности восстановления?", parent=self):
            self.state_data["campaigns"] = [c for c in self.state_data["campaigns"] if c["id"] != id_]
            self._log("info", "Кампания удалена")
            self._refresh()

    # --- Targets ---
    def _target_fields(self):
        return [
            ("name", "Название площадки", "text"),
            ("chat_id", "Chat ID или @username канала / группы", "text"),
            ("type", "Тип площадки", "choice", ["Партнёрский канал", "Свой канал", "Партнёрская группа", "Своя группа"]),
            ("topic", "Тематика площадки", "text"),
            ("audience", "Примерное количество подписчиков", "int"),
            ("contact", "Контакт администратора (не для автоматической переписки)", "text"),
            ("cost_uah", "Договорённая цена размещения, грн", "money"),
            ("cooldown_days", "Минимальный интервал повторных постов (дней)", "int"),
            ("approval_note", "Основание разрешения: когда и кто согласовал рекламу", "long"),
            ("approved", "Есть явное разрешение на рекламу", "bool"),
            ("active", "Площадка включена", "bool"),
        ]

    def _targets(self):
        self._page_heading("Рекламные площадки", "Каталог согласованных каналов и групп с аудиторией и условиями.",
                           ("+ Добавить площадку", self._target_add))
        self._toolbar(self.page_body, [("Изменить", self._target_edit, "secondary"),
                                        ("Проверить права бота", self._target_check, "success"),
                                        ("Удалить", self._target_delete, "danger")])
        self.target_table = self._new_table(self.page_body, [
            ("name", "ПЛОЩАДКА", 205), ("topic", "ТЕМА", 160),
            ("audience", "АУДИТОРИЯ", 115), ("cost", "СТОИМОСТЬ", 120),
            ("interval", "ПОВТОР", 105), ("approval", "РЕКЛАМА", 145)], height=13)
        for i, t in enumerate(self.state_data["targets"]):
            state = "✓ Согласовано" if t.get("approved") and t.get("active") else "— Нет доступа"
            self.target_table.insert("", "end", iid=t["id"], tags=("odd" if i % 2 else "even",),
                                      values=(t.get("name", ""), t.get("topic", "—") or "—",
                                              pretty_num(t.get("audience", 0)), f"{pretty_num(t.get('cost_uah', 0))} ₴",
                                              f"{t.get('cooldown_days', 7)} дн.", state))
        self.target_table.bind("<Double-1>", lambda _e: self._target_edit())
        box = self._panel(self.page_body)
        box.pack(fill="x", padx=13, pady=(8, 16))
        self._label(box, "Как выбирать площадки", 12, True, C["teal"], C["panel"]).pack(anchor="w")
        self._label(box, "Приоритет — совпадение тематики и доверие аудитории, а не максимальное количество чатов. Права администратора у бота НЕ означают согласия на рекламу.",
                    10, color=C["muted"], bg=C["panel"], justify="left", wraplength=870).pack(anchor="w", pady=(8, 0))

    def _valid_target(self, data):
        if not data.get("name") or not CHAT_RE.fullmatch(data.get("chat_id", "")):
            messagebox.showerror("Площадка", "Нужно название и корректный @username / chat ID.", parent=self)
            return False
        if data.get("audience", 0) < 0 or data.get("cost_uah", 0) < 0 or not (1 <= data.get("cooldown_days", 0) <= 365):
            messagebox.showerror("Площадка", "Проверь аудиторию, стоимость и интервал 1–365 дней.", parent=self)
            return False
        if data.get("approved") and not str(data.get("approval_note", "")).strip():
            messagebox.showerror("Разрешение", "Зафиксируй, кто и как разрешил размещать рекламу. Для своей площадки напиши «Собственный канал».", parent=self)
            return False
        return True

    def _target_add(self):
        draft = new_target("Новая площадка", "")
        def finish(data):
            if not self._valid_target(data): return False
            if any(t.get("chat_id") == data["chat_id"] for t in self.state_data["targets"]):
                messagebox.showerror("Дубликат", "Площадка с таким ID уже добавлена.", parent=self)
                return False
            self.state_data["targets"].append(data)
            self._log("info", f"Добавлена площадка «{data['name']}»")
        self._form("Новая площадка", self._target_fields(), draft, finish,
                   "Разрешение администратора обязательно. Не используем личные сообщения незнакомым людям.")

    def _target_edit(self):
        id_ = self._chosen(self.target_table)
        if not id_: return
        target = by_id(self.state_data["targets"], id_)
        def finish(data):
            if not self._valid_target(data): return False
            if data["chat_id"] != target.get("chat_id"):
                if any(p["target_id"] == id_ for p in self.state_data["posts"]):
                    messagebox.showerror("ID площадки", "Нельзя менять Chat ID площадки, связанной с размещениями. Создай новую площадку, чтобы сохранить историю.", parent=self)
                    return False
                data["approved"] = False
                data["approval_note"] = ""
                data["last_sent_at"] = ""
            target.update(data)
            self._log("info", f"Обновлена площадка «{target['name']}»")
        self._form("Площадка и разрешение", self._target_fields(), target, finish)

    def _target_delete(self):
        id_ = self._chosen(self.target_table)
        if not id_: return
        if any(p["target_id"] == id_ for p in self.state_data["posts"]):
            messagebox.showwarning("Нельзя удалить", "Площадка связана с размещениями и статистикой. Отключи её в редакторе.", parent=self)
            return
        if messagebox.askyesno("Удалить", "Удалить площадку?", parent=self):
            self.state_data["targets"] = [t for t in self.state_data["targets"] if t["id"] != id_]
            self._log("info", "Площадка удалена")
            self._refresh()

    def _target_check(self):
        id_ = self._chosen(self.target_table)
        if not id_: return
        target = by_id(self.state_data["targets"], id_)
        if not self.token.get().strip():
            messagebox.showinfo("Telegram", "Сначала укажи токен бота во вкладке «Настройки».", parent=self)
            self._navigate("settings")
            return
        if self.busy: return
        self._run_worker("target_check", target["id"], target["chat_id"])

    # --- Creatives ---
    def _creatives(self):
        self._page_heading("Креативы и A/B тексты", "Несколько подходов к одному предложению, прозрачные условия и предпросмотр.",
                           ("+ Новый креатив", self._template_add))
        self._toolbar(self.page_body, [("Редактировать", self._template_edit, "secondary"),
                                        ("Предпросмотр", self._template_preview, "success"),
                                        ("Удалить", self._template_delete, "danger")])
        self.template_table = self._new_table(self.page_body, [
            ("name", "НАЗВАНИЕ / ВАРИАНТ", 240), ("preview", "ТЕКСТ", 490),
            ("length", "СИМВОЛОВ", 110)], height=10)
        for i, t in enumerate(self.state_data["templates"]):
            self.template_table.insert("", "end", iid=t["id"], tags=("odd" if i%2 else "even",),
                                       values=(t["name"], t["body"].replace("\n", "  ·  ")[:110], len(t["body"])))
        self.template_table.bind("<Double-1>", lambda _e: self._template_edit())
        box = self._panel(self.page_body)
        box.pack(fill="x", padx=13, pady=10)
        self._label(box, "Переменные в текстах", 13, True, C["teal"], C["panel"]).pack(anchor="w")
        self._label(box, "{channel}  ·  {description}  ·  {topic}  ·  {price}  ·  {link}  ·  {platform}",
                    11, bg=C["panel"]).pack(anchor="w", pady=(12, 4))
        self._label(box, "В A/B тесте сравнивай одинаковые площадки/периоды, а не делай выводы только по количеству публикаций. Условия оплаты не скрывать.",
                    10, color=C["muted"], bg=C["panel"], wraplength=870, justify="left").pack(anchor="w")
        self._button(box, "Проверить лимит Telegram Ads: 160 символов", self._ads_check, "outline").pack(anchor="w", pady=(15, 0))

    def _template_add(self):
        draft = {"id": uid(), "name": "Новый вариант", "body": "{channel}\n{description}\nДоступ: {price} Stars каждые 30 дней.\n{link}"}
        def finish(data):
            if not data["name"] or not data["body"].strip():
                messagebox.showerror("Креатив", "Название и текст обязательны.", parent=self)
                return False
            self.state_data["templates"].append(data)
            self._log("info", f"Добавлен креатив «{data['name']}»")
        self._form("Новый креатив", [("name", "Название варианта", "text"),
                                      ("body", "Текст с переменными", "long")], draft, finish)

    def _template_edit(self):
        id_ = self._chosen(self.template_table)
        if not id_: return
        tmpl = by_id(self.state_data["templates"], id_)
        def finish(data):
            if not data["name"] or not data["body"].strip():
                messagebox.showerror("Креатив", "Название и текст обязательны.", parent=self)
                return False
            tmpl.update(data)
            self._log("info", f"Обновлён креатив «{tmpl['name']}»")
        self._form("Редактор креатива", [("name", "Название варианта", "text"),
                                          ("body", "Текст с переменными", "long")], tmpl, finish)

    def _template_delete(self):
        id_ = self._chosen(self.template_table)
        if not id_: return
        if any(p["template_id"] == id_ for p in self.state_data["posts"]):
            messagebox.showwarning("Нельзя удалить", "Этот вариант используется в размещениях.", parent=self)
            return
        if len(self.state_data["templates"]) <= 1:
            messagebox.showwarning("Креатив", "Нужен хотя бы один шаблон.", parent=self)
            return
        self.state_data["templates"] = [t for t in self.state_data["templates"] if t["id"] != id_]
        self._log("info", "Креатив удалён")
        self._refresh()

    def _get_creative_data(self):
        id_ = self._chosen(self.template_table)
        if not id_: return None
        tmpl = by_id(self.state_data["templates"], id_)
        if not self.state_data["campaigns"]:
            messagebox.showinfo("Кампания", "Сначала создай кампанию с реальным описанием канала.", parent=self)
            return None
        camp = by_id(self.state_data["campaigns"], self.state_data.get("selected_campaign", "")) or self.state_data["campaigns"][0]
        return tmpl, camp

    def _template_preview(self):
        data = self._get_creative_data()
        if not data: return
        tmpl, camp = data
        text = render_text(tmpl["body"], camp)
        risks = audit_creative(text, camp)
        self._show_preview(f"Предпросмотр · {tmpl['name']}", text, risks)

    def _ads_check(self):
        data = self._get_creative_data()
        if not data: return
        tmpl, camp = data
        text = render_text(tmpl["body"], camp)
        risks = audit_creative(text, camp, ads=True)
        self._show_preview(f"Проверка для Telegram Ads · {len(text)}/160", text, risks,
                           "Официальные Telegram Ads используют отдельный формат. Это проверка части редакционных правил, НЕ гарантия одобрения.")

    def _show_preview(self, heading, content, risks, note=""):
        modal = tk.Toplevel(self)
        modal.title(heading)
        modal.geometry("700x650")
        modal.configure(bg=C["bg"])
        modal.transient(self)
        modal.grab_set()
        main = tk.Frame(modal, bg=C["bg"], padx=27, pady=24)
        main.pack(fill="both", expand=True)
        self._label(main, heading, 17, True).pack(anchor="w", pady=(0, 12))
        chat = self._panel(main, padx=20, pady=16)
        chat.pack(fill="both", expand=True)
        self._label(chat, "Пример объявления", 10, True, C["teal"], C["panel"]).pack(anchor="w", pady=(0, 11))
        t = tk.Text(chat, wrap="word", font=(FONT, 11), bg=C["panel"], fg=C["text"],
                    relief="flat", highlightthickness=0, padx=0, pady=0)
        t.insert("1.0", content)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True)
        report = self._panel(main, padx=16, pady=12)
        report.pack(fill="x", pady=(13, 0))
        if not risks:
            self._label(report, "✓ Базовая проверка текста пройдена", 10, True, C["teal"], C["panel"]).pack(anchor="w")
        for severity, message in risks:
            self._label(report, ("✕ " if severity == "error" else "! ") + message, 10,
                        color=C["red"] if severity == "error" else C["amber"],
                        bg=C["panel"], wraplength=600, justify="left").pack(anchor="w", pady=4)
        if note:
            self._label(main, note, 9, color=C["muted"], wraplength=610, justify="left").pack(anchor="w", pady=(10, 0))
        controls = tk.Frame(main, bg=C["bg"])
        controls.pack(fill="x", pady=(13, 0))
        self._button(controls, "Закрыть", modal.destroy).pack(side="right")
        def copy():
            self.clipboard_clear()
            self.clipboard_append(content)
            modal.destroy()
        self._button(controls, "Скопировать текст", copy, "primary").pack(side="right", padx=(0, 10))

    # --- Planner ---
    def _post_fields(self):
        campaigns = [(c["name"] + " · " + c["id"][:5], c["id"]) for c in self.state_data["campaigns"]]
        targets = [(t["name"] + " · " + t["id"][:5], t["id"]) for t in self.state_data["targets"]]
        creatives = [(t["name"] + " · " + t["id"][:5], t["id"]) for t in self.state_data["templates"]]
        return [("campaign_id", "Кампания", "select", campaigns),
                ("target_id", "Площадка", "select", targets),
                ("template_id", "Вариант креатива", "select", creatives),
                ("scheduled_at", "Дата и время публикации (ГГГГ-ММ-ДД ЧЧ:ММ)", "text"),
                ("source_link", "Уникальная платная ссылка этого источника (необязательно)", "text")]

    def _planner(self):
        self._page_heading("Планировщик размещений", "Очередь, предпросмотр перед отправкой, отдельные ссылки для источников.",
                           ("+ Запланировать", self._post_add))
        self._toolbar(self.page_body, [("Посмотреть текст", self._post_preview, "secondary"),
                                        ("Отправить выбранное", self._post_send, "primary"),
                                        ("Платная ссылка для источника", self._post_source_link, "success")])
        self._toolbar(self.page_body, [("Редактировать", self._post_edit, "outline"),
                                        ("Удалить", self._post_delete, "danger"),
                                        ("Снять ошибку / повторить вручную", self._post_reset, "outline")])
        self.post_table = self._new_table(self.page_body, [
            ("when", "КОГДА", 152), ("campaign", "КАМПАНИЯ", 155),
            ("target", "ПЛОЩАДКА", 180), ("template", "ВАРИАНТ", 164),
            ("status", "СТАТУС", 135), ("link", "ССЫЛКА", 90)], height=13)
        posts = sorted(self.state_data["posts"], key=lambda p: p.get("scheduled_at", ""), reverse=False)
        for i, p in enumerate(posts):
            camp = by_id(self.state_data["campaigns"], p["campaign_id"]) or {}
            target = by_id(self.state_data["targets"], p["target_id"]) or {}
            tmpl = by_id(self.state_data["templates"], p["template_id"]) or {}
            self.post_table.insert("", "end", iid=p["id"], tags=("odd" if i%2 else "even",),
                                   values=(p.get("scheduled_at"), camp.get("name", "[удалена]"),
                                           target.get("name", "[удалена]"), tmpl.get("name", "[удалён]"),
                                           STATUS.get(p["status"], p["status"]), "отдельная" if p.get("source_link") else "общая"))
        self.post_table.bind("<Double-1>", lambda _e: self._post_preview())
        box = self._panel(self.page_body)
        box.pack(fill="x", padx=13, pady=12)
        self._label(box, "Расписание по умолчанию НЕ запускает отправку", 12, True, C["amber"], C["panel"]).pack(anchor="w")
        self._label(box, "Автоотправку можно отдельно включить в Настройках после проверки токена, прав бота и согласования площадок. Программа должна оставаться открытой.",
                    10, color=C["muted"], bg=C["panel"], wraplength=910, justify="left").pack(anchor="w", pady=(6, 0))
        self._label(box, "Тестовый режим не меняет статус публикации. Telegram 429 останавливает автоотправку: автоматических повторов нет.",
                    10, color=C["muted"], bg=C["panel"], wraplength=910, justify="left").pack(anchor="w", pady=(4, 0))

    def _post_add(self):
        if not self.state_data["campaigns"] or not self.state_data["targets"]:
            messagebox.showinfo("Нет данных", "Создай кампанию и добавь хотя бы одну разрешённую площадку.", parent=self)
            return
        draft = new_post(self.state_data["campaigns"][0]["id"], self.state_data["targets"][0]["id"],
                         self.state_data["templates"][0]["id"],
                         (datetime.now() + timedelta(minutes=10)).strftime(ISO_FMT))
        def finish(data):
            if not self._valid_post(data): return False
            self.state_data["posts"].append(data)
            self._log("info", f"Запланировано размещение на {data['scheduled_at']}")
        self._form("Запланировать размещение", self._post_fields(), draft, finish,
                   "Один пост — одна согласованная площадка. Интервал повторов на площадке проверяется автоматически.")

    def _valid_post(self, data):
        try:
            when = parse_local_datetime(data["scheduled_at"])
        except ValueError:
            messagebox.showerror("Дата", "Используй формат ГГГГ-ММ-ДД ЧЧ:ММ, например 2026-11-15 18:30.", parent=self)
            return False
        if when < datetime.now() - timedelta(minutes=5):
            messagebox.showerror("Дата", "Планируемая публикация не может быть в далёком прошлом.", parent=self)
            return False
        camp = by_id(self.state_data["campaigns"], data.get("campaign_id"))
        target = by_id(self.state_data["targets"], data.get("target_id"))
        tmpl = by_id(self.state_data["templates"], data.get("template_id"))
        if not camp or not target or not tmpl:
            messagebox.showerror("Данные", "Выбери существующие кампанию, площадку и вариант текста.", parent=self)
            return False
        if not camp.get("active"):
            messagebox.showerror("Кампания", "Кампания выключена.", parent=self)
            return False
        if data.get("source_link") and not URL_RE.fullmatch(data["source_link"]):
            messagebox.showerror("Ссылка", "Уникальная ссылка должна иметь формат https://t.me/...", parent=self)
            return False
        ready, note = target_cooldown(self.state_data, target, when, excluding_post_id=data["id"])
        if not ready:
            messagebox.showerror("Размещение недоступно", note, parent=self)
            return False
        # Allow composing draft campaigns; actual posting has mandatory validation.
        return True

    def _post_edit(self):
        id_ = self._chosen(self.post_table)
        if not id_: return
        p = by_id(self.state_data["posts"], id_)
        if p["status"] != "planned":
            messagebox.showwarning("Запись защищена", "После отправки запись сохраняет историю. Изменения статистики доступны во вкладке «Аналитика».", parent=self)
            return
        def finish(data):
            if not self._valid_post(data): return False
            p.update(data)
            self._log("info", "Изменено запланированное размещение")
        self._form("Редактировать размещение", self._post_fields(), p, finish)

    def _post_delete(self):
        id_ = self._chosen(self.post_table)
        if not id_: return
        p = by_id(self.state_data["posts"], id_)
        if p["status"] == "sent":
            messagebox.showwarning("История", "Опубликованное размещение нельзя удалить: оно участвует в статистике.", parent=self)
            return
        if messagebox.askyesno("Удалить", "Удалить выбранное размещение из очереди?", parent=self):
            self.state_data["posts"] = [item for item in self.state_data["posts"] if item["id"] != id_]
            self._log("info", "Удалена запись из очереди")
            self._refresh()

    def _post_reset(self):
        id_ = self._chosen(self.post_table)
        if not id_: return
        post = by_id(self.state_data["posts"], id_)
        if post["status"] not in ("failed", "blocked"):
            messagebox.showwarning("Повтор", "Повтор разрешён только для публикаций с ошибками.", parent=self)
            return
        if messagebox.askyesno("Повторить", "Вернуть размещение в план? Повтор можно будет запустить вручную после проверки причины ошибки.", parent=self):
            post["status"] = "planned"
            post["error"] = ""
            post["scheduled_at"] = (datetime.now() + timedelta(minutes=5)).strftime(ISO_FMT)
            self._log("info", "Размещение возвращено в план вручную")
            self._refresh()

    def _post_preview(self):
        id_ = self._chosen(self.post_table)
        if not id_: return
        post = by_id(self.state_data["posts"], id_)
        camp = by_id(self.state_data["campaigns"], post["campaign_id"])
        target = by_id(self.state_data["targets"], post["target_id"])
        tmpl = by_id(self.state_data["templates"], post["template_id"])
        if not camp or not target or not tmpl:
            messagebox.showerror("Ошибка", "Отсутствует кампания, площадка или креатив.", parent=self)
            return
        rendered = render_text(tmpl["body"], camp, target, link=post.get("source_link") or camp.get("link", ""))
        notes = audit_creative(rendered, camp)
        self._show_preview(f"{target['name']} · {tmpl['name']}", rendered, notes)

    def _post_send(self):
        id_ = self._chosen(self.post_table)
        if not id_: return
        self._send_post(id_, manual=True)

    def _send_post(self, post_id, *, manual):
        if self.busy: return
        post = by_id(self.state_data["posts"], post_id)
        if not post: return
        ok, reason = may_send(self.state_data, post)
        if not ok:
            if manual:
                messagebox.showerror("Проверка перед отправкой", reason, parent=self)
            else:
                post["status"] = "blocked"
                post["error"] = reason
                self._log("error", f"Размещение пропущено: {reason}")
                self._refresh()
            return
        camp = by_id(self.state_data["campaigns"], post["campaign_id"])
        target = by_id(self.state_data["targets"], post["target_id"])
        tmpl = by_id(self.state_data["templates"], post["template_id"])
        text = render_text(tmpl["body"], camp, target, link=post.get("source_link") or camp["link"])
        if self.dry_run.get():
            self._log("test", f"ТЕСТ: {target['name']} · сообщение {len(text)} симв. Ничего не отправлено.")
            if manual:
                self._show_preview("Тестовый режим · без отправки", text, audit_creative(text, camp))
            else:
                self._refresh()
            return
        token = self.token.get().strip()
        if not token:
            if manual: messagebox.showerror("Токен", "Введи токен бота в Настройках.", parent=self)
            return
        if manual and not messagebox.askyesno("Подтверждение", f"Отправить реальную рекламу на площадку «{target['name']}»?\n\nПроверь согласие администратора, текст, цену и содержимое канала.", parent=self):
            return
        # Save an in-flight marker BEFORE asking Telegram to send. If the app crashes
        # after Telegram accepts a post, it must not silently resend after restart.
        post["status"] = "sending"
        self._save()
        self._run_worker("send", post_id, target["chat_id"], text)

    def _post_source_link(self):
        id_ = self._chosen(self.post_table)
        if not id_: return
        post = by_id(self.state_data["posts"], id_)
        if post["status"] != "planned":
            messagebox.showwarning("Ссылка", "Отдельную ссылку можно создать только для ещё не опубликованного размещения.", parent=self)
            return
        camp = by_id(self.state_data["campaigns"], post["campaign_id"])
        target = by_id(self.state_data["targets"], post["target_id"])
        if post.get("source_link"):
            if messagebox.askyesno("Ссылка есть", "Для этого размещения уже создана ссылка. Скопировать её?", parent=self):
                self.clipboard_clear(); self.clipboard_append(post["source_link"])
            return
        if not camp.get("channel_chat_id"):
            messagebox.showerror("ID своего канала", "Добавь ID или @username своего платного канала в настройках кампании.", parent=self)
            return
        if not self.token.get().strip():
            messagebox.showerror("Токен", "В настройках нужен Bot API токен. Бот должен быть администратором платного канала с правом приглашать пользователей.", parent=self)
            return
        if not messagebox.askyesno("Создать платную ссылку", f"Создать через Telegram Bot API уникальную ссылку для «{target['name']}»?\n\nОплата: {camp['price']} Stars каждые 30 дней. Создание ссылки само по себе не публикует рекламу.", parent=self):
            return
        self._run_worker("link", id_, camp["channel_chat_id"], int(camp["price"]), target["name"])

    # --- Analytics ---
    def _analytics(self):
        self._page_heading("Аналитика эффективности", "Статистика по площадкам и вариантам текстов. Значения вносятся вручную.",
                           ("Экспорт CSV", self._analytics_export))
        s = summarize(self.state_data)
        row = tk.Frame(self.page_body, bg=C["bg"])
        row.pack(fill="x", padx=13, pady=(0, 14))
        self._metric(row, "Просмотры", s["views"], "по вашим данным", C["text"])
        self._metric(row, "Клики", s["clicks"], "по вашим данным", C["accent_hover"])
        self._metric(row, "Подписки", s["paid"], "оплаченные, вручную", C["teal"])
        ctr = f"{(100*s['clicks']/s['views']):.1f}%" if s["views"] else "—"
        self._metric(row, "CTR", ctr, "клики / просмотры", C["amber"])
        self._toolbar(self.page_body, [("Внести показатели", self._analytics_edit, "primary"),
                                        ("Открыть размещение", self._analytics_preview, "secondary")])
        self.analytic_table = self._new_table(self.page_body, [
            ("target", "ПЛОЩАДКА", 190), ("campaign", "КАМПАНИЯ", 150),
            ("views", "ПОКАЗЫ", 105), ("clicks", "КЛИКИ", 90),
            ("paid", "ОПЛАТЫ", 96), ("conv", "КЛИК → ОПЛАТА", 125),
            ("cost", "РАСХОД ₴", 110), ("stars", "STARS ВАЛ.", 120)], height=11)
        sent = [p for p in self.state_data["posts"] if p.get("status") == "sent"]
        for i, p in enumerate(reversed(sent)):
            camp = by_id(self.state_data["campaigns"], p["campaign_id"]) or {}
            target = by_id(self.state_data["targets"], p["target_id"]) or {}
            clicks, paid = int(p.get("clicks", 0)), int(p.get("paid", 0))
            conv = f"{100*paid/clicks:.1f}%" if clicks else "—"
            self.analytic_table.insert("", "end", iid=p["id"], tags=("odd" if i%2 else "even",),
                                        values=(target.get("name", "—"), camp.get("name", "—"),
                                                pretty_num(p.get("views", 0)), pretty_num(clicks),
                                                pretty_num(paid), conv, pretty_num(p.get("actual_cost_uah", 0)),
                                                pretty_num(paid*int(p.get("price_at_send") or camp.get("price", 0)))))
        self.analytic_table.bind("<Double-1>", lambda _e: self._analytics_edit())
        self._funnel(self.page_body, s["views"], s["clicks"], s["paid"])
        self._comparison()
        box = self._panel(self.page_body)
        box.pack(fill="x", padx=13, pady=12)
        self._label(box, "Как сравнивать результаты корректно", 12, True, C["teal"], C["panel"]).pack(anchor="w")
        self._label(box, "CTR = клики / просмотры. Конверсия = оплаченные подписки / клики. Это данные, внесённые вручную, а не проверенные Telegram API.\n"
                          "Stars и UAH нельзя превращать в точную прибыль без учёта фактического вывода, комиссий и курса. Не считай каждый переход оплатой.",
                    10, color=C["muted"], bg=C["panel"], justify="left", wraplength=900).pack(anchor="w", pady=(9, 0))

    def _funnel(self, parent, views, clicks, paid):
        """Text+canvas funnel, proportional widths, no claimed automatic tracking."""
        box = self._panel(parent)
        box.pack(fill="x", padx=13, pady=(10, 12))
        self._label(box, "Воронка привлечения", 15, True, bg=C["panel"]).pack(anchor="w", pady=(0, 11))
        bars = [("Просмотры", views, C["accent"]), ("Переходы", clicks, C["teal"]),
                ("Оплаченные подписки", paid, C["amber"])]
        for name, amount, tint in bars:
            row = tk.Frame(box, bg=C["panel"])
            row.pack(fill="x", pady=6)
            self._label(row, name, 10, color=C["muted"], bg=C["panel"], width=23, anchor="w").pack(side="left")
            bar = tk.Canvas(row, height=12, bg=C["panel2"], bd=0, highlightthickness=0)
            bar.pack(side="left", fill="x", expand=True, padx=(4, 16))
            def draw(_e, cv=bar, val=amount, maxval=max(views, clicks, paid, 1), c=tint):
                cv.delete("all")
                cv.create_rectangle(0, 0, max(3, cv.winfo_width() * val/maxval), 12,
                                    fill=c, outline="") if val else None
            bar.bind("<Configure>", draw)
            self._label(row, pretty_num(amount), 11, True, color=C["text"],
                        bg=C["panel"], width=11, anchor="e").pack(side="right")
        text = f"Из просмотров в переход: {100*clicks/views:.1f}%" if views else "Для расчёта CTR внеси просмотры и переходы"
        text += f"    ·    Из переходов в оплату: {100*paid/clicks:.1f}%" if clicks else "    ·    Конверсия в оплату пока неизвестна"
        self._label(box, text, 10, color=C["muted"], bg=C["panel"]).pack(anchor="w", pady=(12, 0))

    def _comparison(self):
        self._section(self.page_body, "Какие размещения дают лучший результат", "По внесённым вручную данным")
        per_platform = {}
        per_creative = {}
        for p in self.state_data["posts"]:
            if p.get("status") != "sent": continue
            for storage, identifier in ((per_platform, p["target_id"]), (per_creative, p["template_id"])):
                row = storage.setdefault(identifier, {"clicks": 0, "paid": 0, "views": 0, "cost": 0, "posts": 0})
                row["clicks"] += int(p.get("clicks", 0))
                row["paid"] += int(p.get("paid", 0))
                row["views"] += int(p.get("views", 0))
                row["cost"] += float(p.get("actual_cost_uah", 0))
                row["posts"] += 1
        grid = tk.Frame(self.page_body, bg=C["bg"])
        grid.pack(fill="x", padx=13, pady=(0, 17))
        for heading, dataset, origin in (("По площадкам", per_platform, "targets"),
                                         ("По креативам", per_creative, "templates")):
            box = self._panel(grid)
            box.pack(side="left", expand=True, fill="both", padx=(0, 9) if origin == "targets" else (9, 0))
            self._label(box, heading, 13, True, bg=C["panel"]).pack(anchor="w", pady=(0, 12))
            ranked = sorted(dataset.items(), key=lambda kv: (kv[1]["paid"], kv[1]["clicks"]), reverse=True)[:5]
            if not ranked or not any(row["paid"] or row["clicks"] for _, row in ranked):
                self._label(box, "Пока недостаточно подтверждённых данных\nдля сравнения вариантов.", 10,
                            color=C["muted"], bg=C["panel"], justify="left").pack(anchor="w", pady=8)
                continue
            for key, item in ranked:
                obj = by_id(self.state_data[origin], key) or {}
                row = tk.Frame(box, bg=C["panel"])
                row.pack(fill="x", pady=8)
                self._label(row, obj.get("name", "[удалено]")[:24], 10,
                            color=C["text"], bg=C["panel"]).pack(side="left")
                self._label(row, f"{item['paid']} оплат  ·  {item['clicks']} кликов", 9,
                            color=C["teal"], bg=C["panel"]).pack(side="right")
            self._label(box, "Первые результаты не доказывают, что вариант лучше:\nсопоставляй расходы, объём трафика и аудиторию.", 9,
                        color=C["muted"], bg=C["panel"], justify="left").pack(anchor="w", pady=(11, 0))

    def _analytics_edit(self):
        id_ = self._chosen(self.analytic_table)
        if not id_: return
        p = by_id(self.state_data["posts"], id_)
        target = by_id(self.state_data["targets"], p["target_id"]) or {}
        def finish(data):
            if any(data[x] < 0 for x in ("views", "clicks", "paid", "actual_cost_uah")):
                messagebox.showerror("Метрики", "Метрики не могут быть отрицательными.", parent=self)
                return False
            if data["clicks"] > data["views"] and data["views"]:
                if not messagebox.askyesno("Проверка данных", "Кликов больше, чем просмотров. Это действительно разные методы учёта и числа правильные?", parent=self):
                    return False
            p.update(data)
            self._log("info", f"Обновлены метрики размещения «{target.get('name', '')}»")
        self._form("Метрики · " + target.get("name", ""), [
            ("views", "Просмотры / показы (вручную)", "int"),
            ("clicks", "Клики / переходы (вручную)", "int"),
            ("paid", "Подтверждённые оплаченные вступления (вручную)", "int"),
            ("actual_cost_uah", "Фактические расходы на рекламу, грн", "money"),
        ], p, finish, "Вноси только подтверждённые значения. Telegram Bot API не сообщает автоматические клики по обычным ссылкам.")

    def _analytics_preview(self):
        id_ = self._chosen(self.analytic_table)
        if not id_: return
        p = by_id(self.state_data["posts"], id_)
        camp = by_id(self.state_data["campaigns"], p["campaign_id"]) or {}
        self._show_preview("Отправленное объявление", p.get("rendered_text", "Данные старого сообщения отсутствуют"),
                           audit_creative(p.get("rendered_text", ""), camp))

    def _analytics_export(self):
        name = f"telegram_promo_stats_{datetime.now():%Y%m%d}.csv"
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                            initialfile=name, filetypes=[("CSV", "*.csv")])
        if not path: return
        try:
            export_posts_csv(self.state_data, Path(path))
        except OSError as exc:
            messagebox.showerror("Экспорт", str(exc), parent=self)
            return
        self._log("info", "Выгружен CSV с аналитикой")
        messagebox.showinfo("Готово", "CSV сохранён. Кодировка UTF-8 с BOM, разделитель ;", parent=self)

    # --- Journal ---
    def _journal(self):
        self._page_heading("Журнал событий", "Проверки, отказы, успешные публикации и ручные изменения.")
        row = self._toolbar(self.page_body, [("Экспорт журнала CSV", self._journal_export, "secondary")])
        table = self._new_table(self.page_body, [("when", "ВРЕМЯ", 160), ("level", "УРОВЕНЬ", 115),
                                                 ("text", "СОБЫТИЕ", 620)], height=16)
        for i, e in enumerate(reversed(self.state_data["activity"][-400:])):
            table.insert("", "end", tags=("odd" if i%2 else "even",),
                         values=(e.get("at"), e.get("level", "info").upper(), e.get("message", "")))

    def _journal_export(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                            initialfile="telegram_promo_activity.csv", filetypes=[("CSV", "*.csv")])
        if not path: return
        try:
            with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["at", "level", "message"], delimiter=";")
                writer.writeheader()
                writer.writerows(self.state_data["activity"])
        except OSError as exc:
            messagebox.showerror("Экспорт", str(exc), parent=self)
            return
        messagebox.showinfo("Экспорт", "Журнал сохранён.", parent=self)

    # --- Settings ---
    def _settings(self):
        self._page_heading("Настройки и подключение", "Токен бота хранится только в памяти запущенного приложения.")
        account = self._panel(self.page_body)
        account.pack(fill="x", padx=13, pady=(0, 16))
        self._label(account, "Telegram Bot API", 15, True, bg=C["panel"]).pack(anchor="w", pady=(0, 8))
        self._label(account, "Используй бота @BotFather. Добавь его администратором разрешённых площадок с правами публикации.",
                    10, color=C["muted"], bg=C["panel"], wraplength=910, justify="left").pack(anchor="w")
        entry_line = tk.Frame(account, bg=C["panel"])
        entry_line.pack(fill="x", pady=(17, 11))
        self.token_entry = tk.Entry(entry_line, textvariable=self.token, show="●", font=(FONT, 11),
                                    relief="flat", bg=C["field"], fg=C["text"], insertbackground=C["text"],
                                    highlightthickness=1, highlightbackground=C["border"])
        self.token_entry.pack(side="left", fill="x", expand=True, ipady=11, ipadx=11, padx=(0, 10))
        self._button(entry_line, "Проверить соединение", self._bot_check, "success").pack(side="right")
        self._label(account, "Токен НЕ сохраняется в state.json, CSV или журнале. Никому не отправляй его в чат.",
                    9, color=C["teal"], bg=C["panel"]).pack(anchor="w")
        controls = self._panel(self.page_body)
        controls.pack(fill="x", padx=13, pady=(0, 16))
        self._label(controls, "Режим публикаций", 15, True, bg=C["panel"]).pack(anchor="w", pady=(0, 14))
        self._setting_toggle(controls, "Тестовый режим", "Ничего не отправлять в Telegram. Предпросмотры и проверки без изменений очереди.",
                             self.dry_run, self._switch_dry)
        self.auto_var = tk.BooleanVar(value=self.auto_schedule)
        self._setting_toggle(controls, "Автоматическая отправка по расписанию", "Только заранее согласованные публикации, пока приложение открыто. После перезапуска отключена.",
                             self.auto_var, self._switch_auto)
        hint = self._panel(self.page_body)
        hint.pack(fill="x", padx=13, pady=(0, 16))
        self._label(hint, "Безопасность и ограничения", 15, True, bg=C["panel"]).pack(anchor="w", pady=(0, 12))
        for item in [
            "• Нет массовых личных сообщений и автоматического вступления в чужие группы.",
            "• Telegram 429 автоматически останавливает публикации. Нет обхода лимитов.",
            "• Бот должен иметь реальные права, а согласие владельца площадки сохраняется вручную.",
            "• Перед публикацией проверяются цена, период подписки, реальное описание и ссылка.",
            "• Данные сохраняются локально в домашней папке текущего пользователя.",
        ]:
            self._label(hint, item, 10, color=C["muted"], bg=C["panel"],
                        wraplength=890, justify="left").pack(anchor="w", pady=4)
        info = self._panel(self.page_body)
        info.pack(fill="x", padx=13, pady=(0, 18))
        self._label(info, "Где хранятся данные", 12, True, bg=C["panel"]).pack(anchor="w")
        self._label(info, str(STATE_FILE), 10, color=C["muted"], bg=C["panel"]).pack(anchor="w", pady=(8, 11))
        self._button(info, "Открыть папку данных", self._open_data_dir, "outline").pack(anchor="w")

    def _setting_toggle(self, parent, title, description, variable, command):
        row = tk.Frame(parent, bg=C["panel"])
        row.pack(fill="x", pady=8)
        texts = tk.Frame(row, bg=C["panel"])
        texts.pack(side="left", fill="x", expand=True)
        self._label(texts, title, 11, True, bg=C["panel"]).pack(anchor="w")
        self._label(texts, description, 9, color=C["muted"], bg=C["panel"],
                    wraplength=700, justify="left").pack(anchor="w", pady=(3, 0))
        tk.Checkbutton(row, variable=variable, command=command, text="", bg=C["panel"],
                       activebackground=C["panel"], selectcolor=C["panel2"],
                       highlightthickness=0, fg=C["text"]).pack(side="right", padx=12)

    def _open_data_dir(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        import os, subprocess, sys
        try:
            if sys.platform == "win32": os.startfile(str(APP_DIR))
            elif sys.platform == "darwin": subprocess.Popen(["open", str(APP_DIR)])
            else: subprocess.Popen(["xdg-open", str(APP_DIR)])
        except Exception as exc:
            messagebox.showinfo("Папка", str(APP_DIR), parent=self)

    def _switch_dry(self):
        if self.auto_schedule and self.dry_run.get():
            self.auto_schedule = False
            self._log("info", "Автоматическая отправка отключена из-за включения тестового режима")
        if not self.dry_run.get():
            if not messagebox.askyesno("Реальная отправка", "Включить возможность реальных публикаций?\n\nОтправка возможна только после отдельных команд/согласованного расписания. Telegram не гарантирует отсутствие ограничений.", parent=self):
                self.dry_run.set(True)
        self._save()
        self._refresh()

    def _switch_auto(self):
        if self.auto_schedule:
            self.auto_schedule = False
            self._log("info", "Автоотправка по расписанию отключена")
            self._refresh()
            return
        if self.dry_run.get():
            messagebox.showwarning("Демо-режим", "Для автоотправки сначала выключи тестовый режим вручную.", parent=self)
            self._refresh()
            return
        if not self.token.get().strip():
            messagebox.showwarning("Токен", "Сначала введи токен Telegram-бота.", parent=self)
            self._refresh()
            return
        if not messagebox.askyesno("Включить автоотправку", "Я подтверждаю, что каждое размещение согласовано с администрацией площадки, а платный канал содержит обещанный контент.\n\nВключить отправку ПО УЖЕ СОЗДАННОМУ РАСПИСАНИЮ, пока программа открыта?", parent=self):
            self._refresh(); return
        self.auto_schedule = True
        self._log("info", "Автоотправка включена вручную (только на эту сессию)")
        self._refresh()

    # --- Network tasks via single guarded worker ---
    def _run_worker(self, job, *args):
        if self.busy:
            messagebox.showinfo("Занято", "Предыдущая операция Telegram ещё не завершена.", parent=self)
            return
        token = self.token.get().strip()
        if not token:
            messagebox.showerror("Токен", "Сначала введи токен бота в Настройках.", parent=self)
            return
        self.busy = True
        def work():
            try:
                client = BotClient(token)
                me = client.get_me()
                if job == "bot_check":
                    self.events.put(("ok", f"Подключён бот @{me.get('username', '?')}"))
                elif job == "target_check":
                    id_, chat_id = args
                    info = client.permission_check(chat_id, me["id"])
                    self.events.put(("ok", f"Площадка {info['chat_title']}: роль {info['role']}, тип {info['chat_type']}. Согласие на рекламу проверяется отдельно."))
                elif job == "send":
                    post_id, chat_id, text = args
                    client.permission_check(chat_id, me["id"])
                    result = client.post(chat_id, text)
                    self.events.put(("sent", post_id, result.get("message_id"), text))
                elif job == "link":
                    post_id, own_chat_id, price, target_name = args
                    link = client.create_subscription_link(own_chat_id, price, f"promo-{post_id[:8]}-{target_name}"[:32])
                    self.events.put(("link", post_id, link))
            except TelegramError as exc:
                self.events.put(("error", job, args[0] if args else None, str(exc), exc.retry_after))
            except Exception as exc:
                self.events.put(("error", job, args[0] if args else None, f"Ошибка обработки: {type(exc).__name__}", 0))
            finally:
                self.events.put(("done",))
        threading.Thread(target=work, daemon=True).start()

    def _bot_check(self):
        self._run_worker("bot_check")

    def _process_events(self):
        redraw = False
        try:
            while True:
                event = self.events.get_nowait()
                action = event[0]
                if action == "ok":
                    self._log("success", event[1]); redraw = True
                elif action == "sent":
                    _, post_id, message_id, text = event
                    p = by_id(self.state_data["posts"], post_id)
                    if p:
                        p["status"] = "sent"; p["message_id"] = str(message_id)
                        p["sent_at"] = now_str(); p["rendered_text"] = text
                        camp = by_id(self.state_data["campaigns"], p["campaign_id"]) or {}
                        p["price_at_send"] = int(camp.get("price", 0))
                        t = by_id(self.state_data["targets"], p["target_id"])
                        if t: t["last_sent_at"] = p["sent_at"]
                        self._log("success", f"Отправлено в «{t.get('name','?') if t else '?'}», message_id={message_id}")
                        redraw = True
                elif action == "link":
                    _, post_id, link = event
                    p = by_id(self.state_data["posts"], post_id)
                    if p:
                        p["source_link"] = link
                        self._log("success", f"Сформирована отдельная платная ссылка для размещения {post_id[:8]}")
                        redraw = True
                        messagebox.showinfo("Новая ссылка", "Ссылка создана и сохранена в размещении. Её можно посмотреть через предпросмотр объявления.", parent=self)
                elif action == "error":
                    _, job, post_id, reason, retry_after = event
                    self._log("error", f"Telegram ({job}): {reason}")
                    if job == "send":
                        p = by_id(self.state_data["posts"], post_id)
                        if p:
                            p["status"] = "blocked" if retry_after else "failed"
                            p["error"] = reason
                    if retry_after:
                        self.auto_schedule = False
                        self._log("error", "Автоотправка отключена после 429 Telegram; автоматических повторов нет")
                    redraw = True
                elif action == "done":
                    self.busy = False
                self._save()
        except queue.Empty:
            pass
        if redraw:
            self._refresh()
        self.after(250, self._process_events)

    def _schedule_tick(self):
        if self.auto_schedule and not self.dry_run.get() and not self.busy:
            if time.monotonic() - self.last_start >= max(3, int(self.state_data["settings"].get("pacing_sec", 3))):
                now = datetime.now()
                due = []
                for p in self.state_data["posts"]:
                    if p.get("status") != "planned": continue
                    try:
                        scheduled = parse_local_datetime(p.get("scheduled_at", ""))
                        if scheduled <= now:
                            if (now - scheduled).total_seconds() > 3600:
                                p["status"] = "blocked"
                                p["error"] = "Расписание просрочено более чем на 1 час. Перепланируй вручную."
                                self._log("error", p["error"])
                            else:
                                due.append(p)
                    except ValueError:
                        p["status"] = "blocked"; p["error"] = "Неверная дата"
                if due:
                    due.sort(key=lambda p: p["scheduled_at"])
                    self.last_start = time.monotonic()
                    self._send_post(due[0]["id"], manual=False)
        self.after(3000, self._schedule_tick)

    def _exit(self):
        if self.busy and not messagebox.askyesno("Операция выполняется", "Сетевая операция может ещё выполняться. Закрыть приложение?", parent=self):
            return
        self.auto_schedule = False
        self._save()
        self.destroy()


if __name__ == "__main__":
    PromoStudio().mainloop()
