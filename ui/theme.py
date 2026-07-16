"""
Theme system: light (ElevenLabs-style white) and dark (black/grey).

IMPORTANT -- why there's a QPalette here, not just a stylesheet:
Some Qt widgets (combobox dropdown popups, checkboxes, tooltips, the
native Windows menu/list highlight) pull their colors from the OS-level
QPalette for parts a stylesheet doesn't fully override. If Windows is in
Dark Mode while this app is in Light theme (or vice versa), that palette
can default to the wrong contrast -- invisible white-on-white or
black-on-black text. build_palette() pins every palette role explicitly
so the app looks the same regardless of the OS theme. Pair it with
app.setStyle("Fusion") in main.py -- Fusion is the one built-in Qt style
that reliably respects a custom QPalette across all widgets, including
combobox popups (the native Windows style sometimes ignores palette
overrides for those).

Font note: "Google Sans" is a proprietary Google font and isn't
redistributable / rarely installed on a random Windows machine. The
stylesheet asks for it first and falls back to "Segoe UI" (Windows),
"SF Pro Display"/"Helvetica Neue" (Mac), then Arial -- so you get the same
bold, geometric sans look even if Google Sans itself isn't installed.
"""

from PyQt6.QtGui import QPalette, QColor

FONT_FAMILIES = '"Google Sans", "Segoe UI", "SF Pro Display", "Helvetica Neue", Arial, sans-serif'


class ThemeColors:
    """Color tokens used both by the QSS stylesheet below AND by the chat
    bubble HTML that main_window.py builds at runtime (message backgrounds,
    code block colors, etc.) -- keeping them in one place means the chat
    log actually matches the rest of the app instead of staying hardcoded
    to light-mode colors forever."""

    def __init__(self, dark: bool):
        self.dark = dark
        if dark:
            self.window_bg = "#121214"
            self.panel_bg = "#1a1a1d"
            self.border = "#2c2c30"
            self.text = "#e8e8ea"
            self.text_dim = "#9a9a9e"
            self.text_faint = "#6b6b6f"
            self.accent_bg = "#e8e8ea"
            self.accent_text = "#121214"
            self.accent_hover = "#cfcfd2"
            self.disabled_bg = "#2c2c30"
            self.disabled_text = "#5a5a5e"
            self.hover_bg = "#232326"
            self.user_bubble_bg = "#2c2c30"
            self.user_bubble_text = "#ffffff"
            self.assistant_bubble_bg = "#1e1e21"
            self.code_bg = "#0a0a0b"
            self.code_text = "#e8e8ea"
            self.inline_code_bg = "#2c2c30"
            self.success = "#5fd07a"
            self.warning = "#e0a446"
            self.error = "#e0605f"
            self.link = "#7fb0ff"
        else:
            self.window_bg = "#ffffff"
            self.panel_bg = "#ffffff"
            self.border = "#e5e5e7"
            self.text = "#0a0a0a"
            self.text_dim = "#6b6b6f"
            self.text_faint = "#9a9a9e"
            self.accent_bg = "#0a0a0a"
            self.accent_text = "#ffffff"
            self.accent_hover = "#262626"
            self.disabled_bg = "#d8d8db"
            self.disabled_text = "#9a9a9e"
            self.hover_bg = "#f5f5f6"
            self.user_bubble_bg = "#0a0a0a"
            self.user_bubble_text = "#ffffff"
            self.assistant_bubble_bg = "#f7f7f8"
            self.code_bg = "#0d0d0f"
            self.code_text = "#e8e8ea"
            self.inline_code_bg = "#eeeeef"
            self.success = "#0a7a3d"
            self.warning = "#b5720a"
            self.error = "#c22b2b"
            self.link = "#2a5db0"


def build_palette(dark: bool = False) -> QPalette:
    c = ThemeColors(dark)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(c.window_bg))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c.text))
    palette.setColor(QPalette.ColorRole.Base, QColor(c.panel_bg))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c.hover_bg))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c.panel_bg))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c.text))
    palette.setColor(QPalette.ColorRole.Text, QColor(c.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(c.panel_bg))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c.text))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(c.error))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(c.accent_bg))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(c.accent_text))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c.text_faint))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(c.disabled_text))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(c.disabled_text))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(c.disabled_text))
    return palette


# Kept for backwards compatibility with any existing call sites.
def build_light_palette() -> QPalette:
    return build_palette(dark=False)


