from __future__ import annotations


WORK_APPS = {
    "code",
    "code.exe",
    "devenv.exe",
    "rider64.exe",
    "pycharm64.exe",
    "webstorm64.exe",
    "unity.exe",
    "unity hub.exe",
    "cmd.exe",
    "powershell.exe",
    "windowsterminal.exe",
    "wezterm-gui.exe",
}

CHAT_APPS = {
    "discord.exe",
    "slack.exe",
    "teams.exe",
    "telegram.exe",
    "line.exe",
    "whatsapp.exe",
    "messenger.exe",
}

GAME_HINTS = (
    "steam",
    "epic games",
    "riot client",
    "battle.net",
    "game",
    "helldivers",
    "genshin",
    "valorant",
    "league of legends",
    "minecraft",
)

VIDEO_HINTS = (
    "youtube",
    "netflix",
    "twitch",
    "bilibili",
    "disney+",
    "prime video",
    "vlc",
    "media player",
)

BROWSER_APPS = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
}


def classify_mode(app_name: str, window_title: str, idle_seconds: int = 0) -> str:
    app = (app_name or "").lower()
    title = (window_title or "").lower()
    combined = f"{app} {title}"

    if idle_seconds >= 300:
        return "閒置"

    if app in WORK_APPS:
        return "工作"

    if app in CHAT_APPS:
        return "聊天"

    if any(hint in combined for hint in VIDEO_HINTS):
        return "看片"

    if any(hint in combined for hint in GAME_HINTS):
        return "遊戲"

    if app in BROWSER_APPS:
        if any(word in title for word in ("github", "stackoverflow", "docs", "documentation", "chatgpt", "jira")):
            return "工作"
        return "瀏覽"

    return "一般"
