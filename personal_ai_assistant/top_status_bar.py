from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QLabel, QHBoxLayout, QWidget

from .display import (
    STATUS_BAR_MARGIN,
    STATUS_BAR_MAX_WIDTH,
    STATUS_BAR_MIN_WIDTH,
    POSITION_TOP_LEFT,
    choose_status_screen,
    mark_primary,
    sanitized_screen_geometry,
    status_bar_geometry,
)
from .models import DisplayGeometry


class TopStatusBar(QWidget):
    HORIZONTAL_MARGIN = STATUS_BAR_MARGIN
    MAX_WIDTH = STATUS_BAR_MAX_WIDTH
    MIN_WIDTH = STATUS_BAR_MIN_WIDTH

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowTitle("私人助理狀態列")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedHeight(28)

        shell = QFrame()
        shell.setObjectName("TopStatusShell")
        shadow = QGraphicsDropShadowEffect(shell)
        shadow.setBlurRadius(14)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 70))
        shell.setGraphicsEffect(shadow)

        self.label = QLabel("提醒服務中")
        self.label.setObjectName("TopStatusLabel")
        self.label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.label.setWordWrap(False)

        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(12, 0, 12, 0)
        shell_layout.addWidget(self.label)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(shell)

        self.setStyleSheet(
            """
            QWidget {
                background: transparent;
                color: #f2f5f8;
                font-family: "Microsoft JhengHei UI", "Segoe UI", sans-serif;
                font-size: 12px;
            }
            #TopStatusShell {
                background: rgba(18, 20, 24, 218);
                border: 1px solid rgba(255, 255, 255, 18);
                border-radius: 8px;
            }
            #TopStatusLabel {
                font-weight: 600;
            }
            """
        )
        self.move_to_primary_top()

    def screen_geometries(self) -> list[DisplayGeometry]:
        primary = QGuiApplication.primaryScreen()
        primary_name = primary.name() if primary else None
        return mark_primary(
            [sanitized_screen_geometry(screen, index, primary_name) for index, screen in enumerate(QGuiApplication.screens())]
        )

    def move_to_primary_top(self) -> None:
        self.move_to_screen_top(choose_status_screen(self.screen_geometries(), None, "primary"))

    def move_to_screen_top(self, screen: DisplayGeometry | None, position: str = POSITION_TOP_LEFT) -> None:
        if not screen:
            return
        self.setGeometry(*status_bar_geometry(screen, self.width(), position, height=self.height()))

    def set_status_text(self, text: str, screen: DisplayGeometry | None = None, position: str = POSITION_TOP_LEFT) -> None:
        screen = screen or choose_status_screen(self.screen_geometries(), None, "primary")
        if not screen:
            self.label.setText(text)
            return
        metrics = QFontMetrics(self.label.font())
        text_width = metrics.horizontalAdvance(text) + 32
        _, _, width, _ = status_bar_geometry(screen, text_width, position, height=self.height())
        label_width = max(80, width - 24)
        self.resize(width, self.height())
        self.label.setText(metrics.elidedText(text, Qt.ElideRight, label_width))
        self.move_to_screen_top(screen, position)
