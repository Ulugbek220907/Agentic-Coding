from __future__ import annotations

import html
import threading
import uuid
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTreeView,
    QTextEdit, QTextBrowser, QLineEdit, QPushButton, QFileDialog,
    QMessageBox, QLabel, QMenuBar, QComboBox, QSizePolicy,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QUrl
from PyQt6.QtGui import QAction, QFileSystemModel

from core.config import ConfigManager
from core.failover import ModelRouter
from core.agent import run_agent_task
from core.memory import load_memory, append_memory
from ui.settings_dialog import SettingsDialog

AUTO_MODEL_LABEL = "Auto (priority order)"

IDLE_BADGE_STYLE = (
    "background-color:#f0f0f2;color:#1a1a1a;border-radius:10px;"
    "padding:4px 12px;font-weight:600;font-size:12px;"
)
ACTIVE_BADGE_STYLE = (
    "background-color:#e6f9ed;color:#0a7a3d;border-radius:10px;"
    "padding:4px 12px;font-weight:600;font-size:12px;"
)


class ConfirmationBridge(QObject):
    """
    Lives on the main GUI thread. The agent worker (running on a background
    QThread) emits these signals to ask "can I write this file / run this
    command?". Qt connections are automatically queued when sender and
    receiver live on different threads, so the connected handler below
    always actually runs on the main thread. Unlike a QMessageBox, the
    handler here doesn't block anything -- it posts an Accept/Decline
    prompt into the chat log and returns immediately. The WORKER thread is
    the one that blocks, waiting on a threading.Event until the user clicks
    a button in the chat.
    """
    request_write = pyqtSignal(dict)
    request_command = pyqtSignal(dict)


