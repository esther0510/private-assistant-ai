from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from .reminder_trace import trace_reminder


TIME_PATTERN = re.compile(r"(?P<hour>[01]?\d|2[0-3])[:：](?P<minute>[0-5]\d)")
CHINESE_TIME_PATTERN = re.compile(
    r"(?P<period>今晚|今天晚上|明天早上|明天上午|明天下午|明天晚上|後天早上|後天上午|後天下午|後天晚上|凌晨|早上|上午|中午|下午|晚上)?\s*(?P<hour>[零〇一二兩三四五六七八九十百\d]{1,4})\s*[點点]\s*(?P<half>半)?(?P<minute>[零〇一二兩三四五六七八九十百\d]{1,4})?\s*分?"
)
DATE_PATTERN = re.compile(r"(?:(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日號]?|(?P<slash_month>\d{1,2})\s*/\s*(?P<slash_day>\d{1,2}))")
DAILY_PATTERN = re.compile(r"^(每天|每日)\s*")
WEEKLY_PATTERN = re.compile(r"^(每週|每周|每星期)\s*(?P<days>[一二三四五六日天、,，和與\s]+)\s*")
INTERVAL_PATTERN = re.compile(r"每\s*(?P<days>\d+|[一二兩三四五六七八九十百]+)\s*天")
ACTIVE_DAYS_PATTERN = re.compile(r"(?:連續(?:吃|使用|擦|做)?|吃|服用|使用|擦|活動|持續)\s*(?P<days>\d+|[一二兩三四五六七八九十百]+)\s*天")
PERIOD_DAYS_PATTERN = re.compile(r"週期\s*(?P<days>\d+|[一二兩三四五六七八九十百]+)\s*天")
BREAK_DAYS_PATTERN = re.compile(r"(?:停|休息|暫停)\s*(?P<days>\d+|[一二兩三四五六七八九十百]+)\s*天")
RELATIVE_PATTERN = re.compile(
    r"(?P<amount>半|(?:\d+|[一二兩三四五六七八九十百]+)(?:個)?半?|(?:\d+|[一二兩三四五六七八九十百]+)\s*個\s*半)\s*(?P<unit>分鐘|分|小時|鐘頭)\s*後"
)
WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
AMBIGUOUS_TIME_GRACE = timedelta(minutes=10)
AMBIGUOUS_TIME_CONFIRM_MARGIN = 1.0
SLEEP_HOURS = {0, 1, 2, 3, 4, 5, 6}


@dataclass(frozen=True)
class ParsedReminder:
    text: str
    due_at: datetime
    recurrence_rule: str | None = None
    next_due_at: datetime | None = None
    needs_confirmation: bool = False
    confirmation_summary: str | None = None
    missing_fields: tuple[str, ...] = ()
    kind: str = "scheduled"
    context_name: str | None = None
    context_trigger: str | None = None
    time_inferred: bool = False
    inference_reason: str | None = None
    ambiguous_time_options: tuple[datetime, ...] = ()

    @property
    def understood_message(self) -> str:
        if self.kind == "today_inbox":
            return f"已理解：今天有空時提醒「{self.text}」"
        if self.kind == "context":
            trigger = "打開" if self.context_trigger == "open" else "關掉"
            return f"已理解：下次{trigger}{self.context_name or '指定 App'}提醒「{self.text}」"
        day = "今天" if self.due_at.date() == datetime.now().date() else self.due_at.strftime("%m/%d")
        return f"已理解：{day} {format_clock_time(self.due_at.hour, self.due_at.minute)} 提醒「{self.text}」"


@dataclass(frozen=True)
class RecurrenceOccurrence:
    due_at: datetime
    next_due_at: datetime | None
    cycle_index: int | None = None
    occurrence_index: int | None = None
    position_in_cycle: int | None = None
    active_days: int | None = None
    is_missed: bool = False


@dataclass(frozen=True)
class TimeResolution:
    hour: int
    minute: int
    start: int
    end: int
    time_inferred: bool = False
    inference_reason: str | None = None
    needs_confirmation: bool = False
    confirmation_options: tuple[datetime, ...] = ()
    due_at_override: datetime | None = None


@dataclass(frozen=True)
class _RawTimeMatch:
    hour: int
    minute: int
    start: int
    end: int
    explicit_daypart: bool = False
    twenty_four_hour: bool = False
    day_offset: int | None = None
    bare_twelve: bool = False


def parse_natural_reminder(raw_text: str, now: datetime | None = None,
                           active_hour_scores: dict[int, float] | None = None,
                           user_is_active: bool = True) -> ParsedReminder:
    trace_reminder("input", raw_text=raw_text)
    parsed = _parse_natural_reminder(raw_text, now, active_hour_scores, user_is_active)
    trace_reminder("parsed_datetime", due_at=parsed.due_at,
                   needs_confirmation=parsed.needs_confirmation)
    return parsed


