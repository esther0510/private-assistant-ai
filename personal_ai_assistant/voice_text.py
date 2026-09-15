from .reminder_trace import trace_reminder
"""Voice-only preparation and expiring diagnostics; optional DEBUG trace contains text only."""
import re
import time
import logging
import json
import unicodedata


def normalize_voice(text, aliases=()):
    raw = str(text or '')
    value = unicodedata.normalize('NFKC', raw).strip()
    value = re.sub(r'[，,。！!？?、；;]', ' ', value)
    value = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', value)
    value = re.sub(r'^(?:(?:欸|嗯|呃|那個|嘿|hey)\s*)+', '', value, flags=re.I)
    names = list(aliases)
    if any(re.search('[賈贾甲]維|[賈贾甲]维|jarvis', a, re.I) for a in names):
        names += ['賈維斯','賈維思','甲維斯','贾维斯','賈威斯','賈維絲','甲維思','假尾思','假如是','假維斯','Jarvis']
    removed = ''
    for name in sorted(names, key=len, reverse=True):
        pattern = r'^' + r'\s*'.join(re.escape(c) for c in name if not c.isspace())
        match = re.match(pattern, value, re.I) if name.strip() else None
        if match:
            removed = match.group()
            value = value[match.end():].strip()
            break
    value = re.sub(r'^(?:(?:可以幫我|可不可以幫我|能不能幫我|麻煩你|麻煩|請你|請|幫我|我要|我想要|我想|想要|欸|嗯|呃|那個)\s*)+', '', value)
    value = re.sub(r'^(打開|開啟|切換到|切到|開)(?:\s*\1)+', r'\1', value)
    # Preserve already recognizable non-launch commands (goals/reminders can mention opening).
    from .command_router import route_assistant_command
    if route_assistant_command(value).intent == 'unknown':
        match = re.search(r'打開|開啟|切換到|切到|幫我看|啟動|開', value)
        if match:
            prefix = value[:match.start()]
            # Negation is not wake residue.
            if not re.search(r'不要|別|不能|不想', prefix):
                value = value[match.start():]
                value = re.sub(r'^幫我看', '打開 ', value)
    # Narrow, launch-shaped STT substitutions; never fuzzy-match arbitrary actions.
    match = re.match(r'^(打凱|打凯|打慨|打楷|開起|开启|打开|切換道|切到)\s*(.+)$', value)
    fuzzy = bool(match)
    if match:
        value = '打開 ' + match.group(2)
    value = re.sub(r'^(?:開啟|切換到|切到|啟動|開|看)\s*', '打開 ', value)
    value = re.sub(r'^打開\s*', '打開 ', value)
    value = re.sub(r'\s+', ' ', value)
    return dict(raw=raw[:2000], normalized=value.strip()[:2000], wake_removed=bool(removed), wake_prefix=removed, fuzzy_intent=fuzzy)


def command_transcript_state(text, aliases=('賈維斯',)):
    """Semantic endpoint check, exclusively after an accepted wake event."""
    prepared = normalize_voice(text, aliases)
    value = prepared['normalized']
    for _ in range(2):
        value = normalize_voice(value, aliases)['normalized']
    if not value:
        return 'awaiting_command', value
    routed = route_voice_command(value)
    if routed.intent == 'activate_or_launch_app' and not routed.target:
        return 'awaiting_target', value
    return 'complete', value


class VoiceDiagnostics:
    def __init__(self):
        self.clear()
    def clear(self):
        self.data = {}
        self.expires = 0
    def begin(self, data):
        self.data = dict(data)
        self.expires = time.monotonic() + 600
    def mark(self, stage, at=None):
        if time.monotonic() < self.expires:
            self.data.setdefault('timing', {})[stage] = time.monotonic() if at is None else at
            self.summarize()
    def summarize(self):
        timing = self.data.get('timing', {})
        pairs = [('recording', 'recording_start', 'end_of_speech_detected'),
                 ('stt', 'stt_start', 'stt_done'), ('normalize', 'stt_done', 'normalize_done'),
                 ('intent', 'normalize_done', 'intent_done'), ('resolver', 'intent_done', 'resolver_done'),
                 ('action', 'action_start', 'action_done'), ('total', 'wake_event_at', 'action_done'),
                 ('after_eos', 'end_of_speech_detected', 'action_done'),
                 ('after_speech', 'speech_ended_at', 'action_done')]
        self.data['latency_ms'] = {name: round(max(0, timing[end]-timing[start])*1000, 2)
                                   for name, start, end in pairs if start in timing and end in timing}
    def log(self):
        # JSON escapes control characters; bounded text only, never PCM/model objects.
        logging.getLogger(__name__).debug('post-wake trace %s', json.dumps(self.data, ensure_ascii=False))
    def update(self, **values):
        if time.monotonic() < self.expires:
            self.data.update(values)
            self.summarize()
    def snapshot(self):
        if time.monotonic() >= self.expires:
            self.clear()
        return dict(self.data)


def route_voice_command(text, **kwargs):
    from .command_router import route_assistant_command, _intent
    trace_reminder('voice_input', raw_text=text)
    text = normalize_voice(text)['normalized']
    trace_reminder('voice_normalized', raw_text=text)
    if text.strip() == '打開':
        return _intent('activate_or_launch_app', text, .95)
    return route_assistant_command(text, **kwargs)
