"""Application entry point for Satori Vibro Test."""

import os
import sys

# Gigaport eX exposes 6 channels via ASIO on Windows — must be set before sounddevice loads.
if sys.platform == "win32":
    os.environ.setdefault("SD_ENABLE_ASIO", "1")

import warnings

from PyQt5.QtWidgets import QApplication

from ui.main_window import MainWindow


def main() -> int:
    """Start Qt application."""
    warnings.filterwarnings("ignore", message="data discontinuity in recording")
    app = QApplication(sys.argv)
    app.setApplicationName("Satori Vibro Test")
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
