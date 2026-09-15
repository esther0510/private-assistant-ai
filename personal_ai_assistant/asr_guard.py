"""Fail-closed speech and decoder gates. Never retain microphone audio."""
import math
import re


def speech_presence(pcm, sample_rate=16000):
    from .post_wake import SpeechEndpoint
    if sample_rate != 16000:
        raise RuntimeError('語音取樣率不支援')
    endpoint = SpeechEndpoint()
    for offset in range(0, len(pcm)-959, 960):
        endpoint.feed(pcm[offset:offset+960])
    detected = endpoint.speech_detected
    return dict(speech_detected=detected, speech_confidence=endpoint.voiced_ms / max(30, endpoint.elapsed_ms),
                discard_reason='' if detected else 'no_speech')


def prompt_echo(text, candidates=(), speech_confidence=0):
    parts = [p.strip().casefold() for p in re.split(r'[,，、;；\n]+', text) if p.strip()]
    names = {str(n).strip().casefold() for n in candidates}
    overlap = sum(p in names for p in parts) / max(1, len(parts))
    # An almost verbatim candidate list is unsafe even if a noise VAD fired.
    return (len(parts) >= 2 and overlap >= .8) or (speech_confidence < .5 and len(parts) >= 4 and
            (overlap >= .5 or all(re.search(r'[a-zA-Z]', p) for p in parts)))


def assess_segments(segments, candidates=(), speech_confidence=0):
    text = ''.join(s.text for s in segments).strip()
    probabilities = [float(s.no_speech_prob) for s in segments if isinstance(getattr(s, 'no_speech_prob', None), (float, int))]
    logs = [float(s.avg_logprob) for s in segments if isinstance(getattr(s, 'avg_logprob', None), (float, int))]
    ratios = [float(s.compression_ratio) for s in segments if isinstance(getattr(s, 'compression_ratio', None), (float, int))]
    confidence = math.exp(min(0, min(logs))) if logs else None
    reason = ''
    if prompt_echo(text, candidates, speech_confidence):
        reason = 'prompt_echo'
    elif not text or any(not math.isfinite(p) or p >= .6 for p in probabilities):
        reason = 'no_speech'
    elif any(not math.isfinite(v) or v < -1 for v in logs) or any(not math.isfinite(v) or v > 2.4 for v in ratios):
        reason = 'low_confidence'
    return ('' if reason else text), dict(speech_detected=True, stt_confidence=confidence,
        no_speech_prob=max(probabilities) if probabilities else None, discard_reason=reason)
