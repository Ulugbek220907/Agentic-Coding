import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from ui.main_window import MainWindow
from ui.theme import STYLESHEET, build_light_palette


def main():
    app = QApplication(sys.argv)

    # Fusion is the one built-in Qt style that reliably respects a custom
    # QPalette across every widget (including combobox popups). Without
    # this, Windows' native style can ignore our palette for certain
    # widgets and fall back to the OS theme -- which, in Dark Mode, means
    # invisible white-on-white text in dropdowns and checkboxes.
    app.setStyle("Fusion")
    app.setPalette(build_light_palette())

    font = QFont("Google Sans")
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    font.setWeight(QFont.Weight.Bold)
    font.setPointSize(10)
    app.setFont(font)

    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
