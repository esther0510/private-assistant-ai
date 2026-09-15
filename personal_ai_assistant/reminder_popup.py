from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .models import DisplayGeometry
from .models import Reminder


class ReminderPopup(QWidget):
    def __init__(
        self,
        reminder: Reminder,
        title: str,
        body: str,
        on_complete: Callable[[int], None],
        on_snooze: Callable[[int], None],
        on_ignore: Callable[[int], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.reminder = reminder
        self.on_complete = on_complete
        self.on_snooze = on_snooze
        self.on_ignore = on_ignore

        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setFixedWidth(360)
        self.setFocusPolicy(Qt.NoFocus)

        shell = QFrame()
        shell.setObjectName("ReminderShell")
        shadow = QGraphicsDropShadowEffect(shell)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        shell.setGraphicsEffect(shadow)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setObjectName("ReminderTitle")
        title_label.setWordWrap(True)

        body_label = QLabel(body)
        body_label.setObjectName("ReminderBody")
        body_label.setWordWrap(True)

        time_label = QLabel(datetime.now().strftime("%H:%M"))
        time_label.setObjectName("ReminderTime")

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        complete_button = QPushButton("完成")
        snooze_button = QPushButton("10 分鐘後")
        for button in (complete_button, snooze_button):
            button.setFocusPolicy(Qt.NoFocus)
            button.setMinimumWidth(120)
            button.setMinimumHeight(34)

        complete_button.clicked.connect(self._complete)
        snooze_button.clicked.connect(self._snooze)
        button_row.addWidget(complete_button, stretch=1)
        button_row.addWidget(snooze_button, stretch=1)

        layout.addWidget(title_label)
        layout.addWidget(body_label)
        layout.addWidget(time_label)
        layout.addLayout(button_row)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(shell)

        self.setStyleSheet(
            """
            #ReminderShell {
                background: #fffdf8;
                border: 1px solid #d8caa9;
                border-radius: 8px;
            }
            #ReminderTitle {
                font-size: 16px;
                font-weight: 700;
                color: #202020;
            }
            #ReminderBody {
                color: #333;
                line-height: 1.35;
            }
            #ReminderTime {
                color: #6a6255;
                font-size: 12px;
            }
            QPushButton {
                padding: 7px 10px;
                border: 1px solid #b8b0a0;
                border-radius: 5px;
                background: #ffffff;
                color: #1f2933;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #f4efe4;
            }
            QPushButton:pressed {
                background: #e9ddc8;
            }
            """
        )

    def show_near_top_center(
        self,
        stack_index: int = 0,
        top_offset: int = 36,
        screen_geometry: DisplayGeometry | None = None,
    ) -> None:
        self.adjustSize()
        qt_screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        margin = 18
        if screen_geometry:
            left = screen_geometry.available_x
            top = screen_geometry.available_y
            width = screen_geometry.available_width
        else:
            geometry = qt_screen.availableGeometry()
            left = geometry.left()
            top = geometry.top()
            width = geometry.width()
        x = left + ((width - self.width()) // 2)
        y = top + top_offset + (stack_index * (self.height() + 12))
        self.move(max(left + margin, x), max(top + margin, y))
        self.show()
        self.raise_()

    def show_near_bottom_right(self, stack_index: int = 0) -> None:
        self.show_near_top_center(stack_index)

    def _complete(self) -> None:
        self.on_complete(self.reminder.id)
        self.close()

    def _snooze(self) -> None:
        self.on_snooze(self.reminder.id)
        self.close()


class AssistantNotificationPrompt(QWidget):
    def __init__(
        self,
        notification_event_id: int,
        title: str,
        body: str,
        on_open: Callable[[int], None],
        on_snooze: Callable[[int], None],
        on_ignore: Callable[[int], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.notification_event_id = notification_event_id
        self.on_open = on_open
        self.on_snooze = on_snooze
        self.on_ignore = on_ignore
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setFixedWidth(390)
        self.setFocusPolicy(Qt.NoFocus)

        shell = QFrame()
        shell.setObjectName("AssistantPromptShell")
        shadow = QGraphicsDropShadowEffect(shell)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        shell.setGraphicsEffect(shadow)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setObjectName("AssistantPromptTitle")
        title_label.setWordWrap(True)
        body_label = QLabel(body)
        body_label.setObjectName("AssistantPromptBody")
        body_label.setWordWrap(True)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        open_button = QPushButton("去看看")
        snooze_button = QPushButton("稍後提醒")
        ignore_button = QPushButton("忽略")
        for button in (open_button, snooze_button, ignore_button):
            button.setFocusPolicy(Qt.NoFocus)
            button.setMinimumHeight(34)
        open_button.clicked.connect(self._open)
        snooze_button.clicked.connect(self._snooze)
        ignore_button.clicked.connect(self._ignore)
        button_row.addWidget(open_button, stretch=1)
        button_row.addWidget(snooze_button, stretch=1)
        button_row.addWidget(ignore_button, stretch=1)

        layout.addWidget(title_label)
        layout.addWidget(body_label)
        layout.addLayout(button_row)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(shell)
        self.setStyleSheet(
            """
            #AssistantPromptShell {
                background: #f7fbff;
                border: 1px solid #aac7e8;
                border-radius: 8px;
            }
            #AssistantPromptTitle {
                font-size: 16px;
                font-weight: 700;
                color: #172033;
            }
            #AssistantPromptBody {
                color: #243247;
                line-height: 1.35;
            }
            QPushButton {
                padding: 7px 10px;
                border: 1px solid #9fb5ce;
                border-radius: 5px;
                background: #ffffff;
                color: #172033;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #eaf3ff;
            }
            QPushButton:pressed {
                background: #d8e8fb;
            }
            """
        )

    def show_near_top_center(
        self,
        stack_index: int = 0,
        top_offset: int = 36,
        screen_geometry: DisplayGeometry | None = None,
    ) -> None:
        self.adjustSize()
        qt_screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        margin = 18
        if screen_geometry:
            left = screen_geometry.available_x
            top = screen_geometry.available_y
            width = screen_geometry.available_width
        else:
            geometry = qt_screen.availableGeometry()
            left = geometry.left()
            top = geometry.top()
            width = geometry.width()
        x = left + ((width - self.width()) // 2)
        y = top + top_offset + (stack_index * (self.height() + 12))
        self.move(max(left + margin, x), max(top + margin, y))
        self.show()
        self.raise_()

    def _open(self) -> None:
        self.on_open(self.notification_event_id)
        self.close()

    def _snooze(self) -> None:
        self.on_snooze(self.notification_event_id)
        self.close()

    def _ignore(self) -> None:
        self.on_ignore(self.notification_event_id)
        self.close()