class AgentWorker(QObject):
    agent_event = pyqtSignal(dict)
    finished = pyqtSignal()

    def __init__(self, router, project_root, instruction, max_iterations,
                 confirm_write, confirm_command, project_memory):
        super().__init__()
        self.router = router
        self.project_root = project_root
        self.instruction = instruction
        self.max_iterations = max_iterations
        self.confirm_write = confirm_write
        self.confirm_command = confirm_command
        self.project_memory = project_memory
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True

    def run(self):
        try:
            for ev in run_agent_task(
                self.router, self.project_root, self.instruction,
                max_iterations=self.max_iterations,
                confirm_write=self.confirm_write,
                confirm_command=self.confirm_command,
                stop_check=lambda: self._stop_requested,
                project_memory=self.project_memory,
            ):
                self.agent_event.emit(ev)
        except Exception as e:
            self.agent_event.emit({"type": "error", "text": f"Unexpected error: {e}"})
        self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Agent Desktop")
        self.resize(1280, 840)

        self.config = ConfigManager()
        self.thread: QThread | None = None
        self.worker: AgentWorker | None = None
        self._pending_confirmation: dict | None = None
        self.project_memory: list[str] = []
        self._last_instruction: str = ""
        self._last_assistant_text: str | None = None

        self.confirm_bridge = ConfirmationBridge()
        self.confirm_bridge.request_write.connect(self._handle_confirm_write_request)
        self.confirm_bridge.request_command.connect(self._handle_confirm_command_request)

        self._build_menu()
        self._build_ui()
        self._refresh_model_combo()

        if self.config.config.project_folder and Path(self.config.config.project_folder).is_dir():
            self._set_project_folder(self.config.config.project_folder)
        else:
            self._prompt_for_project_folder()

    # ---------- UI construction ----------

    def _build_menu(self):
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")

        open_action = QAction("Open project folder...", self)
        open_action.triggered.connect(self._prompt_for_project_folder)
        file_menu.addAction(open_action)

        settings_action = QAction("Model settings...", self)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

    def _build_ui(self):
        container = QWidget()
        outer_layout = QVBoxLayout(container)
        outer_layout.setContentsMargins(16, 12, 16, 16)
        outer_layout.setSpacing(10)

        # --- Header row: title + model picker + active-model badge ---
        header_row = QHBoxLayout()
        title = QLabel("AI Agent Desktop")
        title.setObjectName("HeaderTitle")
        header_row.addWidget(title)
        header_row.addStretch()

        model_label = QLabel("Model:")
        model_label.setStyleSheet("color:#6b6b6f; font-weight:600; font-size:12px;")
        header_row.addWidget(model_label)

        self.model_combo = QComboBox()
        self.model_combo.setToolTip(
            "Pick which model to try first. If it fails or times out, the app\n"
            "still automatically falls back to your other configured models."
        )
        self.model_combo.setMinimumWidth(200)
        self.model_combo.setFixedHeight(30)
        header_row.addWidget(self.model_combo)

        # Fixed height + a capped (not expanding) size policy: this badge
        # must never grow to fill available space, regardless of DPI or
        # style quirks.
        self.status_badge = QLabel("No model used yet")
        self.status_badge.setStyleSheet(IDLE_BADGE_STYLE)
        self.status_badge.setFixedHeight(28)
        self.status_badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        header_row.addWidget(self.status_badge)
        outer_layout.addLayout(header_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: file tree
        self.fs_model = QFileSystemModel()
        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        for col in (1, 2, 3):
            self.tree.hideColumn(col)
        splitter.addWidget(self.tree)

        # Right: chat/log + input
        right = QWidget()
        right_layout = QVBoxLayout(right)

        self.project_label = QLabel("No project folder selected.")
        self.project_label.setStyleSheet("color: #6b6b6f; font-weight: 500; font-size: 12px;")
        right_layout.addWidget(self.project_label)

        self.log = QTextBrowser()
        self.log.setReadOnly(True)
        # Enable clickable links (for the inline Accept/Decline buttons) but
        # stop Qt from trying to "open" them as if they were real URLs --
        # we intercept clicks ourselves in _on_log_anchor_clicked.
        self.log.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.log.setOpenLinks(False)
        self.log.anchorClicked.connect(self._on_log_anchor_clicked)
        right_layout.addWidget(self.log)

        input_row = QHBoxLayout()
        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("Describe what you want the agent to do to this project...")
        self.input_box.returnPressed.connect(self._on_send)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("DangerButton")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        input_row.addWidget(self.input_box)
        input_row.addWidget(self.send_btn)
        input_row.addWidget(self.stop_btn)
        right_layout.addLayout(input_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        outer_layout.addWidget(splitter)
        self.setCentralWidget(container)

    # ---------- Model picker ----------

    def _refresh_model_combo(self):
        current_text = self.model_combo.currentText() if self.model_combo.count() else AUTO_MODEL_LABEL
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem(AUTO_MODEL_LABEL)
        for m in self.config.sorted_models():
            self.model_combo.addItem(m.name)
        idx = self.model_combo.findText(current_text)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_combo.blockSignals(False)

    # ---------- Project folder handling ----------

    def _prompt_for_project_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select project folder")
        if folder:
            self._set_project_folder(folder)
            self.config.set_project_folder(folder)

    def _set_project_folder(self, folder: str):
        self.project_folder = folder
        self.project_memory = load_memory(folder)
        self.fs_model.setRootPath(folder)
        self.tree.setRootIndex(self.fs_model.index(folder))
        self.project_label.setText(f"Project: {folder}")

    # ---------- Settings ----------

    def _open_settings(self):
        dlg = SettingsDialog(self.config, self)
        dlg.exec()
        self._refresh_model_combo()

    # ---------- Agent run ----------

    def _append_log(self, html_fragment: str):
        self.log.append(html_fragment)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _remember(self, summary: str | None):
        """Distill this turn into one line of project memory (see core/memory.py)."""
        if not summary or not getattr(self, "project_folder", None):
            return
        entry = f'User asked: "{self._last_instruction}" -> {summary}'
        self.project_memory = append_memory(self.project_folder, entry)

    def _confirm_write(self, path: str, content: str) -> bool:
        """Called from the WORKER thread -- never touch Qt widgets directly here."""
        if self.config.config.auto_confirm_writes:
            return True
        event = threading.Event()
        result: dict = {}
        self.confirm_bridge.request_write.emit({"path": path, "content": content, "event": event, "result": result})
        event.wait()
        return result.get("ok", False)

    def _confirm_command(self, command: str) -> bool:
        """Called from the WORKER thread -- see _confirm_write for why this hops threads."""
        if self.config.config.auto_confirm_commands:
            return True
        event = threading.Event()
        result: dict = {}
        self.confirm_bridge.request_command.emit({"command": command, "event": event, "result": result})
        event.wait()
        return result.get("ok", False)

    def _post_confirmation_prompt(self, header: str, body_preview: str, event: threading.Event, result: dict):
        """
        Runs on the MAIN thread. Posts an Accept/Decline prompt into the chat
        log instead of a popup dialog -- it scrolls with the rest of the
        conversation, so a long script never gets cut off or hidden off-screen.
        """
        token = uuid.uuid4().hex[:8]
        self._pending_confirmation = {"token": token, "event": event, "result": result}

        max_preview = 4000
        preview = body_preview if len(body_preview) < max_preview else body_preview[:max_preview] + "\n... (truncated)"
        escaped_preview = html.escape(preview)

        self._append_log(
            f"<div style='margin:8px 0;padding:10px 12px;border:1px solid #e5e5e7;"
            f"border-radius:10px;background:#fafafa;'>"
            f"<b>{header}</b>"
            f"<pre style='white-space:pre-wrap;word-wrap:break-word;font-family:Consolas,monospace;"
            f"font-size:12px;color:#333;margin:8px 0;'>{escaped_preview}</pre>"
            f"<a href='agentaction://accept/{token}' "
            f"style='background:#0a0a0a;color:#ffffff;padding:5px 14px;border-radius:12px;"
            f"text-decoration:none;font-weight:600;margin-right:8px;'>Accept</a>"
            f"<a href='agentaction://decline/{token}' "
            f"style='background:#ffffff;color:#c22b2b;border:1px solid #f0c9c9;padding:5px 14px;"
            f"border-radius:12px;text-decoration:none;font-weight:600;'>Decline</a>"
            f"</div>"
        )

    def _handle_confirm_write_request(self, payload: dict):
        self._post_confirmation_prompt(
            f"Confirm file write: {payload['path']}",
            payload["content"],
            payload["event"],
            payload["result"],
        )

    def _handle_confirm_command_request(self, payload: dict):
        self._post_confirmation_prompt(
            "Confirm shell command",
            payload["command"],
            payload["event"],
            payload["result"],
        )

    def _on_log_anchor_clicked(self, url: QUrl):
        if url.scheme() != "agentaction":
            return
        action = url.host()
        token = url.path().lstrip("/")

        pending = self._pending_confirmation
        if not pending or pending["token"] != token:
            return  # stale click on an already-resolved (or unrelated) prompt

        ok = action == "accept"
        pending["result"]["ok"] = ok
        pending["event"].set()
        self._pending_confirmation = None
        self._append_log(f"<i style='color:#6b6b6f;'>You {'accepted' if ok else 'declined'} the request above.</i>")

    def _on_send(self):
        instruction = self.input_box.text().strip()
        if not instruction:
            return
        if not getattr(self, "project_folder", None):
            QMessageBox.warning(self, "No project folder", "Select a project folder first.")
            return
        if not Path(self.project_folder).is_dir():
            QMessageBox.warning(
                self, "Project folder missing",
                f"'{self.project_folder}' no longer exists on disk. Please choose another folder.",
            )
            self._prompt_for_project_folder()
            return

        models = self.config.sorted_models()
        if not models:
            QMessageBox.warning(self, "No models configured", "Add at least one model in Model settings.")
            return

        # If the user picked a specific model instead of "Auto", move it to
        # the front of the list -- it's tried first, but the rest are still
        # there as automatic fallbacks if it fails.
        preferred_name = self.model_combo.currentText()
        if preferred_name and preferred_name != AUTO_MODEL_LABEL:
            preferred = [m for m in models if m.name == preferred_name]
            rest = [m for m in models if m.name != preferred_name]
            models = preferred + rest

        self._last_instruction = instruction
        self._last_assistant_text = None
        self.input_box.clear()
        self._append_log(
            f"<div style='margin:8px 0;'>"
            f"<span style='background:#0a0a0a;color:#ffffff;border-radius:12px;padding:6px 12px;'>{html.escape(instruction)}</span>"
            f"</div>"
        )
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        router = ModelRouter(models)

        self.thread = QThread()
        self.worker = AgentWorker(
            router, self.project_folder, instruction,
            self.config.config.max_agent_iterations,
            self._confirm_write, self._confirm_command,
            self.project_memory,
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.agent_event.connect(self._on_agent_event)
        self.worker.finished.connect(self._on_agent_finished)
        self.thread.start()

    def _on_stop(self):
        if self.worker:
            self.worker.request_stop()
        # If the agent is stuck waiting on an Accept/Decline prompt, treat
        # Stop as an implicit decline so the worker thread can wake up and
        # notice the stop request instead of hanging forever.
        if self._pending_confirmation:
            self._pending_confirmation["result"]["ok"] = False
            self._pending_confirmation["event"].set()
            self._pending_confirmation = None
            self._append_log("<i style='color:#6b6b6f;'>(auto-declined because you hit Stop)</i>")
        self.stop_btn.setEnabled(False)

    def _on_agent_event(self, ev: dict):
        t = ev["type"]
        if t == "status":
            self._append_log(f"<span style='color:#9a9a9e;font-size:12px;'>{html.escape(ev['text'])}</span>")
        elif t == "model_switch":
            self._append_log(f"<span style='color:#b5720a;'>&#8635; Switching to backup model: {html.escape(ev['model'])}</span>")
        elif t == "model_failed":
            self._append_log(f"<span style='color:#c22b2b;'>Model failed ({html.escape(ev['model'])}): {html.escape(ev['error'])}</span>")
        elif t == "model_success":
            self._set_active_model(ev["model"])
        elif t == "assistant_text":
            self._last_assistant_text = ev["text"]
            self._append_log(
                f"<div style='margin:8px 0;background:#f7f7f8;border-radius:12px;padding:8px 12px;'>"
                f"<b>{html.escape(ev.get('model','Agent'))}:</b> {html.escape(ev['text'])}</div>"
            )
        elif t == "tool_call":
            self._append_log(
                f"<span style='color:#6b6b6f;font-family:Consolas,monospace;font-size:12px;'>"
                f"&rarr; {html.escape(ev['name'])}({html.escape(str(ev['arguments']))})</span>"
            )
        elif t == "tool_result":
            result_preview = str(ev["result"])[:500]
            self._append_log(
                f"<span style='color:#9a9a9e;font-family:Consolas,monospace;font-size:12px;'>"
                f"&nbsp;&nbsp;{html.escape(result_preview)}</span>"
            )
        elif t == "stopped":
            self._append_log(f"<b style='color:#b5720a;'>Stopped:</b> {html.escape(ev['text'])}")
            self._remember("(user stopped this task before it finished)")
        elif t == "done":
            summary = ev.get("summary")
            if summary:
                # A real task_complete summary -- show it and remember it.
                self._append_log(f"<b style='color:#0a7a3d;'>Done:</b> {html.escape(summary)}")
                self._remember(summary)
            else:
                # Plain conversational reply -- its text was already shown
                # as an assistant_text bubble above, so don't print it again.
                # Still worth a one-line memory entry for continuity.
                self._remember(self._last_assistant_text)
        elif t == "error":
            self._append_log(f"<b style='color:#c22b2b;'>Error:</b> {html.escape(ev['text'])}")
            self._remember(f"(task did not complete: {ev['text'][:200]})")

    def _set_active_model(self, model_name: str):
        self.status_badge.setText(f"Active model: {model_name}")
        self.status_badge.setStyleSheet(ACTIVE_BADGE_STYLE)

    def _on_agent_finished(self):
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if self.thread:
            self.thread.quit()
            self.thread.wait()
