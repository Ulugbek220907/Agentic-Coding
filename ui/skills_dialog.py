from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QTextEdit, QMessageBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtCore import QUrl

from core.skills import SkillManager, SKILLS_DIR


class SkillsDialog(QDialog):
    """
    Lists every folder under ~/.ai_agent_desktop/skills/, shows what the
    static risk scan found in each, and requires an explicit Approve click
    before a new/changed skill becomes callable by the AI. See the big
    docstring at the top of core/skills.py for the full rules this enforces.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Skills")
        self.resize(720, 480)
        self.manager = SkillManager()
        self.skills = []

        layout = QHBoxLayout(self)

        left = QVBoxLayout()
        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_selection_changed)
        left.addWidget(self.list_widget)

        open_folder_btn = QPushButton("Open skills folder")
        open_folder_btn.setObjectName("SecondaryButton")
        open_folder_btn.setToolTip(
            f"Opens {SKILLS_DIR} -- drop a new skill folder (skill.json + "
            f"SKILL.md + your .py file(s)) in here, then click Rescan."
        )
        open_folder_btn.clicked.connect(self._open_skills_folder)
        left.addWidget(open_folder_btn)

        rescan_btn = QPushButton("Rescan")
        rescan_btn.setObjectName("SecondaryButton")
        rescan_btn.clicked.connect(self._reload)
        left.addWidget(rescan_btn)

        layout.addLayout(left, 1)

        right = QVBoxLayout()
        self.name_label = QLabel("Select a skill")
        self.name_label.setStyleSheet("font-weight:700; font-size:14px;")
        right.addWidget(self.name_label)

        self.status_label = QLabel("")
        right.addWidget(self.status_label)

        right.addWidget(QLabel("SKILL.md:"))
        self.doc_view = QTextEdit()
        self.doc_view.setReadOnly(True)
        right.addWidget(self.doc_view)

        right.addWidget(QLabel("Risk scan findings (review before approving):"))
        self.risk_view = QTextEdit()
        self.risk_view.setReadOnly(True)
        self.risk_view.setMaximumHeight(100)
        right.addWidget(self.risk_view)

        button_row = QHBoxLayout()
        self.approve_btn = QPushButton("Approve && Enable")
        self.approve_btn.clicked.connect(self._approve_current)
        self.disable_btn = QPushButton("Disable")
        self.disable_btn.setObjectName("SecondaryButton")
        self.disable_btn.clicked.connect(self._disable_current)
        button_row.addWidget(self.approve_btn)
        button_row.addWidget(self.disable_btn)
        right.addLayout(button_row)

        layout.addLayout(right, 2)

        self._reload()

    def _open_skills_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(SKILLS_DIR)))

    def _reload(self):
        self.skills = self.manager.discover()
        self.list_widget.clear()
        for skill in self.skills:
            if skill.load_error:
                status = "⚠ error"
            elif skill.needs_trust_review:
                status = "🔒 needs approval"
            elif skill.enabled:
                status = "✓ enabled"
            else:
                status = "○ disabled"
            item = QListWidgetItem(f"{skill.display_name}  [{status}]")
            self.list_widget.addItem(item)
        if self.skills:
            self.list_widget.setCurrentRow(0)

    def _on_selection_changed(self, row: int):
        if row < 0 or row >= len(self.skills):
            self.name_label.setText("Select a skill")
            self.status_label.setText("")
            self.doc_view.setPlainText("")
            self.risk_view.setPlainText("")
            return
        skill = self.skills[row]
        self.name_label.setText(skill.display_name)

        if skill.load_error:
            self.status_label.setText(f"⚠ {skill.load_error}")
        elif skill.needs_trust_review:
            self.status_label.setText(
                "🔒 New or changed since last approval -- review the risk findings below, "
                "then click Approve & Enable to allow the AI to call it."
            )
        elif skill.enabled:
            self.status_label.setText(f"✓ Enabled. Functions: {', '.join(f.name for f in skill.functions)}")
        else:
            self.status_label.setText("○ Disabled (approved previously, but turned off).")

        self.doc_view.setPlainText(skill.doc or "(no SKILL.md content)")
        if skill.risk_findings:
            self.risk_view.setPlainText("\n".join(f"⚠ {f}" for f in skill.risk_findings))
        else:
            self.risk_view.setPlainText("No risky patterns matched by the static scanner. "
                                          "This is not a guarantee of safety -- only enable "
                                          "skills from sources you trust.")

    def _approve_current(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.skills):
            return
        skill = self.skills[row]
        if skill.load_error:
            QMessageBox.warning(self, "Cannot enable", skill.load_error)
            return
        reply = QMessageBox.question(
            self, "Confirm skill approval",
            f"Enable '{skill.display_name}'?\n\n"
            f"It will run in an isolated subprocess with no access to your "
            f"AI provider API keys, but it CAN read/write files within "
            f"whatever project folder is open when it's called, and its "
            f"code is arbitrary Python from a source you supplied. "
            f"Risk scan found:\n" + ("\n".join(f"- {f}" for f in skill.risk_findings) or "(nothing flagged)"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.manager.approve_skill(skill)
        self.manager.set_enabled(skill.name, True)
        self._reload()

    def _disable_current(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.skills):
            return
        skill = self.skills[row]
        self.manager.set_enabled(skill.name, False)
        self._reload()