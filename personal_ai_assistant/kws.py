"""Offline, text-defined streaming KWS. No enrollment or ASR in the wake gate.

Optional imports stay inside methods so the text UI needs no voice dependencies.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import re
import shutil
import tarfile
import tempfile
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path

from .storage import default_data_dir

logger = logging.getLogger(__name__)

MODEL = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL}.tar.bz2"
MODEL_FILES = (
    "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
    "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    "tokens.txt", "en.phone",
)
UNSUPPORTED = "此名稱無法建立本機喚醒詞，請換一個名稱或使用按鍵喚醒"


class UnsupportedKeyword(ValueError):
    pass


class PcmRingBuffer:
    def __init__(self, seconds=0.5, sample_rate=16000):
        self.capacity = int(seconds * sample_rate) * 2
        self.data = bytearray()

    def append(self, pcm):
        self.data.extend(pcm)
        del self.data[:max(0, len(self.data) - self.capacity)]

    def snapshot(self):
        return bytes(self.data)

    def clear(self):
        self.data.clear()


def model_directory():
    return default_data_dir() / "models" / MODEL


def ensure_model(root=None, progress=None, cancelled=None):
    root = Path(root) if root else model_directory()
    progress = progress or (lambda message: None)
    cancelled = cancelled or (lambda: False)
    if all((root / name).is_file() for name in MODEL_FILES):
        return root
    root.parent.mkdir(parents=True, exist_ok=True)
    # Download/extract outside the UI thread; publish only complete files.
    with tempfile.TemporaryDirectory(prefix="kws-", dir=root.parent) as tmp:
        archive = Path(tmp) / "model.tar.bz2"
        request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "PrivateAssistantAI"})
        with urllib.request.urlopen(request, timeout=30) as response, archive.open("wb") as out:
            total = int(response.headers.get("Content-Length", 0))
            received = 0
            while True:
                if cancelled():
                    raise InterruptedError("KWS setup cancelled")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                received += len(chunk)
                percent = f" {received * 100 // total}%" if total else f" {received // 1024} KB"
                progress("正在下載本機喚醒模型" + percent)
        with tarfile.open(archive, "r:bz2") as bundle:
            staged = Path(tmp) / "files"
            staged.mkdir()
            for name in MODEL_FILES:
                member = bundle.getmember(f"{MODEL}/{name}")
                if not member.isfile() or member.size > 32 * 1024 * 1024:
                    raise ValueError("Invalid KWS model archive")
                with bundle.extractfile(member) as src, (staged / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            if cancelled():
                raise InterruptedError("KWS setup cancelled")
            root.mkdir(exist_ok=True)
            for name in MODEL_FILES:
                (staged / name).replace(root / name)
    return root


class SherpaKeywordEngine:
    """Provider-specific normalization, validated phone tokens, and CPU inference."""
    provider_name = "sherpa-onnx / zh-en Zipformer 3M (CPU int8)"

    def __init__(self, root=None):
        self.root = Path(root) if root else model_directory()
        self.spotter = None
        self.stream = None
        self.compiled = ()
        self.threshold = 0.11
        self._pending_threshold = None

    def compile(self, aliases):
        from opencc import OpenCC
        from sherpa_onnx.utils import text2token
        converter = OpenCC("t2s")
        vocabulary = {line.split()[0] for line in (self.root / "tokens.txt").read_text(encoding="utf-8").splitlines()}
        compiled = []
        for alias in aliases:
            normalized = converter.convert(unicodedata.normalize("NFKC", alias)).strip()
            words = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]+(?:'[A-Za-z]+)?", normalized)
            if not words or re.sub(r"\s+", "", normalized) != "".join(words):
                raise UnsupportedKeyword(UNSUPPORTED)
            normalized = " ".join(word.upper() if word.isascii() else word for word in words)
            # Invoke one name at a time: sherpa skips OOV English words, which must
            # never silently remove a requested wake name or shift alias indices.
            result = text2token([normalized], str(self.root / "tokens.txt"),
                                tokens_type="phone+ppinyin", lexicon=str(self.root / "en.phone"))
            if len(result) != 1 or not result[0] or any(t not in vocabulary or t.startswith("<") for t in result[0]):
                raise UnsupportedKeyword(UNSUPPORTED)
            label = "_".join(unicodedata.normalize("NFKC", alias).split())
            compiled.append(" ".join(result[0]) + " @" + label)
        if not compiled:
            raise UnsupportedKeyword(UNSUPPORTED)
        self.compiled = tuple(compiled)
        logger.debug("KWS compiled keywords: %s", self.compiled)
        return self.compiled

    def configure(self, aliases, sensitivity="standard"):
        import sherpa_onnx
        compiled = self.compile(aliases)
        cache = self.root / "keywords"
        cache.mkdir(exist_ok=True)
        digest = hashlib.sha256("\n".join(compiled).encode()).hexdigest()
        keyword_file = cache / (digest + ".txt")
        if not keyword_file.exists():
            keyword_file.write_text("\n".join(compiled) + "\n", encoding="utf-8")
        from .wake_sensitivity import KWS_THRESHOLDS
        self.threshold = KWS_THRESHOLDS.get(sensitivity, 0.11)
        self._pending_threshold = None
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(self.root / "tokens.txt"), encoder=str(self.root / MODEL_FILES[0]),
            decoder=str(self.root / MODEL_FILES[1]), joiner=str(self.root / MODEL_FILES[2]),
            keywords_file=str(keyword_file), num_threads=1, provider="cpu",
            keywords_threshold=self.threshold, keywords_score=3.0,
            num_trailing_blanks=1,
        )
        self.reset(reason="configuration/service start")

    def set_threshold(self, threshold):
        # Queue explicit changes; never discard an in-flight keyword decoder.
        threshold = float(threshold)
        if not 0 < threshold < 1:
            raise ValueError("KWS threshold must be between 0 and 1")
        self._pending_threshold = threshold if threshold != self.threshold else None

    def reset(self, reason="explicit configuration/service restart"):
        if self._pending_threshold is not None:
            self.threshold = self._pending_threshold
            self._pending_threshold = None
        keywords = '\n'.join(line.replace(' @', f' #{self.threshold:.3f} @') for line in self.compiled)
        self.stream = self.spotter.create_stream(keywords) if self.spotter else None
        logger.debug("KWS stream created: reason=%s threshold=%.3f score=3.0", reason, self.threshold)

    def accept(self, pcm):
        import numpy as np
        self.stream.accept_waveform(16000, np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0)
        found = ""
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
            result = self.spotter.get_result(self.stream)
            if result:
                found = result
                logger.debug("KWS hit alias=%s; stream reset reason=keyword hit", result)
                if self._pending_threshold is not None:
                    self.reset(reason="keyword hit / pending threshold")
                else:
                    self.spotter.reset_stream(self.stream)
        return found


class StreamingKwsWakeWordProvider:
    """Continuous standby KWS, bounded command history and one-shot handoff."""
    provider_name = SherpaKeywordEngine.provider_name

    def __init__(self, status_callback=None, engine_factory=SherpaKeywordEngine):
        from .voice_command import WakeRuntimeState, WakeSettings
        self.settings = WakeSettings()
        self.runtime_state = WakeRuntimeState()
        self.status_callback = status_callback or (lambda message: None)
        self.engine_factory = engine_factory
        self.engine = None
        self._thread = None
        self._restart_requested = False
        self._stop = threading.Event()
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._mic = None
        self._queue = queue.Queue(maxsize=100)
        self.ring = PcmRingBuffer(0.5)
        self._history = PcmRingBuffer(3.0)
        self._command = bytearray()
        self._latched = False
        self._speaking = False
        self._silence = 0
        self.last_wake_at = -float("inf")
        self.last_keyword = ""
        self._configured = None
        from .wake_sensitivity import AdaptiveWakeSensitivity
        self.sensitivity = AdaptiveWakeSensitivity()
        from .microphone_gain import MicrophoneGain
        self.preamp = MicrophoneGain()
        self._calibrating = False
        self._fed_frames = 0
        self.checks = dict.fromkeys(("Dependencies", "KWS", "Mic", "Pipeline"), False)

    def _state(self, state, message):
        from .voice_command import WakeRuntimeState
        self.runtime_state = WakeRuntimeState(state, message)

    def configure_settings(self, settings):
        if settings != self.settings:
            self.stop()
            self.settings = settings
            from .microphone_gain import MicrophoneGain
            self.preamp = MicrophoneGain(settings.microphone_gain)
            from .wake_sensitivity import AdaptiveWakeSensitivity
            device = hashlib.sha256(settings.microphone_id.encode()).hexdigest()[:16]
            self.sensitivity = AdaptiveWakeSensitivity(settings.sensitivity,
                default_data_dir() / ('wake_calibration_' + device + '.json'))

    def configure(self, aliases, microphone_id=""):
        from .voice_command import WakeSettings
        self.configure_settings(WakeSettings(assistant_name=aliases[0] if aliases else "",
                                            wake_aliases=tuple(aliases[1:]), microphone_id=microphone_id))

    def worker_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        with self._lock:
            return self._start_locked()

    def _start_locked(self):
        # A cancelled setup may still be unwinding. Never overlap its worker.
        if self.worker_alive():
            if self._stop.is_set():
                self._restart_requested = True
                self._state("starting", "等待舊語音串流關閉後重新啟動")
            return self.runtime_state
        self._restart_requested = False
        self._queue = queue.Queue(maxsize=100)
        self.ring.clear()
        self._history.clear()
        self._command.clear()
        self._latched = self._speaking = False
        self._silence = 0
        self.checks = dict.fromkeys(self.checks, False)
        self._stop = threading.Event()
        self._event.clear()
        self._state("starting", "正在建立喚醒詞")
        self._thread = threading.Thread(target=self._worker, args=(self._stop,), name="streaming-kws", daemon=True)
        self._thread.start()
        return self.runtime_state

    @staticmethod
    def _close_mic(mic):
        # Closing must still happen if a disconnected device rejects abort.
        for operation in (mic.abort, mic.close):
            try:
                operation()
            except Exception:
                logger.debug("KWS stream shutdown operation failed", exc_info=True)

    def _worker(self, stop):
        try:
            self._run(stop)
        finally:
            with self._lock:
                self.engine = None
                self._configured = None
                self._thread = None
                if self._restart_requested:
                    logger.debug("KWS restarting after previous worker shutdown")
                    self._start_locked()

    def stop(self):
        with self._lock:
            self._restart_requested = False
            self._stop.set()
            worker = self._thread
            mic, self._mic = self._mic, None
            if mic is not None:
                self._close_mic(mic)
            self._event.clear()
            self._command.clear()
            self.ring.clear()
            self._history.clear()
            self._latched = False
            self._speaking = False
            self._silence = 0
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._state("disabled", "語音助理未啟用")
        if worker and worker is not threading.current_thread():
            worker.join(timeout=0.5)

    def begin_command(self):
        with self._lock:
            self._command.clear()
            self._event.clear()
            self.last_wake_at = time.monotonic()
            self._latched = True
            self._state('woken', '正在聽…')

    def rearm(self):
        # Discard the consumed event and its audio, while retaining the mic worker.
        with self._lock:
            self._event.clear()
            self._command.clear()
            self.ring.clear()
            self._history.clear()
            self._speaking = False
            self._silence = 0
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self.last_wake_at = -float('inf')
            self._latched = False
            if self._thread and self._thread.is_alive() and self._mic is not None and not self._stop.is_set():
                self._state('standby', '語音助理：待機中')
                return self.runtime_state
        return self.start()

    def poll(self):
        if self._stop.is_set() or not self._event.is_set():
            return False
        self._event.clear()
        return True

    def process_microphone_frame(self, raw, vad):
        processed = self.preamp.process(raw)
        _, speech = self.sensitivity.process(processed, vad)
        self.consume(processed, speech)
        return processed

    def consume(self, pcm, speech, now=None, kws_pcm=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._stop.is_set():
                return
            pcm = kws_pcm if kws_pcm is not None else pcm
            self._history.append(pcm)
            if self._latched:
                self._command.extend(pcm)
                del self._command[16000 * 2 * 15:]
                return
            # Speech/VAD and pre-roll never gate or replay decoder input.
            audio = kws_pcm if kws_pcm is not None else pcm
            self.ring.append(audio)
            self._fed_frames += 1
            if self._fed_frames == 1 or self._fed_frames % 1000 == 0:
                logger.debug("KWS feed frames=%d bytes=%d", self._fed_frames, len(audio))
            result = self.engine.accept(audio)
            if result and self._calibrating:
                self.sensitivity.successful_wake()
                return
            if result and now - self.last_wake_at >= 0.75:
                self.sensitivity.successful_wake()
                try:
                    self.sensitivity.save()
                except OSError:
                    pass
                self.last_wake_at = now
                self.last_keyword = result
                self._latched = True
                # Keep the wake and any command syllables already consumed by KWS.
                self._command = bytearray(self._history.snapshot())
                self._event.set()
                self._state("woken", f"{self.settings.assistant_name}正在聽…")

    def _run(self, stop):
        mic = None
        stage = "error_dependency"
        try:
            from .voice_command import _sounddevice_module, _webrtcvad_module
            from .audio_devices import sounddevice_index_from_identifier
            from .voice_command import wake_aliases_for_settings
            self._state("verifying", "正在驗證本機語音元件…")
            from .voice_command import detect_voice_components
            from .vad import create_vad
            verified = detect_voice_components()
            if verified.state != "ready":
                self._state(verified.state if verified.state in {"missing_components", "error_dependency"} else "error_dependency", verified.message)
                return
            vad = create_vad()
            self.checks["Dependencies"] = True
            stage = "error_model"
            self._state("loading_models", "KWS 模型載入中；STT 模型於喚醒後載入")
            # Check runtime before downloading, so dependency errors are explicit.
            import sherpa_onnx  # noqa: F401
            aliases = wake_aliases_for_settings(self.settings)
            signature = (aliases, self.settings.sensitivity)
            if signature != self._configured:
                engine = self.engine_factory()
                ensure_model(engine.root, lambda msg: None if stop.is_set() else self._state("loading_models", msg), stop.is_set)
                engine.configure(aliases, self.settings.sensitivity)
                if stop.is_set():
                    return
                self.engine = engine
                self._configured = signature
            self.checks["KWS"] = True
            if stop.is_set():
                return
            stage = "error_microphone"
            self._state("opening_microphone", "正在開啟選定麥克風…")
            audio_queue = self._queue
            callback_count = 0
            sample_count = 0
            logged_callbacks = 0
            def callback(indata, frames, timing, status):
                nonlocal callback_count, sample_count
                if stop.is_set():
                    return
                try:
                    audio_queue.put_nowait(bytes(indata))
                    callback_count += 1
                    sample_count += frames
                except queue.Full:
                    self._state("error", "麥克風異常：音訊處理超時")
                    stop.set()
            with self._lock:
                if stop.is_set():
                    return
                mic = _sounddevice_module().RawInputStream(
                    samplerate=16000, blocksize=480, channels=1, dtype="int16",
                    device=sounddevice_index_from_identifier(self.settings.microphone_id), callback=callback)
                self._mic = mic
                mic.start()
                logger.debug("KWS stream opened: device=%s", self.settings.microphone_id)
            first_frame_deadline = time.monotonic() + 5
            received = False
            while not stop.is_set():
                try:
                    pcm = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if time.monotonic() > first_frame_deadline:
                        raise RuntimeError("麥克風未傳回音訊資料")
                    continue
                if not stop.is_set():
                    first_frame_deadline = time.monotonic() + 5
                    self.checks["Mic"] = True
                    if not logged_callbacks or callback_count - logged_callbacks >= 1000:
                        logged_callbacks = callback_count
                        logger.debug("KWS audio callbacks=%d samples=%d queued=%d", callback_count, sample_count, self._queue.qsize())
                    with self._lock:
                        self.process_microphone_frame(pcm, vad)
                    if not received:
                        received = True
                        logger.debug("KWS callback receiving audio; gain=%.2f", 10 ** (self.preamp.gain_db / 20))
                        self.checks["Pipeline"] = True
                        stage = "error_runtime"
                        if not self._latched:
                            self._state("standby", "語音助理：待機中")
        except UnsupportedKeyword:
            if not stop.is_set():
                self._state("unsupported", UNSUPPORTED)
        except Exception as exc:
            if not stop.is_set():
                message = "麥克風異常" if mic is not None else "本機喚醒元件無法啟動，請安裝 / 重試"
                import logging
                logging.getLogger(__name__).exception("Voice pipeline failed at %s", stage)
                labels = {"error_dependency": "語音元件驗證失敗", "error_model": "KWS 模型尚未就緒", "error_microphone": "選定麥克風無法開啟或讀取", "error_runtime": "語音管線執行失敗"}
                self.checks["Pipeline"] = False
                self._state(stage, labels[stage])
        finally:
            with self._lock:
                if mic is not None and self._mic is mic:
                    self._close_mic(mic)
                    self._mic = None
                    logger.debug("KWS stream closed")

    def capture_command(self, timeout_seconds=8):
        stop = self._stop
        # Post-wake capture only: KWS decisions and its ring buffer are unchanged.
        from .post_wake import SpeechEndpoint
        endpoint = SpeechEndpoint()
        deadline = time.monotonic() + timeout_seconds
        offset = 0
        with self._lock:
            wake_boundary = max(0, len(self._command) - int(max(0, time.monotonic() - self.last_wake_at) * 32000))
        while not stop.is_set() and time.monotonic() < deadline:
            with self._lock:
                buffered = bytes(self._command)
            while offset + 960 <= len(buffered):
                frame = buffered[offset:offset + 960]
                offset += 960
                if endpoint.feed(frame) and offset >= wake_boundary:
                    break
                endpoint.ended = False
            if endpoint.ended:
                break
            stop.wait(0.015)
        with self._lock:
            pcm = b"" if stop.is_set() else bytes(self._command[:offset or len(self._command)])
        self.command_eos_at = time.monotonic()
        self.command_speech_ended_at = self.command_eos_at - endpoint.quiet_ms / 1000 if endpoint.voiced_ms else None
        self.stop()
        return pcm

    def calibrate(self, duration_seconds=6):
        with self._lock:
            self._calibrating = True
            self.sensitivity._speech.clear()
            self.preamp.measurements.clear()
            self._latched = False
            self._event.clear()
        try:
            if self._stop.wait(duration_seconds):
                return '校準已取消，未儲存音訊。'
            with self._lock:
                return self.sensitivity.calibration_result() + "\n" + self.preamp.levels()
        finally:
            with self._lock:
                self._calibrating = False
                self.ring.clear()
                self._history.clear()
                self._speaking = False

    def test_microphone(self, duration_seconds=2):
        from .voice_command import analyze_microphone_pcm, record_microphone_pcm
        from dataclasses import replace
        from .microphone_gain import MicrophoneGain, rms, db
        raw = record_microphone_pcm(self.settings.microphone_id, duration_seconds)
        preamp = MicrophoneGain(self.settings.microphone_gain)
        processed = b''.join(preamp.process(raw[i:i+960]) for i in range(0, len(raw), 960))
        result = analyze_microphone_pcm(processed)
        return replace(result, message=result.message + f'\n原始 RMS：{db(rms(raw)):.1f} dBFS → 處理後 RMS：{db(rms(processed)):.1f} dBFS')

    def test_wake_word(self, duration_seconds=8):
        from .voice_command import WakeTestResult
        if self.runtime_state.state not in {"standby", "woken"}:
            return WakeTestResult(self.runtime_state.state, self.runtime_state.message)
        # Setup has its own timeout; the advertised test window starts at standby.
        deadline = time.monotonic() + 120
        while self.runtime_state.state == "starting" and time.monotonic() < deadline and not self._stop.is_set():
            self._stop.wait(0.05)
        if self.runtime_state.state not in {"standby", "woken"}:
            state = self.runtime_state
            self.stop()
            return WakeTestResult(state.state, state.message, provider_name=self.provider_name)
        deadline = time.monotonic() + min(10, max(5, duration_seconds))
        matched = False
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.poll():
                matched = True
                break
            self._stop.wait(0.03)
        keyword = self.last_keyword if matched else ""
        self.stop()
        return WakeTestResult("matched" if matched else "no-match",
                              ("偵測到" if matched else "未偵測到") + "\n" + self.provider_name,
                              matched=matched, matched_alias=keyword, provider_name=self.provider_name)
