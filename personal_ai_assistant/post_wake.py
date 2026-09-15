"""Short-silence endpoint for post-wake recording; never persists microphone audio."""
class SpeechEndpoint:
    def __init__(self, vad=None, silence_ms=750, minimum_speech_ms=150, grace_ms=1000):
        from .vad import create_vad
        self.vad = vad if vad is not None else create_vad(3)
        self.silence_ms = silence_ms
        self.minimum_speech_ms = minimum_speech_ms
        self.voiced_ms = 0
        self.quiet_ms = 0
        self.ended = False
        self.elapsed_ms = 0
        self.grace_ms = grace_ms
        self.speech_revision = 0
        self.consecutive_ms = 0
        self.speech_detected = False

    def feed(self, frame):
        self.elapsed_ms += 30
        if self.vad.is_speech(frame, 16000):
            self.consecutive_ms += 30
            self.speech_detected |= self.consecutive_ms >= self.minimum_speech_ms
            self.speech_revision += 1
            self.voiced_ms += 30
            self.quiet_ms = 0
        else:
            self.consecutive_ms = 0
            self.quiet_ms += 30
        self.ended = (self.elapsed_ms >= self.grace_ms and
                      self.speech_detected and self.quiet_ms >= self.silence_ms)
        return self.ended


def capture_post_wake(provider, transcribe, aliases, timeout_seconds=8, cancelled=lambda: False, mark=lambda *a: None, executor=None):
    """Consume the existing ring buffer while KWS's microphone keeps capturing.

    Acoustic EOS proposes a snapshot; only semantic EOS finalizes it. Recognition
    runs off-thread so speech arriving during STT invalidates that snapshot.
    The KWS detector, thresholds and microphone callback are untouched.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor
    from .voice_text import command_transcript_state
    endpoint = SpeechEndpoint()
    wake_at = provider.last_wake_at or time.monotonic()
    deadline = wake_at + timeout_seconds
    with provider._lock:
        boundary = max(0, len(provider._command) - int(max(0, time.monotonic()-wake_at)*32000))
    capture_stop = provider._stop
    offset = 0
    future = None
    revision = -1
    checked = -1
    state = 'awaiting_command'
    last_text = ''
    continuation_until = 0
    proposed_eos = wake_at
    proposed_speech_end = wake_at
    pool = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='command-stt')
    try:
        while not capture_stop.is_set() and not cancelled():
            now = time.monotonic()
            with provider._lock:
                buffered = bytes(provider._command)
            buffered = buffered[:boundary + int(timeout_seconds * 32000)]
            while offset + 960 <= len(buffered):
                frame = buffered[offset:offset+960]
                offset += 960
                if offset > boundary:
                    endpoint.feed(frame)
            if future is not None and future.done():
                last_text = future.result()
                mark('stt_done')
                future = None
                checked = revision
                state, normalized = command_transcript_state(last_text, aliases)
                if state != 'awaiting_command':
                    mark('speech_onset', wake_at + max(0, (offset-boundary)/32000 - (endpoint.quiet_ms+endpoint.voiced_ms)/1000))
                if state == 'awaiting_target':
                    continuation_until = now + .8
                if state == 'complete' and revision == endpoint.speech_revision and endpoint.ended and now-wake_at >= 1:
                    mark('end_of_speech_detected', proposed_eos)
                    mark('speech_ended_at', proposed_speech_end)
                    return last_text
            if now >= deadline + 5:
                raise TimeoutError('語音辨識逾時')
            expired = now >= deadline
            if future is None and endpoint.speech_revision != checked and endpoint.speech_detected and (expired or (endpoint.ended and now-wake_at >= 1)):
                revision = endpoint.speech_revision
                proposed_eos = now
                proposed_speech_end = now-endpoint.quiet_ms/1000
                mark('stt_start')
                future = pool.submit(transcribe, buffered[:offset])
            if future is None and state == 'awaiting_target' and now >= continuation_until and checked == endpoint.speech_revision:
                mark('end_of_speech_detected')
                return last_text
            if expired and future is None:
                mark('end_of_speech_detected')
                return last_text if state != 'awaiting_command' else ''
            provider._stop.wait(.015)
        return ''
    finally:
        if future is not None:
            future.cancel()
        if executor is None:
            pool.shutdown(wait=False, cancel_futures=True)


def record_command(microphone_id, timeout_seconds=8, cancelled=lambda: False, gain_mode="auto"):
    from .microphone_gain import MicrophoneGain
    preamp = MicrophoneGain(gain_mode)
    import queue
    import time
    from .voice_command import _sounddevice_module
    from .audio_devices import sounddevice_index_from_identifier
    frames = queue.Queue(maxsize=300)
    def callback(indata, _frames, _timing, _status):
        try:
            frames.put_nowait(bytes(indata))
        except queue.Full:
            pass
    endpoint = SpeechEndpoint()
    chunks = []
    with _sounddevice_module().RawInputStream(samplerate=16000, blocksize=480, channels=1,
            dtype='int16', device=sounddevice_index_from_identifier(microphone_id), callback=callback):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not cancelled():
            try:
                frame = frames.get(timeout=min(.05, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            frame = preamp.process(frame)
            chunks.append(frame)
            if endpoint.feed(frame):
                break
    return b'' if cancelled() or not endpoint.speech_detected else b''.join(chunks)
