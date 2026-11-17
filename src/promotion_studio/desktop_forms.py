"""Shared operator confirmations and BotFather connection dialog."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QVBoxLayout)

from .desktop_style import label


def get_bot_token(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Подключить бота")
    dialog.resize(480, 230)
    form = QFormLayout(dialog)
    form.addRow(label("Бот может публиковать на площадках, где администратор разрешил размещение и дал ему права.",
                      "muted", wrap=True))
    guide = QLabel('<a href="https://t.me/BotFather">Открыть @BotFather</a>')
    guide.setOpenExternalLinks(True)
    form.addRow(guide)
    form.addRow(label("В BotFather отправьте /newbot. Для существующего бота откройте /mybots и выберите API Token.",
                      "muted", wrap=True))
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setPlaceholderText("Вставьте токен из BotFather")
    form.addRow("Токен бота", field)
    controls = QDialogButtonBox()
    submit = controls.addButton("Подключить", QDialogButtonBox.ButtonRole.AcceptRole)
    submit.setProperty("variant", "primary")
    cancel = controls.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
    submit.setEnabled(False)
    field.textChanged.connect(lambda text: submit.setEnabled(bool(text.strip())))
    submit.clicked.connect(dialog.accept)
    cancel.clicked.connect(dialog.reject)
    form.addRow(controls)
    return field.text().strip() if dialog.exec() == QDialog.DialogCode.Accepted else None


def confirm(parent, title, detail):
    if len(detail) < 260 and detail.count("\n") < 4:
        dialog = QMessageBox(QMessageBox.Icon.Question, title, detail,
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        return dialog.exec() == QMessageBox.StandardButton.Yes
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(630, 410)
    layout = QVBoxLayout(dialog)
    content = QPlainTextEdit(detail)
    content.setReadOnly(True)
    layout.addWidget(content, 1)
    buttons = QDialogButtonBox()
    accept = buttons.addButton("Подтвердить", QDialogButtonBox.ButtonRole.AcceptRole)
    cancel = buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
    accept.setProperty("variant", "primary")
    cancel.setDefault(True)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return dialog.exec() == QDialog.DialogCode.Accepted
