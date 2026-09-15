"""Shared 16 kHz PCM preamp. Only numeric levels are retained."""
from array import array
from collections import deque
from statistics import median
import math


def rms(pcm):
    samples = array('h', pcm)
    return math.sqrt(sum(float(x)*x for x in samples) / max(1, len(samples))) / 32768


def db(level):
    return 20 * math.log10(max(level, 1e-6))


class MicrophoneGain:
    target_db = -24.0
    max_gain_db = 18.0
    ceiling = int(32767 * 10 ** (-1 / 20))

    def __init__(self, mode='auto'):
        self.mode = mode
        self.gain_db = 18.0
        self.noise_floor = .001
        self.raw_level = self.processed_level = 0.0
        self._applied = 1.0
        self.measurements = deque(maxlen=200)

    def process(self, pcm):
        self.raw_level = rms(pcm)
        if self.mode == 'off' or not pcm:
            self.processed_level = self.raw_level
            if self.raw_level > .002:
                self.measurements.append((self.raw_level, self.raw_level))
            self._applied = 1.0
            return pcm
        samples = array('h', pcm)
        seconds = len(samples) / 16000
        # Energy evidence is independent of downstream VAD: a false VAD must
        # not prevent quiet speech from reaching the continuous decoder.
        floor = max(.002, self.noise_floor * 3)
        speech_like = self.raw_level > floor
        if speech_like:
            desired = min(self.max_gain_db, max(0., self.target_db-db(self.raw_level)))
            # At most 0.6 dB/s up and 1 dB/s down; limiter handles transients.
            self.gain_db += max(-seconds, min(.6*seconds, desired-self.gain_db))
        else:
            alpha = 1-math.exp(-seconds/3)
            self.noise_floor += alpha*(min(self.raw_level, .01)-self.noise_floor)
        # Low noise remains unity, with a soft transition near the speech floor.
        confidence = min(1., max(0., (self.raw_level-floor*.5)/(floor*.5)))
        gain = 1 + confidence*(10**(self.gain_db/20)-1)
        peak = max((abs(x) for x in samples), default=0)
        cap = self.ceiling/max(1, peak)
        start, end = min(self._applied, cap), min(gain, cap)
        out = array('h', (int(x*(start+(end-start)*(i+1)/len(samples))) for i,x in enumerate(samples))).tobytes()
        self._applied = end
        self.processed_level = rms(out)
        if speech_like:
            self.measurements.append((self.raw_level, self.processed_level))
        return out

    def levels(self):
        raw, processed = (tuple(median(v) for v in zip(*self.measurements))
                          if self.measurements else (self.raw_level, self.processed_level))
        return f'原始 RMS：{db(raw):.1f} dBFS → 處理後 RMS：{db(processed):.1f} dBFS'
