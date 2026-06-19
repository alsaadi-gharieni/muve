"""Application entry point for Satori Vibro Test."""

import sys
from PyQt5.QtWidgets import QApplication

from ui.main_window import MainWindow


def main() -> int:
    """Start Qt application."""
    app = QApplication(sys.argv)
    app.setApplicationName("Satori Vibro Test")
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