def _parse_natural_reminder(
    raw_text: str,
    now: datetime | None = None,
    active_hour_scores: dict[int, float] | None = None,
    user_is_active: bool = True,
) -> ParsedReminder:
    """Parse one-time, recurring, Today Inbox, and app-context reminder text."""
    now = now or datetime.now()
    value = " ".join(raw_text.strip().split())
    if not value:
        raise ValueError("請輸入提醒內容，例如：18:30 喝水")

    context_reminder = _parse_context_reminder(value, now)
    if context_reminder:
        return context_reminder

    if _looks_like_today_inbox(value):
        return ParsedReminder(
            text=_strip_inbox_prefix(value),
            due_at=_today_inbox_due_at(now),
            kind="today_inbox",
        )

    complex_reminder = _parse_complex_reminder(value, now, active_hour_scores, user_is_active)
    if complex_reminder:
        return complex_reminder

    relative = _parse_relative_reminder(value, now)
    if relative:
        return relative

    raw_time_match = _find_time(value)
    time_match = _resolve_time_match(value, raw_time_match, now, active_hour_scores, user_is_active)
    if not time_match:
        cleaned = _clean_label(value)
        if cleaned and cleaned != value:
            raise ValueError("我看懂了事項，但沒看懂時間。可以說：10分鐘後、今晚10點半、或明天早上8點。")
        raise ValueError("我還沒看懂提醒時間。可以說：10分鐘後、今晚10點半、或明天早上8點。")

    trace_reminder("time_resolution", hour=time_match.hour, minute=time_match.minute,
                   due_at=time_match.due_at_override, needs_confirmation=time_match.needs_confirmation)
    hour, minute, start, end = time_match.hour, time_match.minute, time_match.start, time_match.end
    prefix = value[:start].strip()
    suffix = value[end:].strip()
    explicit_date = _parse_date_in_text(value, now)
    date_part = explicit_date[0] if explicit_date else None
    recurrence = _parse_recurrence_prefix(prefix, hour, minute)
    if recurrence:
        reminder_text = _clean_label(suffix.strip(" -，,：:"))
    else:
        reminder_text = (prefix + " " + suffix).strip(" -，,：:")
        if explicit_date:
            reminder_text = (reminder_text[: explicit_date[1]] + " " + reminder_text[explicit_date[2] :]).strip()
        reminder_text = _clean_label(reminder_text)
    if not reminder_text:
        reminder_text = "提醒"

    if recurrence:
        rule = recurrence_to_rule(recurrence)
        due_at = next_occurrence(rule, now)
        next_due_at = next_occurrence(rule, due_at)
        return ParsedReminder(
            text=reminder_text,
            due_at=due_at,
            recurrence_rule=rule,
            next_due_at=next_due_at,
            time_inferred=time_match.time_inferred,
            inference_reason=time_match.inference_reason,
            needs_confirmation=time_match.needs_confirmation,
            confirmation_summary=_ambiguous_time_confirmation(reminder_text, time_match.confirmation_options)
            if time_match.needs_confirmation
            else None,
            ambiguous_time_options=time_match.confirmation_options,
        )

    due_date = date_part or _relative_date_from_period(value, now) or now.date()
    due_at = time_match.due_at_override or datetime.combine(due_date, time(hour, minute))
    if due_at <= now:
        if not (time_match.time_inferred and now - due_at <= AMBIGUOUS_TIME_GRACE):
            due_at = _roll_forward_once(value, due_at, now)

    return ParsedReminder(
        text=reminder_text,
        due_at=due_at,
        needs_confirmation=time_match.needs_confirmation,
        confirmation_summary=_ambiguous_time_confirmation(reminder_text, time_match.confirmation_options)
        if time_match.needs_confirmation
        else None,
        time_inferred=time_match.time_inferred,
        inference_reason=time_match.inference_reason,
        ambiguous_time_options=time_match.confirmation_options,
    )


def recurrence_to_rule(recurrence: dict[str, object]) -> str:
    return json.dumps(recurrence, ensure_ascii=False, sort_keys=True)


def describe_recurrence_rule(rule_text: str | None) -> str:
    if not rule_text:
        return "一次性"
    try:
        rule = json.loads(rule_text)
    except json.JSONDecodeError:
        return "週期提醒"

    if rule.get("freq") == "daily":
        return f"每天 {rule.get('time', '')}".strip()
    if rule.get("freq") == "weekly":
        days = "".join(_weekday_label(int(day)) for day in rule.get("days", []))
        day_text = "、".join(days)
        return f"每週{day_text} {rule.get('time', '')}".strip()
    if rule.get("freq") == "cycle":
        base = f"從 {rule.get('start_date')} {rule.get('time', '')}"
        active = rule.get("active_days")
        break_days = rule.get("break_days")
        repeat = "循環" if rule.get("repeat") else "不循環"
        if rule.get("interval_days"):
            base += f" 每 {rule.get('interval_days')} 天"
        else:
            base += " 每天"
        if active:
            base += f" 活動 {active} 天"
        if break_days is not None:
            base += f" 休息 {break_days} 天"
        return f"{base} {repeat}".strip()
    if rule.get("freq") == "interval":
        return f"每 {rule.get('interval_days')} 天 {rule.get('time', '')}".strip()
    return "週期提醒"


