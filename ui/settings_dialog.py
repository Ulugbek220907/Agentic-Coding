from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QFormLayout, QLineEdit, QComboBox, QCheckBox, QSpinBox,
    QDialogButtonBox, QMessageBox, QHeaderView, QListWidget, QListWidgetItem,
    QLabel, QTextEdit, QFrame,
)
from PyQt6.QtCore import Qt

from core.config import ConfigManager, ModelConfig
from core.presets import PROVIDER_PRESETS
from core.providers import call_model, ProviderError


class PresetPickerDialog(QDialog):
    """Lets the user pick one of the known free-tier providers to quick-fill the Add Model form."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add from preset")
        self.resize(560, 520)
        self.chosen_preset = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Pick a provider to prefill its API details:"))

        self.list_widget = QListWidget()
        for name, preset in PROVIDER_PRESETS.items():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list_widget.addItem(item)
        self.list_widget.currentItemChanged.connect(self._show_notes)
        layout.addWidget(self.list_widget)

        self.notes_label = QLabel("")
        self.notes_label.setWordWrap(True)
        self.notes_label.setStyleSheet("color: #6b6b6f; font-size: 12px; font-weight: 400;")
        layout.addWidget(self.notes_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def _show_notes(self, current, _previous):
        if not current:
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        preset = PROVIDER_PRESETS[name]
        models = ", ".join(preset["example_models"])
        self.notes_label.setText(f"Example models: {models}\n\n{preset['notes']}")

    def accept(self):
        item = self.list_widget.currentItem()
        if item:
            self.chosen_preset = item.data(Qt.ItemDataRole.UserRole)
        super().accept()


class ModelEditDialog(QDialog):
    def __init__(self, parent=None, model: ModelConfig | None = None, preset_name: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Model")
        self.resize(620, 560)
        self.model = model

        layout = QFormLayout(self)

        preset = PROVIDER_PRESETS.get(preset_name) if preset_name else None

        self.name_edit = QLineEdit(model.name if model else (preset_name or ""))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["openai_compatible", "anthropic"])
        if model:
            self.provider_combo.setCurrentText(model.provider)
        elif preset:
            self.provider_combo.setCurrentText(preset["provider"])

        default_base_url = model.base_url if model else (preset["base_url"] if preset else "https://api.openai.com/v1")
        self.base_url_edit = QLineEdit(default_base_url)
        self.api_key_edit = QLineEdit(model.api_key if model else "")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)

        default_model_id = model.model_id if model else (preset["example_models"][0] if preset else "")
        self.model_id_edit = QLineEdit(default_model_id)

        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 999)
        self.priority_spin.setValue(model.priority if model else 0)
        self.enabled_check = QCheckBox()
        self.enabled_check.setChecked(model.enabled if model else True)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 600)
        self.timeout_spin.setValue(model.timeout_seconds if model else 60)

        self.rpm_spin = QSpinBox()
        self.rpm_spin.setRange(0, 10000)
        self.rpm_spin.setSpecialValueText("Unlimited")
        default_rpm = (model.rpm_limit if model else None) or (preset.get("default_rpm_limit") if preset else None) or 0
        self.rpm_spin.setValue(default_rpm)
        self.rpm_spin.setToolTip(
            "Requests per minute you want THIS APP to self-cap at for this "
            "model. 0 = unlimited. Pre-filled from the preset's known free-tier "
            "limit when available -- double check it matches your actual "
            "account tier (limits vary by region/account age/billing status)."
        )
        self.rpd_spin = QSpinBox()
        self.rpd_spin.setRange(0, 1000000)
        self.rpd_spin.setSpecialValueText("Unlimited")
        default_rpd = (model.rpd_limit if model else None) or (preset.get("default_rpd_limit") if preset else None) or 0
        self.rpd_spin.setValue(default_rpd)
        self.rpd_spin.setToolTip("Requests per day self-cap. 0 = unlimited. Pre-filled from the preset when known.")

        layout.addRow("Friendly name:", self.name_edit)
        layout.addRow("Provider type:", self.provider_combo)
        layout.addRow("Base URL:", self.base_url_edit)
        layout.addRow("API key:", self.api_key_edit)
        layout.addRow("Model ID:", self.model_id_edit)
        layout.addRow("Priority (0 = tried first):", self.priority_spin)
        layout.addRow("Timeout (seconds):", self.timeout_spin)
        layout.addRow("RPM limit (self-imposed):", self.rpm_spin)
        layout.addRow("RPD limit (self-imposed):", self.rpd_spin)
        layout.addRow("Enabled:", self.enabled_check)

        if preset:
            hint = QLabel(f"Example model IDs: {', '.join(preset['example_models'])}")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #6b6b6f; font-size: 11px; font-weight: 400;")
            layout.addRow("", hint)

        test_btn = QPushButton("Test connection")
        test_btn.setObjectName("SecondaryButton")
        test_btn.clicked.connect(self._test_connection)
        layout.addRow("", test_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _test_connection(self):
        test_model = self.get_model()
        if not test_model.api_key or not test_model.base_url or not test_model.model_id:
            QMessageBox.warning(self, "Test connection", "Fill in base URL, API key, and model ID first.")
            return
        try:
            response = call_model(
                test_model,
                [{"role": "user", "content": "Reply with exactly one word: pong"}],
                tools=[],
            )
            text = (response.get("content") or "").strip()
            QMessageBox.information(self, "Test connection", f"Success! Model replied: \"{text}\"")
        except ProviderError as e:
            QMessageBox.critical(self, "Test connection failed", str(e))
        except Exception as e:
            QMessageBox.critical(self, "Test connection failed", f"Unexpected error: {e}")

    def get_model(self) -> ModelConfig:
        return ModelConfig(
            name=self.name_edit.text().strip() or "Unnamed model",
            provider=self.provider_combo.currentText(),
            base_url=self.base_url_edit.text().strip(),
            api_key=self.api_key_edit.text().strip(),
            model_id=self.model_id_edit.text().strip(),
            priority=self.priority_spin.value(),
            enabled=self.enabled_check.isChecked(),
            timeout_seconds=self.timeout_spin.value(),
            rpm_limit=self.rpm_spin.value() or None,
            rpd_limit=self.rpd_spin.value() or None,
        )


class SettingsDialog(QDialog):
    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Model Settings")
        self.resize(880, 660)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Models"))

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Priority", "Name", "Provider", "Model ID", "Enabled"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        preset_btn = QPushButton("Add from preset")
        add_btn = QPushButton("Add custom model")
        add_btn.setObjectName("SecondaryButton")
        edit_btn = QPushButton("Edit selected")
        edit_btn.setObjectName("SecondaryButton")
        remove_btn = QPushButton("Remove selected")
        remove_btn.setObjectName("DangerButton")
        preset_btn.clicked.connect(self.add_from_preset)
        add_btn.clicked.connect(self.add_model)
        edit_btn.clicked.connect(self.edit_model)
        remove_btn.clicked.connect(self.remove_model)
        btn_row.addWidget(preset_btn)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(remove_btn)
        layout.addLayout(btn_row)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("color: #e5e5e7;")
        layout.addWidget(divider)

        layout.addWidget(QLabel("Confirmations"))

        self.auto_write_check = QCheckBox("Auto-confirm file writes (skip the Accept/Decline prompt in chat)")
        self.auto_write_check.setChecked(self.config.config.auto_confirm_writes)
        self.auto_write_check.toggled.connect(self._toggle_auto_write)
        layout.addWidget(self.auto_write_check)

        self.auto_command_check = QCheckBox("Auto-confirm shell commands (skip the Accept/Decline prompt in chat)")
        self.auto_command_check.setChecked(self.config.config.auto_confirm_commands)
        self.auto_command_check.toggled.connect(self._toggle_auto_command)
        layout.addWidget(self.auto_command_check)

        warn = QLabel(
            "Auto-confirming shell commands lets the agent run anything in this folder "
            "without asking first. Only turn this on for a folder under git version control."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#c22b2b; font-size:11px; font-weight:500;")
        layout.addWidget(warn)

        divider2 = QFrame()
        divider2.setFrameShape(QFrame.Shape.HLine)
        divider2.setStyleSheet("color: #e5e5e7;")
        layout.addWidget(divider2)

        layout.addWidget(QLabel("Agent step limit"))
        iter_row = QHBoxLayout()
        self.max_iter_spin = QSpinBox()
        self.max_iter_spin.setRange(0, 1000)
        self.max_iter_spin.setSpecialValueText("No limit (stall detection only)")
        self.max_iter_spin.setValue(self.config.config.max_agent_iterations)
        self.max_iter_spin.setToolTip(
            "Max tool-call steps per task. 0 = no fixed cap -- the agent runs "
            "until done, genuinely stuck (same call repeated, or too long "
            "with no real progress), or you hit Stop. Surgical edits "
            "(read_symbol/edit_symbol) do less per step than a full-file "
            "read/write did, so a low cap here can cut off legitimate work "
            "on larger tasks."
        )
        self.max_iter_spin.valueChanged.connect(self._set_max_iterations)
        iter_row.addWidget(self.max_iter_spin)
        iter_row.addStretch()
        layout.addLayout(iter_row)

        close_btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn.rejected.connect(self.accept)
        close_btn.accepted.connect(self.accept)
        layout.addWidget(close_btn)

        self.refresh_table()

    def _set_max_iterations(self, value: int):
        self.config.config.max_agent_iterations = value
        self.config.save()

    def _toggle_auto_write(self, checked: bool):
        self.config.config.auto_confirm_writes = checked
        self.config.save()

    def _toggle_auto_command(self, checked: bool):
        self.config.config.auto_confirm_commands = checked
        self.config.save()

    def refresh_table(self):
        self.table.setRowCount(0)
        for m in self.config.config.models:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(m.priority)))
            self.table.setItem(row, 1, QTableWidgetItem(m.name))
            self.table.setItem(row, 2, QTableWidgetItem(m.provider))
            self.table.setItem(row, 3, QTableWidgetItem(m.model_id))
            self.table.setItem(row, 4, QTableWidgetItem("yes" if m.enabled else "no"))

    def add_from_preset(self):
        picker = PresetPickerDialog(self)
        if picker.exec() and picker.chosen_preset:
            dlg = ModelEditDialog(self, preset_name=picker.chosen_preset)
            if dlg.exec():
                self.config.add_model(dlg.get_model())
                self.refresh_table()

    def add_model(self):
        dlg = ModelEditDialog(self)
        if dlg.exec():
            self.config.add_model(dlg.get_model())
            self.refresh_table()

    def _selected_index(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def edit_model(self):
        idx = self._selected_index()
        if idx is None:
            QMessageBox.information(self, "Edit model", "Select a model first.")
            return
        dlg = ModelEditDialog(self, self.config.config.models[idx])
        if dlg.exec():
            self.config.update_model(idx, dlg.get_model())
            self.refresh_table()

    def remove_model(self):
        idx = self._selected_index()
        if idx is None:
            QMessageBox.information(self, "Remove model", "Select a model first.")
            return
        self.config.remove_model(idx)
        self.refresh_table()