"""
Clean white theme, loosely modeled on ElevenLabs' UI: lots of white space,
near-black text, pill-shaped buttons, soft gray borders, a black primary
action button instead of a "blue software" accent.

IMPORTANT -- why there's a QPalette here, not just a stylesheet:
Some Qt widgets (combobox dropdown popups, checkboxes, tooltips, the
native Windows menu/list highlight) pull their colors from the OS-level
QPalette for parts a stylesheet doesn't fully override. If Windows is in
Dark Mode, that palette defaults to light text -- which is invisible
against the white backgrounds this stylesheet paints everywhere else.
build_light_palette() pins every palette role to a light-theme color so
the app looks the same regardless of the OS theme. Pair it with
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


def build_light_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#0a0a0a"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f7f8"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#0a0a0a"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#0a0a0a"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#0a0a0a"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#c22b2b"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#0a0a0a"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#9a9a9e"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#b5b5b9"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#b5b5b9"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#b5b5b9"))
    return palette


STYLESHEET = f"""
* {{
    font-family: {FONT_FAMILIES};
}}

QMainWindow, QDialog {{
    background-color: #ffffff;
}}

QLabel {{
    color: #0a0a0a;
    font-size: 13px;
}}

QLabel#HeaderTitle {{
    font-size: 18px;
    font-weight: 700;
    color: #0a0a0a;
}}

QTreeView, QTableWidget, QListWidget {{
    background-color: #ffffff;
    border: 1px solid #e5e5e7;
    border-radius: 10px;
    color: #0a0a0a;
    selection-background-color: #f0f0f2;
    selection-color: #0a0a0a;
    gridline-color: #eeeeee;
}}

QListWidget::item {{
    padding: 6px;
    border-radius: 6px;
}}

QListWidget::item:selected {{
    background-color: #f0f0f2;
    color: #0a0a0a;
}}

QHeaderView::section {{
    background-color: #fafafa;
    color: #6b6b6f;
    border: none;
    border-bottom: 1px solid #e5e5e7;
    padding: 6px;
    font-weight: 600;
    font-size: 12px;
}}

QTextEdit {{
    background-color: #ffffff;
    border: 1px solid #e5e5e7;
    border-radius: 12px;
    padding: 10px;
    color: #0a0a0a;
    font-size: 13px;
}}

QLineEdit {{
    background-color: #ffffff;
    border: 1px solid #dcdce0;
    border-radius: 18px;
    padding: 8px 16px;
    color: #0a0a0a;
    font-size: 13px;
}}

QLineEdit:focus {{
    border: 1px solid #0a0a0a;
}}

QPushButton {{
    background-color: #0a0a0a;
    color: #ffffff;
    border: none;
    border-radius: 18px;
    padding: 8px 20px;
    font-weight: 600;
    font-size: 13px;
}}

QPushButton:hover {{
    background-color: #262626;
}}

QPushButton:disabled {{
    background-color: #d8d8db;
    color: #9a9a9e;
}}

QPushButton#SecondaryButton {{
    background-color: #ffffff;
    color: #0a0a0a;
    border: 1px solid #dcdce0;
}}

QPushButton#SecondaryButton:hover {{
    background-color: #f5f5f6;
}}

QPushButton#DangerButton {{
    background-color: #ffffff;
    color: #c22b2b;
    border: 1px solid #f0c9c9;
}}

QPushButton#DangerButton:hover {{
    background-color: #fdf0f0;
}}

QComboBox {{
    background-color: #ffffff;
    border: 1px solid #dcdce0;
    border-radius: 10px;
    padding: 6px 10px;
    color: #0a0a0a;
}}

QComboBox:hover {{
    border: 1px solid #b5b5b9;
}}

QComboBox::drop-down {{
    border: none;
    width: 24px;
}}

/* This is the actual popup list that opens when you click a combobox --
   the part that was showing invisible white-on-white text before. Explicit
   colors here + Fusion style + the QPalette above make it reliable. */
QComboBox QAbstractItemView {{
    background-color: #ffffff;
    color: #0a0a0a;
    border: 1px solid #e5e5e7;
    border-radius: 8px;
    selection-background-color: #f0f0f2;
    selection-color: #0a0a0a;
    outline: none;
    padding: 4px;
}}

QSpinBox {{
    background-color: #ffffff;
    border: 1px solid #dcdce0;
    border-radius: 10px;
    padding: 6px 10px;
    color: #0a0a0a;
}}

QCheckBox {{
    color: #0a0a0a;
    font-size: 13px;
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid #b5b5b9;
    border-radius: 4px;
    background-color: #ffffff;
}}

QCheckBox::indicator:checked {{
    background-color: #0a0a0a;
    border: 1px solid #0a0a0a;
}}

QMenuBar {{
    background-color: #ffffff;
    color: #0a0a0a;
    border-bottom: 1px solid #eeeeee;
}}

QMenuBar::item {{
    color: #0a0a0a;
    background: transparent;
}}

QMenuBar::item:selected {{
    background-color: #f0f0f2;
    border-radius: 6px;
}}

QMenu {{
    background-color: #ffffff;
    border: 1px solid #e5e5e7;
    border-radius: 8px;
    color: #0a0a0a;
    padding: 4px;
}}

QMenu::item {{
    color: #0a0a0a;
    padding: 6px 12px;
}}

QMenu::item:selected {{
    background-color: #f0f0f2;
    border-radius: 6px;
}}

QToolTip {{
    background-color: #ffffff;
    color: #0a0a0a;
    border: 1px solid #e5e5e7;
    padding: 4px 8px;
    border-radius: 6px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
}}

QScrollBar::handle:vertical {{
    background: #d8d8db;
    border-radius: 5px;
    min-height: 30px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
"""