def next_occurrence(rule_text: str, after: datetime) -> datetime | None:
    rule = json.loads(rule_text)
    hour, minute = _parse_rule_time(str(rule["time"]))

    if rule["freq"] == "daily":
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    if rule["freq"] == "weekly":
        days = sorted(int(day) for day in rule["days"])
        for offset in range(8):
            day = after + timedelta(days=offset)
            if day.weekday() not in days:
                continue
            candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > after:
                return candidate
        raise ValueError("找不到下一次週期提醒時間")

    if rule["freq"] == "interval":
        start_date = date.fromisoformat(str(rule["start_date"]))
        interval_days = max(1, int(rule["interval_days"]))
        return _next_interval_occurrence(start_date, interval_days, hour, minute, after)

    if rule["freq"] == "cycle":
        return _next_cycle_occurrence(rule, hour, minute, after)

    raise ValueError(f"不支援的週期規則：{rule['freq']}")


def recurrence_occurrence(rule_text: str, at: datetime, keep_missed: bool = True) -> RecurrenceOccurrence | None:
    """Return the active occurrence that should be shown or acted on at this local time."""
    rule = json.loads(rule_text)
    hour, minute = _parse_rule_time(str(rule["time"]))
    if rule["freq"] != "cycle":
        due_at = next_occurrence(rule_text, at)
        if due_at is None:
            return None
        return RecurrenceOccurrence(due_at=due_at, next_due_at=next_occurrence(rule_text, due_at), is_missed=due_at <= at)

    upcoming = _next_cycle_occurrence(rule, hour, minute, at)
    if upcoming is not None and upcoming.date() == at.date():
        return RecurrenceOccurrence(
            due_at=upcoming,
            next_due_at=_next_cycle_occurrence(rule, hour, minute, upcoming),
            **(_cycle_metadata(rule, upcoming.date()) or {}),
            is_missed=False,
        )

    current = _cycle_due_on_or_before(rule, hour, minute, at) if keep_missed else None
    if current is not None:
        next_due_at = _next_cycle_occurrence(rule, hour, minute, current)
        return RecurrenceOccurrence(
            due_at=current,
            next_due_at=next_due_at,
            **(_cycle_metadata(rule, current.date()) or {}),
            is_missed=current <= at,
        )

    if upcoming is None:
        return None
    return RecurrenceOccurrence(
        due_at=upcoming,
        next_due_at=_next_cycle_occurrence(rule, hour, minute, upcoming),
        **(_cycle_metadata(rule, upcoming.date()) or {}),
        is_missed=False,
    )


def recurrence_next_after(rule_text: str, resolved_due_at: datetime) -> RecurrenceOccurrence | None:
    due_at = next_occurrence(rule_text, resolved_due_at)
    if due_at is None:
        return None
    try:
        metadata = _cycle_metadata(json.loads(rule_text), due_at.date()) or {}
    except (json.JSONDecodeError, KeyError, ValueError):
        metadata = {}
    return RecurrenceOccurrence(
        due_at=due_at,
        next_due_at=next_occurrence(rule_text, due_at),
        **metadata,
    )


def cycle_status(rule_text: str | None, at: datetime | None = None) -> str:
    if not rule_text:
        return ""
    at = at or datetime.now()
    try:
        rule = json.loads(rule_text)
    except json.JSONDecodeError:
        return ""
    if rule.get("freq") != "cycle":
        return ""

    status = _cycle_status_for_date(rule, at.date())
    if status is None:
        start_date = date.fromisoformat(str(rule["start_date"]))
        return f"尚未開始｜開始：{start_date.strftime('%m/%d')} {rule.get('time', '')}"
    return status


def format_clock_time(hour: int, minute: int) -> str:
    value = f"{hour:02d}:{minute:02d}"
    return value + "（午夜12點）" if hour == 0 and minute == 0 else value


def format_due_at(due_at: datetime) -> str:
    return due_at.strftime("%Y-%m-%d ") + format_clock_time(due_at.hour, due_at.minute)


def format_response_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "尚無反應資料"
    if seconds < 60:
        return f"平均 {seconds} 秒反應"
    minutes = round(seconds / 60)
    return f"平均 {minutes} 分鐘反應"


