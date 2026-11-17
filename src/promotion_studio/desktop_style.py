"""Visual components shared by the desktop workspace."""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractItemView, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget



INK = "#e8e7ef"
ACCENT = "#ad97ff"
MUTED = "#a29eb0"

STYLE = """
QWidget { color: #e8e7ef; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog, QWizard { background: #19191f; }
QFrame#sidebar { background: #141419; border-right: 1px solid #2e2c37; }
QWidget#workspace, QWidget#dashboardContent { background: #19191f; }
QLabel#brand { font-size: 14px; font-weight: 700; }
QLabel#pageTitle { font-size: 25px; font-weight: 650; }
QLabel#sectionTitle { font-size: 14px; font-weight: 650; }
QLabel#muted, QLabel#sidebarCaption { color: #a29eb0; }
QLabel#metricValue { font-size: 23px; font-weight: 700; }
QFrame#card, QFrame#emptyCard { background: #202027; border: 1px solid #302e3a; border-radius: 8px; }
QLabel#connected { color: #78d6ac; font-weight: 600; }
QPushButton { min-height: 27px; padding: 3px 11px; background: #25252e; border: 1px solid #373442;
              border-radius: 6px; font-weight: 600; }
QPushButton:hover { background: #312b40; border-color: #5a4878; }
QPushButton:focus { border-color: #ad97ff; }
QPushButton:disabled { color: #898494; background: #23232a; border-color: #2f2d36; }
QPushButton[variant="primary"] { background: #7455dd; border-color: #8a6af2; color: white; }
QPushButton[variant="primary"]:hover { background: #8465ef; }
QPushButton[variant="danger"] { color: #ffc0bc; background: #39252b; border-color: #6c3944; }
QPushButton[variant="nav"] { text-align: left; background: transparent; border-color: transparent;
                            min-height: 31px; padding: 2px 9px; color: #beb9cb; }
QPushButton[variant="nav"]:hover { background: #23212d; color: #e7defc; }
QPushButton[variant="nav"]:checked { background: #312942; color: #e5d9ff; }
QPushButton[variant="nav"]:focus { border-color: #856beb; }
QTableWidget, QListWidget { background: #202027; alternate-background-color: #24242c; border: 1px solid #34313e;
                           border-radius: 7px; selection-background-color: #483763;
                           selection-color: #fff; outline: none; }
QTableWidget::item, QListWidget::item { padding: 4px 8px; }
QHeaderView::section { padding: 7px 8px; background: #2a2933; color: #c9c5d6;
                       font-weight: 600; border: none; border-bottom: 1px solid #393344; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QDateTimeEdit { min-height: 25px; padding: 4px 7px;
                   background: #202027; border: 1px solid #3a3745; border-radius: 5px;
                   selection-background-color: #483763; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #ad97ff; }
QComboBox QAbstractItemView { background: #202027; selection-background-color: #483763; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: #282531; }
QCalendarWidget QToolButton { color: #e8e7ef; background: #282531; padding: 5px; }
QCalendarWidget QAbstractItemView { background: #202027; alternate-background-color: #26232e;
                                  selection-background-color: #7455dd; selection-color: white; }
QCalendarWidget QMenu, QMenu { background: #25242c; }
QStatusBar { background: #17171d; color: #a29eb0; border-top: 1px solid #2e2c37; }
QToolTip { background: #312942; color: white; border: none; padding: 5px; }
QScrollArea { border: none; background: transparent; }
QTabWidget::pane { border: 0; border-top: 1px solid #3a3745; }
QTabBar::tab { margin-right: 6px; padding: 7px 11px; color: #a29eb0; background: #19191f; }
QTabBar::tab:selected { color: #e8e7ef; border-bottom: 2px solid #ad97ff; }
QTabBar::tab:hover { color: #e8e7ef; }
"""


def apply_app_theme(app):
    if app.property("studio_theme"):
        return
    app.setProperty("studio_theme", True)
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in {"Window": "#19191f", "WindowText": INK, "Base": "#202027", "AlternateBase": "#24242c",
                        "Text": INK, "PlaceholderText": MUTED, "Button": "#25252e", "ButtonText": INK,
                        "Highlight": "#7455dd", "HighlightedText": "#ffffff", "Link": ACCENT}.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    app.setPalette(palette)


def label(text, role=None, *, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setObjectName(role)
    widget.setWordWrap(wrap)
    return widget


def card(role="card", margin=18):
    frame = QFrame()
    frame.setObjectName(role)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(8)
    return frame, layout


def page(title, subtitle, *, actions=None):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(14)
    heading = QVBoxLayout()
    heading.setSpacing(3)
    heading.addWidget(label(title, "pageTitle"))
    heading.addWidget(label(subtitle, "muted", wrap=True))
    if actions is None:
        layout.addLayout(heading)
    else:
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addLayout(heading, 1)
        row.addLayout(actions)
        layout.addLayout(row)
    return widget, layout


def empty_state(title, body):
    frame, layout = card("emptyCard", 24)
    layout.addStretch(1)
    for text, role in ((title, "sectionTitle"), (body, "muted")):
        item = label(text, role, wrap=True)
        item.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(item)
    return frame, layout


def polish_table(table):
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(35)
    table.horizontalHeader().setMinimumHeight(33)


_ICON_PATHS = {
    "brand": "M2 8 16 2l-4 14-4-5-6-3Zm6 3 8-9",
    "home": "M2 2h5v5H2zM11 2h5v5h-5zM2 11h5v5H2zM11 11h5v5h-5z",
    "jobs": "M3 2h12v14H3zM6 6h6M6 9h6M6 12h4",
    "catalog": "M9 16s5-4.5 5-9A5 5 0 1 0 4 7c0 4.5 5 9 5 9zM11 7a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
    "templates": "M4 2h7l3 3v11H4zM11 2v4h3M6 9h6M6 12h5",
    "accounts": "M12 6a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM3 16v-1a5 5 0 0 1 5-4h2a5 5 0 0 1 5 4v1",
    "calendar": "M3 4h12v12H3zM3 8h12M6 2v4M12 2v4",
    "report": "M3 3v12h13M6 12V9m4 3V6m4 6V4",
    "settings": "M3 5h12M3 9h12M3 13h12M6 3v4m6 0v4m-4 0v4",
}


def icon(kind, color=ACCENT):
    data = _ICON_PATHS.get(kind, _ICON_PATHS["brand"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           f'<path fill="none" stroke="{color}" stroke-width="1.45" '
           f'stroke-linecap="round" stroke-linejoin="round" d="{data}"/></svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("ascii")))
    pixmap = QPixmap(36, 36)
    pixmap.fill(Qt.GlobalColor.transparent)
    with QPainter(pixmap) as painter:
        renderer.render(painter)
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)
