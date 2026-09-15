from __future__ import annotations

import webbrowser
from dataclasses import dataclass


PROVIDER_URLS = {
    "chatgpt": "https://chatgpt.com/",
    "grok": "https://grok.com/",
    "claude": "https://claude.ai/new",
    "gemini": "https://gemini.google.com/app",
    "custom": "https://chatgpt.com/",
}


@dataclass(frozen=True)
class AIHandoffResult:
    provider: str
    url: str
    prompt: str


class AIHandoffProvider:
    """No-API handoff: copy a clean prompt and open the user's chosen AI website."""

    def __init__(self, provider: str = "chatgpt", url: str | None = None) -> None:
        self.provider = provider or "chatgpt"
        self.url = url or PROVIDER_URLS.get(self.provider, PROVIDER_URLS["chatgpt"])

    def handoff(self, prompt: str, copy_text: object | None = None, open_browser: bool = True) -> AIHandoffResult:
        if copy_text:
            copy_text(prompt)
        if open_browser:
            webbrowser.open(self.url)
        return AIHandoffResult(provider=self.provider, url=self.url, prompt=prompt)