def _parse_complex_reminder(
    value: str,
    now: datetime,
    active_hour_scores: dict[int, float] | None = None,
    user_is_active: bool = True,
) -> ParsedReminder | None:
    active_match = ACTIVE_DAYS_PATTERN.search(value)
    period_match = PERIOD_DAYS_PATTERN.search(value)
    break_match = BREAK_DAYS_PATTERN.search(value)
    interval_match = INTERVAL_PATTERN.search(value)
    has_cycle_language = bool(active_match or break_match or interval_match or "循環" in value or "週期" in value)
    if not has_cycle_language:
        return None

    raw_time_match = _find_time(value)
    time_match = _resolve_time_match(value, raw_time_match, now, active_hour_scores, user_is_active)
    if not time_match:
        start_date = _parse_start_date(value, now) or now.date()
        label = _clean_complex_text(value, len(value), len(value)) or _clean_label(value) or "提醒"
        return ParsedReminder(
            text=label,
            due_at=_today_inbox_due_at(now),
            needs_confirmation=True,
            confirmation_summary="\n".join(
                [
                    "我理解的是：",
                    f"事項：{label}",
                    f"開始日期：{start_date.isoformat()}",
                    "提醒時間：未指定",
                    "需要補充：提醒時間",
                ]
            ),
            missing_fields=("提醒時間",),
        )
    hour, minute, time_start, time_end = time_match.hour, time_match.minute, time_match.start, time_match.end
    start_date = _parse_start_date(value, now)
    active_days = _parse_number(active_match.group("days")) if active_match else (_parse_number(period_match.group("days")) if period_match else None)
    break_days = _parse_number(break_match.group("days")) if break_match else None
    interval_days = _parse_number(interval_match.group("days")) if interval_match else None
    repeat = any(token in value for token in ("循環", "再開始", "下一輪"))
    weekdays = _parse_weekdays_anywhere(value)

    missing: list[str] = []
    if start_date is None and interval_days and active_days is None:
        start_date = now.date()
    if start_date is None:
        missing.append("開始日期")
        start_date = now.date()
    if active_days is None and not interval_days:
        missing.append("活動天數")
    if ("週期" in value or repeat) and break_days is None and active_days is not None:
        missing.append("休息天數")

    label = _clean_complex_text(value, time_start, time_end)
    if not label:
        label = "提醒"

    if interval_days and active_days is None:
        rule = {
            "freq": "interval",
            "start_date": start_date.isoformat(),
            "time": f"{hour:02d}:{minute:02d}",
            "interval_days": interval_days,
        }
    else:
        rule = {
            "freq": "cycle",
            "start_date": start_date.isoformat(),
            "time": f"{hour:02d}:{minute:02d}",
            "active_days": active_days,
            "break_days": break_days,
            "repeat": repeat,
            "weekdays": weekdays,
            "interval_days": interval_days,
        }

    rule_text = recurrence_to_rule(rule)
    due_at = next_occurrence(rule_text, now)
    if due_at is None:
        due_at = datetime.combine(start_date, time(hour, minute))
    next_due_at = next_occurrence(rule_text, due_at)

    return ParsedReminder(
        text=label,
        due_at=due_at,
        recurrence_rule=rule_text,
        next_due_at=next_due_at,
        needs_confirmation=True,
        confirmation_summary=_confirmation_summary(label, start_date, hour, minute, active_days, break_days, repeat, weekdays, interval_days, missing)
        + (
            "\n\n" + _ambiguous_time_confirmation(label, time_match.confirmation_options)
            if time_match.needs_confirmation
            else ""
        ),
        missing_fields=tuple(missing),
        time_inferred=time_match.time_inferred,
        inference_reason=time_match.inference_reason,
        ambiguous_time_options=time_match.confirmation_options,
    )


def _parse_recurrence_prefix(prefix: str, hour: int, minute: int) -> dict[str, object] | None:
    prefix = prefix.strip(" ，,：:")
    if re.search(r"(?:^|[，,\s])(每天|每日)$", prefix) or prefix in {"每天", "每日"}:
        return {"freq": "daily", "time": f"{hour:02d}:{minute:02d}"}

    weekly = WEEKLY_PATTERN.search(prefix)
    if not weekly:
        interval = INTERVAL_PATTERN.search(prefix)
        start_date = _parse_start_date(prefix, datetime.now())
        if interval and start_date:
            return {
                "freq": "interval",
                "start_date": start_date.isoformat(),
                "interval_days": _parse_number(interval.group("days")),
                "time": f"{hour:02d}:{minute:02d}",
            }
        return None

    days: list[int] = []
    for char in weekly.group("days"):
        if char in WEEKDAY_MAP:
            day = WEEKDAY_MAP[char]
            if day not in days:
                days.append(day)
    if not days:
        return None
    return {"freq": "weekly", "days": days, "time": f"{hour:02d}:{minute:02d}"}


def _find_time(value: str) -> _RawTimeMatch | None:
    match = TIME_PATTERN.search(value)
    if match:
        hour_text = match.group("hour")
        hour = int(hour_text)
        minute = int(match.group("minute"))
        period_hint = value[max(0, match.start() - 8) : match.start()]
        explicit_daypart = any(token in period_hint for token in ("早上", "上午", "下午", "晚上", "今晚", "凌晨", "中午"))
        if any(token in period_hint for token in ("下午", "晚上", "今晚")) and hour < 12:
            hour += 12
        if "中午" in period_hint and hour < 11:
            hour += 12
        if any(token in period_hint for token in ("凌晨", "晚上", "今晚")) and hour == 12:
            hour = 0
        return _RawTimeMatch(
            hour,
            minute,
            match.start(),
            match.end(),
            explicit_daypart=explicit_daypart,
            twenty_four_hour=hour > 12 or hour == 0 or hour_text.startswith("0"),
            bare_twelve=hour == 12,
        )

    chinese = CHINESE_TIME_PATTERN.search(value)
    if not chinese:
        return None
    hour = _parse_chinese_number(chinese.group("hour"))
    minute_text = chinese.group("minute")
    minute = 30 if chinese.group("half") else (_parse_chinese_number(minute_text) if minute_text else 0)
    period = chinese.group("period") or ""
    explicit_daypart = bool(period)
    day_offset = None
    if period.startswith("明天"):
        day_offset = 1
    elif period.startswith("後天"):
        day_offset = 2
    if any(token in period for token in ("下午", "晚上", "今晚")) and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if any(token in period for token in ("凌晨", "晚上", "今晚")) and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return _RawTimeMatch(
        hour,
        minute,
        chinese.start(),
        chinese.end(),
        explicit_daypart=explicit_daypart,
        twenty_four_hour=hour > 12 or hour == 0,
        day_offset=day_offset,
        bare_twelve=hour == 12,
    )


