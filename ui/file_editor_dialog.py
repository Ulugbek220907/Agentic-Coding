"""
A simple in-app text file editor dialog. Opened by double-clicking a file
in the project tree (see main_window.py). Keeps it deliberately simple --
plain text editing with Save/Discard, not a full IDE.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QPushButton, QLabel, QMessageBox
from PyQt6.QtGui import QFont, QCloseEvent

from ui.theme import ThemeColors


class FileEditorDialog(QDialog):
    def __init__(self, path: str, colors: ThemeColors, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.colors = colors
        self.setWindowTitle(f"Editing: {self.path.name}")
        self.resize(800, 600)

        self._original_text = ""
        self._load_error = None
        try:
            # encoding="utf-8" explicitly -- Windows' default text encoding
            # (cp1252) chokes on non-ASCII/emoji content, same class of bug
            # already fixed elsewhere in this app's file-writing tools.
            self._original_text = self.path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self._load_error = str(e)

        layout = QVBoxLayout(self)

        path_label = QLabel(str(self.path))
        path_label.setStyleSheet(f"color:{colors.text_dim}; font-size:11px;")
        layout.addWidget(path_label)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(self._original_text)
        self.editor.setFont(QFont("Consolas", 11))
        layout.addWidget(self.editor)

        if self._load_error:
            self.editor.setPlainText(f"Could not read this file: {self._load_error}")
            self.editor.setReadOnly(True)

        self.status_label = QLabel("No changes")
        self.status_label.setStyleSheet(f"color:{colors.text_faint}; font-size:11px;")
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.discard_btn = QPushButton("Discard")
        self.discard_btn.setObjectName("SecondaryButton")
        self.discard_btn.setEnabled(False)
        self.discard_btn.clicked.connect(self._discard)
        self.save_btn = QPushButton("Save")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        button_row.addWidget(self.discard_btn)
        button_row.addWidget(self.save_btn)
        layout.addLayout(button_row)

        self.editor.textChanged.connect(self._on_text_changed)

    def _is_dirty(self) -> bool:
        return self.editor.toPlainText() != self._original_text

    def _on_text_changed(self):
        dirty = self._is_dirty()
        self.save_btn.setEnabled(dirty and not self._load_error)
        self.discard_btn.setEnabled(dirty)
        self.status_label.setText("Unsaved changes" if dirty else "No changes")
        self.status_label.setStyleSheet(
            f"color:{self.colors.warning if dirty else self.colors.text_faint}; font-size:11px;"
        )

    def _save(self):
        try:
            self.path.write_text(self.editor.toPlainText(), encoding="utf-8")
        except Exception as e:
            QMessageBox.critical(self, "Save failed", f"Could not save '{self.path}':\n{e}")
            return
        self._original_text = self.editor.toPlainText()
        self.save_btn.setEnabled(False)
        self.discard_btn.setEnabled(False)
        self.status_label.setText("Saved")
        self.status_label.setStyleSheet(f"color:{self.colors.success}; font-size:11px;")

    def _discard(self):
        self.editor.setPlainText(self._original_text)
        # setPlainText triggers textChanged -> _on_text_changed, which will
        # correctly show "No changes" and disable both buttons since the
        # content now matches _original_text again.

    def closeEvent(self, event: QCloseEvent):
        if self._is_dirty():
            reply = QMessageBox.question(
                self, "Unsaved changes",
                f"'{self.path.name}' has unsaved changes. Discard them and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()