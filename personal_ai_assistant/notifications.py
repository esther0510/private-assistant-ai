from __future__ import annotations


class NotificationService:
    def __init__(self, app_id: str = "Private Assistant AI") -> None:
        self.app_id = app_id
        self.last_error: str | None = None

    def notify(self, title: str, body: str) -> bool:
        try:
            from winotify import Notification

            toast = Notification(app_id=self.app_id, title=title, msg=body)
            toast.show()
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False