def _resolve_time_match(
    value: str,
    match: _RawTimeMatch | None,
    now: datetime,
    active_hour_scores: dict[int, float] | None = None,
    user_is_active: bool = True,
) -> TimeResolution | None:
    if not match:
        return None
    # Explicit English dayparts take precedence over activity/context inference.
    before = re.search(r"\b(AM|PM)\s*$", value[:match.start], re.I)
    after = re.match(r"\s*(AM|PM)\b", value[match.end:], re.I)
    meridiem = before or after
    if meridiem and 1 <= match.hour <= 12:
        hour = match.hour % 12 + (12 if meridiem.group(1).upper() == "PM" else 0)
        return TimeResolution(hour, match.minute, before.start() if before else match.start,
                              match.end + after.end() if after else match.end)
    if match.bare_twelve and not match.explicit_daypart:
        night = any(token in value for token in ("睡覺", "睡觉", "晚安", "關電腦", "关电脑", "宵夜", "夜間吃藥", "夜间吃药"))
        day = any(token in value for token in ("午餐", "中午", "白天"))
        if night != day:
            return TimeResolution(0 if night else 12, match.minute, match.start, match.end,
                                  time_inferred=True, inference_reason="十二點依夜間事項判斷為午夜" if night else "十二點依白天事項判斷為中午")
        explicit_date = _parse_date_in_text(value, now)
        options = []
        for hour in (0, 12):
            due = datetime.combine(explicit_date[0] if explicit_date else now.date(), time(hour, match.minute))
            if due <= now:
                due = _roll_forward_once(value, due, now)
            options.append(due)
        return TimeResolution(12, match.minute, match.start, match.end,
                              needs_confirmation=True, confirmation_options=tuple(sorted(options)),
                              inference_reason="十二點缺少可靠情境，需確認午夜或中午")
    if match.day_offset is not None:
        target_date = now.date() + timedelta(days=match.day_offset)
        return TimeResolution(match.hour, match.minute, match.start, match.end, due_at_override=datetime.combine(target_date, time(match.hour, match.minute)))
    if match.explicit_daypart or match.twenty_four_hour or not (1 <= match.hour <= 11):
        return TimeResolution(match.hour, match.minute, match.start, match.end)

    am_hour = match.hour
    pm_hour = match.hour + 12
    am_due = _candidate_due_at(now, am_hour, match.minute)
    pm_due = _candidate_due_at(now, pm_hour, match.minute)
    if now.hour < 7 and match.hour <= 6 and not active_hour_scores:
        return TimeResolution(
            match.hour,
            match.minute,
            match.start,
            match.end,
            needs_confirmation=True,
            confirmation_options=tuple(sorted((am_due, pm_due))),
            inference_reason="清晨建立的模糊時間缺少活躍時段證據",
        )
    am_score, am_reasons = _score_time_candidate(value, now, am_due, "AM", active_hour_scores, user_is_active)
    pm_score, pm_reasons = _score_time_candidate(value, now, pm_due, "PM", active_hour_scores, user_is_active)
    if abs(pm_score - am_score) < AMBIGUOUS_TIME_CONFIRM_MARGIN:
        options = tuple(sorted((am_due, pm_due)))
        return TimeResolution(
            match.hour,
            match.minute,
            match.start,
            match.end,
            needs_confirmation=True,
            confirmation_options=options,
            inference_reason="模糊 12 小時時間，AM/PM 信心不足",
        )

    chosen_due = pm_due if pm_score > am_score else am_due
    chosen_hour = chosen_due.hour
    reasons = pm_reasons if pm_score > am_score else am_reasons
    return TimeResolution(
        chosen_hour,
        match.minute,
        match.start,
        match.end,
        time_inferred=True,
        inference_reason="；".join(reasons),
        due_at_override=chosen_due,
    )