def build_stylesheet(dark: bool = False) -> str:
    c = ThemeColors(dark)
    return f"""
* {{
    font-family: {FONT_FAMILIES};
}}

QMainWindow, QDialog {{
    background-color: {c.window_bg};
}}

QLabel {{
    color: {c.text};
    font-size: 13px;
}}

QLabel#HeaderTitle {{
    font-size: 18px;
    font-weight: 700;
    color: {c.text};
}}

QTreeView, QTableWidget, QListWidget {{
    background-color: {c.panel_bg};
    border: 1px solid {c.border};
    border-radius: 10px;
    color: {c.text};
    selection-background-color: {c.hover_bg};
    selection-color: {c.text};
    gridline-color: {c.border};
}}

QListWidget::item {{
    padding: 6px;
    border-radius: 6px;
}}

QListWidget::item:selected {{
    background-color: {c.hover_bg};
    color: {c.text};
}}

QHeaderView::section {{
    background-color: {c.hover_bg};
    color: {c.text_dim};
    border: none;
    border-bottom: 1px solid {c.border};
    padding: 6px;
    font-weight: 600;
    font-size: 12px;
}}

QTextEdit, QTextBrowser {{
    background-color: {c.panel_bg};
    border: 1px solid {c.border};
    border-radius: 12px;
    padding: 10px;
    color: {c.text};
    font-size: 13px;
}}

QLineEdit {{
    background-color: {c.panel_bg};
    border: 1px solid {c.border};
    border-radius: 18px;
    padding: 8px 16px;
    color: {c.text};
    font-size: 13px;
}}

QLineEdit:focus {{
    border: 1px solid {c.accent_bg};
}}

QPushButton {{
    background-color: {c.accent_bg};
    color: {c.accent_text};
    border: none;
    border-radius: 18px;
    padding: 8px 20px;
    font-weight: 600;
    font-size: 13px;
}}

QPushButton:hover {{
    background-color: {c.accent_hover};
}}

QPushButton:disabled {{
    background-color: {c.disabled_bg};
    color: {c.disabled_text};
}}

QPushButton#SecondaryButton {{
    background-color: {c.panel_bg};
    color: {c.text};
    border: 1px solid {c.border};
}}

QPushButton#SecondaryButton:hover {{
    background-color: {c.hover_bg};
}}

QPushButton#DangerButton {{
    background-color: {c.panel_bg};
    color: {c.error};
    border: 1px solid {c.error};
}}

QPushButton#DangerButton:hover {{
    background-color: {c.hover_bg};
}}

QComboBox {{
    background-color: {c.panel_bg};
    border: 1px solid {c.border};
    border-radius: 10px;
    padding: 6px 10px;
    color: {c.text};
}}

QComboBox:hover {{
    border: 1px solid {c.text_faint};
}}

QComboBox::drop-down {{
    border: none;
    width: 24px;
}}

/* The actual popup list that opens when you click a combobox -- the part
   that was showing invisible white-on-white (or black-on-black) text
   before. Explicit colors here + Fusion style + the QPalette above make
   it reliable regardless of OS theme. */
QComboBox QAbstractItemView {{
    background-color: {c.panel_bg};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 8px;
    selection-background-color: {c.hover_bg};
    selection-color: {c.text};
    outline: none;
    padding: 4px;
}}

QSpinBox {{
    background-color: {c.panel_bg};
    border: 1px solid {c.border};
    border-radius: 10px;
    padding: 6px 10px;
    color: {c.text};
}}

QCheckBox {{
    color: {c.text};
    font-size: 13px;
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {c.text_faint};
    border-radius: 4px;
    background-color: {c.panel_bg};
}}

QCheckBox::indicator:checked {{
    background-color: {c.accent_bg};
    border: 1px solid {c.accent_bg};
}}

QMenuBar {{
    background-color: {c.panel_bg};
    color: {c.text};
    border-bottom: 1px solid {c.border};
}}

QMenuBar::item {{
    color: {c.text};
    background: transparent;
}}

QMenuBar::item:selected {{
    background-color: {c.hover_bg};
    border-radius: 6px;
}}

QMenu {{
    background-color: {c.panel_bg};
    border: 1px solid {c.border};
    border-radius: 8px;
    color: {c.text};
    padding: 4px;
}}

QMenu::item {{
    color: {c.text};
    padding: 6px 12px;
}}

QMenu::item:selected {{
    background-color: {c.hover_bg};
    border-radius: 6px;
}}

QToolTip {{
    background-color: {c.panel_bg};
    color: {c.text};
    border: 1px solid {c.border};
    padding: 4px 8px;
    border-radius: 6px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
}}

QScrollBar::handle:vertical {{
    background: {c.border};
    border-radius: 5px;
    min-height: 30px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
"""


# Kept for backwards compatibility: the original module-level constant some
# code may still import directly. Prefer build_stylesheet(dark=...) going
# forward so the app can actually switch themes at runtime.
STYLESHEET = build_stylesheet(dark=False)