"""Bounded numeric-only wake calibration; no audio is persisted."""
from array import array
from collections import deque
import json
import math
from pathlib import Path


def clamp(value, low, high):
    return min(high, max(low, value))


KWS_THRESHOLDS = {"low": .12, "standard": .11, "high": .10, "auto": .11}


class AdaptiveWakeSensitivity:
    def __init__(self, mode='auto', path=None):
        self.mode, self.path = mode, Path(path) if path else None
        self.noise_floor, self.speech_level = .001, .025
        self.successes = 0
        self.gain = 1.
        self.level = 0.
        self.speech = False
        self._noise = deque(maxlen=200)
        self._speech = deque(maxlen=100)
        if self.path:
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                for key, low, high in [('noise_floor', .0001, .03), ('speech_level', .002, .3), ('successes', 0, 10000)]:
                    value = float(data[key])
                    if math.isfinite(value):
                        setattr(self, key, clamp(value, low, high))
            except (OSError, ValueError, TypeError, KeyError):
                pass

    @property
    def vad_threshold(self):
        ratio = {'low': 3.2, 'standard': 2.4, 'high': 1.65, 'auto': 1.8}.get(self.mode, 2.4)
        floor = {'low': .006, 'standard': .004, 'high': .0018, 'auto': .002}.get(self.mode, .004)
        return clamp(max(floor, self.noise_floor * ratio), floor, .08)

    @property
    def kws_threshold(self):
        # Stable for the service lifetime: VAD/noise learning must not reset KWS.
        return KWS_THRESHOLDS.get(self.mode, .11)

    def process(self, pcm, vad):
        samples = array('h', pcm)
        self.level = math.sqrt(sum(float(x)*x for x in samples) / max(1, len(samples))) / 32768
        # Input is already processed by the shared microphone preamp.
        normalized = pcm
        self.gain = 1.
        native_speech = bool(vad.is_speech(normalized, 16000))
        self.speech = native_speech and self.level >= self.vad_threshold
        if self.speech:
            self._speech.append(self.level)
        elif not native_speech:
            self._noise.append(self.level)
            # Lower quantile resists brief transients and avoids learning speech as noise.
            estimate = sorted(self._noise)[len(self._noise)//4]
            self.noise_floor = clamp(.95*self.noise_floor + .05*estimate, .0001, .03)
        return normalized, self.speech

    def successful_wake(self):
        if self._speech:
            level = sorted(self._speech)[len(self._speech)//2]
            self.speech_level = clamp(.9*self.speech_level + .1*level, .002, .3)
            self.successes = min(10000, self.successes + 1)

    def save(self):
        data = {k: float(getattr(self, k)) for k in ('noise_floor', 'speech_level', 'successes')}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data), encoding='utf-8')
            temporary.replace(self.path)
        return data

    def calibration_result(self):
        if not self._speech:
            return '未偵測到可靠人聲；請確認麥克風後再試。未更新校準。'
        level = sorted(self._speech)[len(self._speech)//2]
        if self.mode == 'auto':
            self.speech_level = clamp(level, .002, .3)
            self.save()
        volume = '低' if level < .012 else '高' if level > .15 else '正常'
        noise = '低' if self.noise_floor < .004 else '中' if self.noise_floor < .012 else '高'
        suggestion = '高' if level < .012 and self.noise_floor < .004 else '標準' if noise == '高' else '自動'
        return f'麥克風音量：{volume}\n環境噪音：{noise}\n建議靈敏度：{suggestion}'