def _candidate_due_at(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now and now - candidate > AMBIGUOUS_TIME_GRACE:
        candidate += timedelta(days=1)
    return candidate


def _score_time_candidate(
    value: str,
    now: datetime,
    candidate: datetime,
    half: str,
    active_hour_scores: dict[int, float] | None,
    user_is_active: bool,
) -> tuple[float, list[str]]:
    delta_hours = (candidate - now).total_seconds() / 3600
    score = max(0.0, 12.0 - abs(delta_hours))
    reasons = [f"{candidate.strftime('%m/%d %H:%M')} 是較近的合理時間"]
    if candidate <= now and now - candidate <= AMBIGUOUS_TIME_GRACE:
        score += 6.0
        minutes = int((now - candidate).total_seconds() // 60)
        reasons.append(f"剛過 {minutes} 分鐘，視為剛到期提醒")
    if half == "PM" and user_is_active and now.hour >= 12 and candidate.date() == now.date() and -0.17 <= delta_hours <= 8:
        score += 5.0
        reasons.append("現在是下午/晚上且使用者正在使用電腦")
    if half == "AM" and candidate.hour in SLEEP_HOURS:
        score -= 2.5
        reasons.append("避免在未明說時自動排到凌晨/清晨")
    if any(token in value for token in ("垃圾", "收拾", "丟", "吃飯", "晚餐", "出門", "洗澡", "洗衣")) and half == "PM":
        score += 0.75
        reasons.append("一般生活事項弱偏向白天/傍晚")
    if active_hour_scores:
        learned = float(active_hour_scores.get(candidate.hour, 0.0))
        if learned:
            score += min(9.0, learned * 9.0)
            reasons.append(f"學習到 {candidate.hour:02d}:00 附近較常活躍")
    return score, reasons


def _ambiguous_time_confirmation(label: str, options: tuple[datetime, ...]) -> str:
    if len(options) < 2:
        return "這個時間需要確認上午或下午。"
    first, second = options[0], options[1]
    return f"請確認提醒時間：{format_due_at(first)} 還是 {format_due_at(second)}？\n事項：{label}"


def _parse_context_reminder(value: str, now: datetime) -> ParsedReminder | None:
    patterns = (
        (r"^(?:下次)?(?:打開|開)(?P<app>.+?)(?:時|後)?提醒我(?P<text>.+)$", "open"),
        (r"^(?:下次)?(?:打開|開)(?P<app>.+?)(?:時|後)?提醒(?P<text>.+)$", "open"),
        (r"^(?P<app>.+?)(?:關掉|關閉|退出|結束)後提醒我(?P<text>.+)$", "close"),
        (r"^(?:下次)?(?:關掉|關閉)(?P<app>.+?)(?:後)?提醒我(?P<text>.+)$", "close"),
    )
    for pattern, trigger in patterns:
        match = re.match(pattern, value)
        if not match:
            continue
        app_name = match.group("app").strip(" ，,：:")
        label = _clean_label(match.group("text"))
        if app_name and label:
            return ParsedReminder(
                text=label,
                due_at=now.replace(second=0, microsecond=0),
                kind="context",
                context_name=app_name,
                context_trigger=trigger,
                needs_confirmation=True,
                confirmation_summary="\n".join(
                    [
                        "我理解的是：",
                        f"事項：{label}",
                        f"情境：{app_name}",
                        f"觸發：{'打開 App' if trigger == 'open' else '關掉 App'}",
                    ]
                ),
            )
    return None


def _parse_relative_reminder(value: str, now: datetime) -> ParsedReminder | None:
    match = RELATIVE_PATTERN.search(value)
    if not match:
        return None
    amount = _parse_duration_amount(match.group("amount"))
    unit = match.group("unit")
    minutes = int(amount if unit in {"分鐘", "分"} else amount * 60)
    due_at = now + timedelta(minutes=minutes)
    due_at = due_at.replace(second=0, microsecond=0)
    label = (value[: match.start()] + " " + value[match.end() :]).strip()
    label = _clean_label(label)
    return ParsedReminder(text=label or "提醒", due_at=due_at)


def _parse_date_in_text(value: str, now: datetime) -> tuple[date, int, int] | None:
    for token, offset in (("後天", 2), ("明天", 1), ("今天", 0), ("今晚", 0)):
        index = value.find(token)
        if index >= 0:
            return now.date() + timedelta(days=offset), index, index + len(token)
    match = DATE_PATTERN.search(value)
    if not match:
        return None
    month = int(match.group("month") or match.group("slash_month"))
    day = int(match.group("day") or match.group("slash_day"))
    year = now.year
    candidate = date(year, month, day)
    if datetime.combine(candidate, time(23, 59)) <= now:
        candidate = date(year + 1, month, day)
    return candidate, match.start(), match.end()


def _relative_date_from_period(value: str, now: datetime) -> date | None:
    if "後天" in value:
        return now.date() + timedelta(days=2)
    if "明天" in value:
        return now.date() + timedelta(days=1)
    if "今天" in value or "今晚" in value:
        return now.date()
    return None


def _roll_forward_once(value: str, due_at: datetime, now: datetime) -> datetime:
    if any(token in value for token in ("今天", "今晚")):
        return due_at + timedelta(days=1)
    if DATE_PATTERN.search(value):
        return due_at + timedelta(days=365)
    return due_at + timedelta(days=1)


def _looks_like_today_inbox(text: str) -> bool:
    value = text.strip()
    if _find_time(value) or RELATIVE_PATTERN.search(value):
        return False
    return any(token in value for token in ("今天記得", "有空提醒", "有空", "今天要", "記得"))


def _strip_inbox_prefix(text: str) -> str:
    value = " ".join(text.split())
    for prefix in ("今天記得", "今天要", "今天有空提醒我", "有空提醒我", "有空提醒", "提醒我", "記得", "有空"):
        if value.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    return _clean_label(value) or "Today Inbox"


def _today_inbox_due_at(now: datetime) -> datetime:
    return now.replace(hour=23, minute=59, second=0, microsecond=0)


def _clean_label(value: str) -> str:
    label = value.strip(" -，,：:")
    label = DATE_PATTERN.sub(" ", label).strip()
    label = re.sub(r"^(?:請|幫我)?提醒我", "", label).strip()
    label = re.sub(r"^(?:請|幫我)?提醒", "", label).strip()
    label = re.sub(r"^我(?:要|想要|需要)?", "", label).strip()
    label = re.sub(r"^(?:今天|明天|後天|今晚)", "", label).strip()
    label = re.sub(r"^(?:早上|上午|中午|下午|晚上|凌晨)", "", label).strip()
    label = re.sub(r"^開始", "", label).strip()
    return label.strip(" -，,：:")


def _parse_start_date(value: str, now: datetime) -> date | None:
    if "後天" in value:
        return now.date() + timedelta(days=2)
    if "明天" in value:
        return now.date() + timedelta(days=1)
    if "今天" in value:
        return now.date()
    match = DATE_PATTERN.search(value)
    if not match:
        return None
    month = int(match.group("month") or match.group("slash_month"))
    day = int(match.group("day") or match.group("slash_day"))
    year = now.year
    candidate = date(year, month, day)
    if candidate < now.date() - timedelta(days=180):
        candidate = date(year + 1, month, day)
    return candidate


def _parse_weekdays_anywhere(value: str) -> list[int] | None:
    match = WEEKLY_PATTERN.search(value)
    if not match:
        return None
    days: list[int] = []
    for char in match.group("days"):
        if char in WEEKDAY_MAP:
            day = WEEKDAY_MAP[char]
            if day not in days:
                days.append(day)
    return days or None


def _clean_complex_text(value: str, time_start: int, time_end: int) -> str:
    label = (value[:time_start] + " " + value[time_end:]).strip()
    cleanup_patterns = [
        r"我每天要",
        r"每天要",
        r"從\s*(?:明天|今天|\d{1,2}\s*月\s*\d{1,2}\s*[日號]?|\d{1,2}\s*/\s*\d{1,2})\s*開始",
        r"每天",
        r"每日",
        r"每\s*(?:\d+|[一二兩三四五六七八九十百]+)\s*天提醒我",
        r"每\s*(?:\d+|[一二兩三四五六七八九十百]+)\s*天",
        r"連續(?:吃|使用|擦|做)?\s*(?:\d+|[一二兩三四五六七八九十百]+)\s*天",
        r"(?:吃|服用|使用|擦|活動|持續)\s*(?:\d+|[一二兩三四五六七八九十百]+)\s*天",
        r"(?:停|休息|暫停)\s*(?:\d+|[一二兩三四五六七八九十百]+)\s*天",
        r"再開始(?:下一輪|下一盒)?",
        r"開始下一盒",
        r"循環",
        r"早上|上午|中午|下午|晚上|凌晨",
        r"提醒我",
        r"，|,|。|：|:",
    ]
    for pattern in cleanup_patterns:
        label = re.sub(pattern, " ", label)
    return " ".join(label.split()).strip(" -，,：:")


def _confirmation_summary(
    label: str,
    start_date: date,
    hour: int,
    minute: int,
    active_days: int | None,
    break_days: int | None,
    repeat: bool,
    weekdays: list[int] | None,
    interval_days: int | None,
    missing: list[str],
) -> str:
    lines = [
        "我理解的是：",
        f"事項：{label}",
        f"開始日期：{start_date.isoformat() if '開始日期' not in missing else '未指定'}",
        f"提醒時間：{format_clock_time(hour, minute)}",
        f"活動天數：{active_days if active_days is not None else '未指定'}",
        f"休息天數：{break_days if break_days is not None else '未指定'}",
        f"是否循環：{'是' if repeat else '否'}",
    ]
    if weekdays:
        lines.append("週幾：" + "、".join(_weekday_label(day) for day in weekdays))
    if interval_days:
        lines.append(f"間隔：每 {interval_days} 天")
    if missing:
        lines.append("需要補充：" + "、".join(missing))
    return "\n".join(lines)


def _parse_rule_time(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.split(":", 1)
    return int(hour_text), int(minute_text)


def _weekday_label(day: int) -> str:
    return ["一", "二", "三", "四", "五", "六", "日"][day]


def _parse_chinese_number(value: str | None) -> int:
    return _parse_number(value)


def _parse_number(value: str | None) -> int:
    if not value:
        return 0
    value = value.strip().replace("個", "")
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "百" in value:
        left, _, right = value.partition("百")
        hundreds = digits.get(left, 1) if left else 1
        return hundreds * 100 + (_parse_number(right) if right else 0)
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for char in value:
        total = total * 10 + digits.get(char, 0)
    return total


def _parse_duration_amount(value: str) -> float:
    text = " ".join(value.replace("個", " 個 ").split()).replace(" ", "")
    if text == "半":
        return 0.5
    if text.endswith("半"):
        base = text[:-1].replace("個", "")
        return _parse_number(base) + 0.5
    if "個半" in text:
        return _parse_number(text.replace("個半", "")) + 0.5
    return float(_parse_number(text.replace("個", "")))


def _next_interval_occurrence(start_date: date, interval_days: int, hour: int, minute: int, after: datetime) -> datetime:
    candidate = datetime.combine(start_date, time(hour, minute))
    if candidate > after:
        return candidate
    elapsed_days = max(0, (after.date() - start_date).days)
    jumps = elapsed_days // interval_days
    candidate = datetime.combine(start_date + timedelta(days=jumps * interval_days), time(hour, minute))
    if candidate <= after:
        candidate += timedelta(days=interval_days)
    return candidate


def _next_cycle_occurrence(rule: dict[str, object], hour: int, minute: int, after: datetime) -> datetime | None:
    start_date = date.fromisoformat(str(rule["start_date"]))
    active_days = int(rule.get("active_days") or 0)
    break_days = int(rule.get("break_days") or 0)
    repeat = bool(rule.get("repeat"))
    interval_days = int(rule.get("interval_days") or 1)
    weekdays = [int(day) for day in (rule.get("weekdays") or [])]
    if active_days <= 0:
        return None

    day = after.date()
    if day < start_date:
        day = start_date
    max_days = 3660 if repeat else active_days + 1
    for offset in range(max_days):
        current = day + timedelta(days=offset)
        if current < start_date:
            continue
        if _is_active_cycle_day(current, start_date, active_days, break_days, repeat):
            active_index = _active_index(current, start_date, active_days, break_days, repeat)
            if active_index is not None and active_index % interval_days == 0:
                if not weekdays or current.weekday() in weekdays:
                    candidate = datetime.combine(current, time(hour, minute))
                    if candidate > after:
                        return candidate
    return None


def _cycle_due_on_or_before(rule: dict[str, object], hour: int, minute: int, at: datetime) -> datetime | None:
    start_date = date.fromisoformat(str(rule["start_date"]))
    active_days = int(rule.get("active_days") or 0)
    break_days = int(rule.get("break_days") or 0)
    repeat = bool(rule.get("repeat"))
    interval_days = int(rule.get("interval_days") or 1)
    weekdays = [int(day) for day in (rule.get("weekdays") or [])]
    if active_days <= 0:
        return None

    current = at.date()
    min_date = start_date
    max_back = max(3660, active_days + break_days + 1) if repeat else active_days + 1
    for offset in range(max_back):
        day = current - timedelta(days=offset)
        if day < min_date:
            return None
        if not _is_active_cycle_day(day, start_date, active_days, break_days, repeat):
            continue
        active_index = _active_index(day, start_date, active_days, break_days, repeat)
        if active_index is None or active_index % interval_days != 0:
            continue
        if weekdays and day.weekday() not in weekdays:
            continue
        candidate = datetime.combine(day, time(hour, minute))
        if candidate <= at:
            return candidate
    return None


def _cycle_metadata(rule: dict[str, object], current: date) -> dict[str, int] | None:
    start_date = date.fromisoformat(str(rule["start_date"]))
    active_days = int(rule.get("active_days") or 0)
    break_days = int(rule.get("break_days") or 0)
    repeat = bool(rule.get("repeat"))
    if active_days <= 0:
        return None
    day_offset = (current - start_date).days
    if day_offset < 0:
        return None
    if not repeat:
        if day_offset >= active_days:
            return None
        return {
            "cycle_index": 1,
            "occurrence_index": day_offset + 1,
            "position_in_cycle": day_offset,
            "active_days": active_days,
        }
    cycle_length = active_days + break_days
    if cycle_length <= 0:
        return None
    position = day_offset % cycle_length
    if position >= active_days:
        return None
    return {
        "cycle_index": day_offset // cycle_length + 1,
        "occurrence_index": position + 1,
        "position_in_cycle": position,
        "active_days": active_days,
    }


def _cycle_status_for_date(rule: dict[str, object], current: date) -> str | None:
    start_date = date.fromisoformat(str(rule["start_date"]))
    active_days = int(rule.get("active_days") or 0)
    break_days = int(rule.get("break_days") or 0)
    repeat = bool(rule.get("repeat"))
    if active_days <= 0:
        return ""

    day_offset = (current - start_date).days
    if day_offset < 0:
        return None

    if not repeat:
        if day_offset < active_days:
            return f"第 {day_offset + 1}/{active_days} 天"
        return "已完成週期"

    cycle_length = active_days + break_days
    if cycle_length <= 0:
        return ""
    position = day_offset % cycle_length
    cycle_no = day_offset // cycle_length + 1
    if position < active_days:
        return f"第 {cycle_no} 輪第 {position + 1}/{active_days} 天"
    break_pos = position - active_days + 1
    next_start = current + timedelta(days=(cycle_length - position))
    return f"休息期第 {break_pos}/{break_days} 天｜下一輪開始：{next_start.strftime('%m/%d')} {rule.get('time', '')}"


def _is_active_cycle_day(current: date, start_date: date, active_days: int, break_days: int, repeat: bool) -> bool:
    elapsed = (current - start_date).days
    if elapsed < 0:
        return False
    if not repeat:
        return elapsed < active_days
    cycle_len = active_days + break_days
    if cycle_len <= 0:
        return False
    return (elapsed % cycle_len) < active_days


def _active_index(current: date, start_date: date, active_days: int, break_days: int, repeat: bool) -> int | None:
    elapsed = (current - start_date).days
    if elapsed < 0:
        return None
    if not repeat:
        return elapsed if elapsed < active_days else None
    cycle_len = active_days + break_days
    cycle = elapsed // cycle_len
    pos = elapsed % cycle_len
    if pos >= active_days:
        return None
    return cycle * active_days + pos
