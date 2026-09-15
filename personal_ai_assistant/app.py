from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .storage import default_data_dir, default_db_path, migrate_legacy_repo_data


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setProperty("interactive_setup", "--background" not in sys.argv)
    project_root = Path(__file__).resolve().parent.parent
    migrate_legacy_repo_data(project_root, default_data_dir())
    db_path = default_db_path()
    window = MainWindow(db_path)
    if "--background" not in sys.argv:
        window.show()
    return app.exec()
