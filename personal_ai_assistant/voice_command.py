from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import logging
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from .audio_devices import sounddevice_index_from_identifier
from .storage import default_data_dir


DEFAULT_ASSISTANT_NAME = "米米"
VOICE_OPTIONAL_DEPENDENCIES = ("faster-whisper", "sounddevice", "webrtcvad-wheels==2.0.14", "sherpa-onnx==1.13.8", "pypinyin==0.55.0", "opencc-python-reimplemented==0.1.7", "sentencepiece")
VOICE_MODEL_NAME = "small"
VOICE_SAMPLE_RATE = 16000
VOICE_FRAME_MS = 30
VOICE_FRAME_BYTES = int(VOICE_SAMPLE_RATE * VOICE_FRAME_MS / 1000) * 2
WAKE_TEMPLATE_SCHEMA_VERSION = 2
WAKE_ENROLLMENT_DEFAULT_SAMPLES = 5
WAKE_ENROLLMENT_MIN_SAMPLES = 3
WAKE_ENROLLMENT_MAX_SAMPLES = 5
WAKE_SENSITIVITY_THRESHOLDS = {"low": 0.94, "standard": 0.90, "high": 0.84}
WAKE_SENSITIVITY_OFFSETS = {"low": 0.04, "standard": 0.0, "high": -0.06}
WAKE_SEGMENT_PADDING_MS = 150
WAKE_MIN_CONSISTENCY = 0.72
WAKE_MIN_MEAN_CONSISTENCY = 0.80
WAKE_SELF_VALIDATION_MARGIN = 0.02
WAKE_COOLDOWN_SECONDS = 0.75

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceCommandStatus:
    available: bool
    message: str
    state: str = "not_installed"


@dataclass(frozen=True)
class WakeSettings:
    assistant_name: str = ""
    wake_aliases: tuple[str, ...] = ()
    microphone_id: str = ""
    wake_sound_enabled: bool = True
    wake_display: str = "osd"
    allow_during_games: bool = False
    stt_device: str = "auto"
    sensitivity: str = "auto"
    microphone_gain: str = "auto"
    template_id: str = ""


@dataclass(frozen=True)
class VoiceInstallState:
    state: str = "not_installed"
    message: str = "語音元件尚未安裝"
    error: str = ""


@dataclass(frozen=True)
class WakeNameValidation:
    risky: bool
    message: str = ""


@dataclass(frozen=True)
class WakeRuntimeState:
    state: str = "disabled"
    message: str = "語音助理未啟用"
    detail: str = ""


@dataclass(frozen=True)
class WakeMatchResult:
    matched: bool
    normalized_text: str
    matched_alias: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class AcousticMatchResult:
    matched: bool
    score: float
    similarity_label: str
    threshold: float
    template_id: str = ""
    reason: str = ""
    friendly_score: int = 0
    best_template_index: int = 0
    template_count: int = 0
    segment_seconds: float = 0.0
    segment_complete: bool = True
    crossed_threshold: bool = False


@dataclass(frozen=True)
class WakeTemplateStatus:
    available: bool
    state: str
    message: str
    template_id: str = ""
    assistant_name: str = ""
    sample_count: int = 0
    quality_label: str = ""


@dataclass(frozen=True)
class EnrollmentSampleResult:
    accepted: bool
    message: str
    features: tuple[tuple[float, ...], ...] = ()
    peak: float = 0.0
    duration_seconds: float = 0.0
    rms: float = 0.0
    segment_complete: bool = True


@dataclass(frozen=True)
class EnrollmentResult:
    success: bool
    message: str
    template_id: str = ""
    sample_count: int = 0
    quality_label: str = ""
    baseline_threshold: float = 0.0
    outlier_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class MicrophoneTestResult:
    stage: str
    message: str
    has_signal: bool = False
    has_voice: bool = False
    peak: float = 0.0
    rms: float = 0.0


@dataclass(frozen=True)
class WakeTestResult:
    stage: str
    message: str
    heard_audio: bool = False
    heard_voice: bool = False
    transcript: str = ""
    matched: bool = False
    matched_alias: str = ""
    similarity_label: str = ""
    acoustic_score: float = 0.0
    debug_only_stt: bool = False
    friendly_score: int = 0
    threshold: float = 0.0
    best_template_index: int = 0
    template_count: int = 0
    segment_seconds: float = 0.0
    segment_complete: bool = True
    provider_name: str = ""
    confidence: float | None = None


@dataclass(frozen=True)
class WakeSegment:
    pcm: bytes
    duration_seconds: float
    has_voice: bool
    complete: bool
    peak: float = 0.0
    rms: float = 0.0


class WakeWordProvider(Protocol):
    def configure(self, aliases: tuple[str, ...], microphone_id: str = "") -> None:
        ...

    def start(self) -> WakeRuntimeState:
        ...

    def stop(self) -> None:
        ...

    def poll(self) -> bool:
        ...


class SpeechToTextProvider(Protocol):
    def configure(self, microphone_id: str = "") -> None:
        ...

    def listen_once(self, timeout_seconds: int = 8) -> str:
        ...


class LocalWhisperTranscriber:
    def __init__(
        self,
        model_name: str = VOICE_MODEL_NAME,
        device: str = "auto",
        model_dir: Path | None = None,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = "cuda" if device == "gpu" else "cpu" if device == "cpu" else "auto"
        self.model_dir = model_dir
        self.status_callback = status_callback or (lambda _message: None)
        self._model = None
        self._lock = threading.Lock()
        self._warm_thread = None
        self._inference_lock = threading.Lock()
        self.context_names = ()

    def warm_background(self):
        if self._warm_thread is not None:
            return
        def warm():
            try:
                self._load_model()
            except Exception:
                logger.debug('Full STT warm-up unavailable', exc_info=True)
        self._warm_thread = threading.Thread(target=warm, daemon=True, name='stt-warmup')
        self._warm_thread.start()

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = VOICE_SAMPLE_RATE, language: str | None = None) -> str:
        if not pcm:
            self.last_evidence = dict(speech_detected=False, discard_reason="no_speech", stt_confidence=None)
            return ""
        from .asr_guard import speech_presence
        self.last_evidence = speech_presence(pcm, sample_rate)
        if not self.last_evidence['speech_detected']:
            return ""
        model = self._load_model()
        try:
            import numpy as np
        except Exception as exc:
            raise RuntimeError("語音元件缺少 numpy，請重新安裝語音元件") from exc
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate != VOICE_SAMPLE_RATE:
            raise RuntimeError("語音取樣率不支援，請改用 16 kHz 麥克風輸入")
        try:
            with self._inference_lock:
                from .asr_guard import assess_segments
                segments, _info = model.transcribe(audio, language=language, beam_size=1, vad_filter=True)
                text, evidence = assess_segments(list(segments), self.context_names, self.last_evidence.get("speech_confidence", 0))
                self.last_evidence.update(evidence)
                return text
        except Exception as exc:
            raise RuntimeError(f"語音模型辨識失敗：{exc}") from exc

    def _load_model(self):
        with self._lock:
            if self._model is not None:
                return self._model
            self.status_callback("語音模型準備中")
            try:
                from faster_whisper import WhisperModel

                kwargs = {}
                if self.model_dir is not None:
                    kwargs["download_root"] = str(self.model_dir)
                self._model = WhisperModel(self.model_name, device=self.device, compute_type="int8", **kwargs)
            except Exception as exc:
                raise RuntimeError(f"語音模型尚未就緒，請按「安裝 / 重試」後再測試：{exc}") from exc
            return self._model


class WakeTemplateStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.root = (data_dir or default_data_dir()) / "wake_templates"

    def template_path(self, template_id: str) -> Path:
        safe_id = "".join(char for char in template_id if char.isalnum() or char in {"-", "_"})
        return self.root / f"{safe_id}.json"

    def status(self, settings: WakeSettings) -> WakeTemplateStatus:
        if not settings.assistant_name.strip():
            return WakeTemplateStatus(False, "missing-name", "助理名稱尚未設定")
        if not settings.template_id:
            return WakeTemplateStatus(False, "needs-training", "需要訓練喚醒詞")
        template = self.load(settings.template_id)
        if not template:
            return WakeTemplateStatus(False, "needs-training", "需要訓練喚醒詞")
        if template.get("assistant_name") != settings.assistant_name:
            return WakeTemplateStatus(False, "needs-training", "助理名稱已變更，需要重新訓練")
        samples = template.get("samples", [])
        sample_count = len(samples) if isinstance(samples, list) else 0
        quality_label = str(template.get("quality_label") or "")
        if not template.get("self_validation_passed"):
            return WakeTemplateStatus(
                False,
                "needs-training",
                "訓練品質不足，請重新訓練喚醒詞",
                settings.template_id,
                settings.assistant_name,
                sample_count,
                quality_label,
            )
        return WakeTemplateStatus(
            True,
            "ready",
            "喚醒詞已訓練",
            settings.template_id,
            settings.assistant_name,
            sample_count,
            quality_label,
        )

    def save(
        self,
        assistant_name: str,
        samples: list[tuple[tuple[float, ...], ...]],
        *,
        baseline_threshold: float,
        quality_label: str,
        consistency_score: float,
        self_validation_passed: bool,
        self_validation_score: float = 0.0,
    ) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        template_id = hashlib.sha256(f"{assistant_name}:{time.time_ns()}".encode("utf-8")).hexdigest()[:24]
        payload = {
            "schema": WAKE_TEMPLATE_SCHEMA_VERSION,
            "assistant_name": assistant_name,
            "sample_count": len(samples),
            "sample_rate": VOICE_SAMPLE_RATE,
            "feature_type": "wake-logmel-dtw-v3",
            "created_at": int(time.time()),
            "baseline_threshold": round(baseline_threshold, 4),
            "quality_label": quality_label,
            "consistency_score": round(consistency_score, 4),
            "self_validation_passed": bool(self_validation_passed),
            "self_validation_score": round(self_validation_score, 4),
            "samples": [[list(frame) for frame in sample] for sample in samples],
        }
        self.template_path(template_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return template_id

    def load(self, template_id: str) -> dict[str, object] | None:
        if not template_id:
            return None
        try:
            payload = json.loads(self.template_path(template_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("schema") != WAKE_TEMPLATE_SCHEMA_VERSION:
            return None
        if not isinstance(payload.get("samples"), list):
            return None
        return payload

    def clear(self, template_id: str = "") -> None:
        if template_id:
            try:
                self.template_path(template_id).unlink()
            except FileNotFoundError:
                pass
            return
        if self.root.exists():
            shutil.rmtree(self.root)


class BaselineAcousticKeywordMatcher:
    def __init__(self, store: WakeTemplateStore | None = None) -> None:
        self.store = store or WakeTemplateStore()

    def template_status(self, settings: WakeSettings) -> WakeTemplateStatus:
        return self.store.status(settings)

    def enroll_features(
        self,
        assistant_name: str,
        features: list[tuple[tuple[float, ...], ...]],
        validation_features: tuple[tuple[float, ...], ...] | None = None,
    ) -> EnrollmentResult:
        valid = [sample for sample in features if sample]
        if len(valid) < WAKE_ENROLLMENT_MIN_SAMPLES:
            return EnrollmentResult(False, f"至少需要 {WAKE_ENROLLMENT_MIN_SAMPLES} 次有效錄音")
        samples = valid[:WAKE_ENROLLMENT_MAX_SAMPLES]
        quality = enrollment_quality(samples)
        if not quality["ready"]:
            return EnrollmentResult(False, quality["message"], sample_count=len(samples), quality_label=quality["label"], outlier_indices=tuple(quality.get("outliers", ())))
        baseline_threshold = calibrated_baseline_threshold(samples)
        validation_score = 0.0
        if validation_features is None:
            return EnrollmentResult(False, "還需要再說一次做自我驗證，通過後才會啟用", sample_count=len(samples), quality_label=quality["label"], outlier_indices=tuple(quality.get("outliers", ())))
        validation_score, _, validation_scores = multi_template_similarity(validation_features, samples)
        validation_threshold = max(0.50, baseline_threshold - WAKE_SELF_VALIDATION_MARGIN)
        # Independent validation must agree with a majority, not just one
        # accidentally matching recording in an otherwise unrelated batch.
        support = sum(score >= validation_threshold for score in validation_scores)
        if validation_score < validation_threshold or support < len(samples) // 2 + 1:
            message = (
                f"自我驗證未通過（匹配強度 {friendly_score(validation_score)}），"
                "訓練品質：建議重錄；請先檢查是否完整收到聲音"
            )
            return EnrollmentResult(False, message, sample_count=len(samples), quality_label="建議重錄", baseline_threshold=baseline_threshold)
        template_id = self.store.save(
            assistant_name,
            samples,
            baseline_threshold=baseline_threshold,
            quality_label=str(quality["label"]),
            consistency_score=float(quality["score"]),
            self_validation_passed=True,
            self_validation_score=validation_score,
        )
        return EnrollmentResult(True, "訓練完成", template_id, len(samples), str(quality["label"]), baseline_threshold)

    def match(self, pcm: bytes, settings: WakeSettings, sample_rate: int = VOICE_SAMPLE_RATE) -> AcousticMatchResult:
        status = self.template_status(settings)
        template = self.store.load(settings.template_id) if status.template_id else None
        threshold = sensitivity_threshold(settings.sensitivity, template)
        if not status.available:
            return AcousticMatchResult(False, 0.0, "低", threshold, reason=status.message, friendly_score=0)
        segment = wake_segment_from_pcm(pcm, sample_rate)
        candidate = acoustic_features_from_pcm(segment.pcm, sample_rate)
        if not candidate:
            return AcousticMatchResult(
                False,
                0.0,
                "低",
                threshold,
                status.template_id,
                "沒有抓到有效聲音",
                friendly_score=0,
                segment_seconds=segment.duration_seconds,
                segment_complete=segment.complete,
            )
        samples: list[tuple[tuple[float, ...], ...]] = []
        for raw_sample in template.get("samples", []) if template else []:
            if not isinstance(raw_sample, list):
                continue
            sample = tuple(tuple(float(value) for value in frame) for frame in raw_sample if isinstance(frame, list))
            if sample:
                samples.append(sample)
        score, best_index, _scores = multi_template_similarity(candidate, samples)
        matched = score >= threshold
        return AcousticMatchResult(
            matched,
            score,
            similarity_label(score),
            threshold,
            status.template_id,
            friendly_score=friendly_score(score),
            best_template_index=best_index,
            template_count=len(samples),
            segment_seconds=segment.duration_seconds,
            segment_complete=segment.complete,
            crossed_threshold=matched,
        )


class PrefixWakeWordProvider:
    """Compatibility provider for tests and fixed text sources."""

    def __init__(self, prefix_source: Callable[[], str] | None = None) -> None:
        self.aliases: tuple[str, ...] = ()
        self.microphone_id = ""
        self.prefix_source = prefix_source
        self.started = False
        self.last_transcript = ""

    def configure(self, aliases: tuple[str, ...], microphone_id: str = "") -> None:
        self.aliases = normalize_wake_aliases("", aliases)
        self.microphone_id = microphone_id

    def start(self) -> WakeRuntimeState:
        self.started = True
        return WakeRuntimeState("standby", "待機中")

    def stop(self) -> None:
        self.started = False

    def poll(self) -> bool:
        if not self.started or not self.prefix_source:
            return False
        self.last_transcript = self.prefix_source()
        return wake_phrase_matches(self.last_transcript, self.aliases).matched

    def test_microphone(self, duration_seconds: float = 2.0) -> MicrophoneTestResult:
        return MicrophoneTestResult("unsupported", "這個 wake backend 沒有麥克風輸入測試")

    def test_wake_word(self, duration_seconds: float = 8.0) -> WakeTestResult:
        text = self.prefix_source() if self.prefix_source else ""
        result = wake_phrase_matches(text, self.aliases)
        return WakeTestResult(
            "matched" if result.matched else "not_matched",
            _format_wake_test_message(bool(text), bool(text), text, result),
            heard_audio=bool(text),
            heard_voice=bool(text),
            transcript=text,
            matched=result.matched,
            matched_alias=result.matched_alias,
        )


class FixedKeywordWakeWordProvider:
    """Adapter for fixed-keyword engines; arbitrary Chinese names are rejected so callers can fall back."""

    def __init__(self, supported_keywords: tuple[str, ...], poll_source: Callable[[], bool] | None = None) -> None:
        self.supported_keywords = tuple(canonical_wake_text(item) for item in supported_keywords)
        self.aliases: tuple[str, ...] = ()
        self.microphone_id = ""
        self.poll_source = poll_source or (lambda: False)
        self.started = False

    def configure(self, aliases: tuple[str, ...], microphone_id: str = "") -> None:
        self.aliases = aliases
        self.microphone_id = microphone_id

    def start(self) -> WakeRuntimeState:
        unsupported = [alias for alias in self.aliases if canonical_wake_text(alias) not in self.supported_keywords]
        if unsupported:
            self.started = False
            return WakeRuntimeState("unsupported", "目前固定喚醒詞 backend 不支援這個自訂名稱")
        self.started = True
        return WakeRuntimeState("standby", "待機中")

    def stop(self) -> None:
        self.started = False

    def poll(self) -> bool:
        return self.started and bool(self.poll_source())


class FallbackWakeWordProvider:
    def __init__(self, primary: WakeWordProvider, fallback: WakeWordProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.active_provider: WakeWordProvider = primary
        self.aliases: tuple[str, ...] = ()
        self.microphone_id = ""
        self.runtime_state = WakeRuntimeState()

    def configure(self, aliases: tuple[str, ...], microphone_id: str = "") -> None:
        self.aliases = aliases
        self.microphone_id = microphone_id
        self.primary.configure(aliases, microphone_id)
        self.fallback.configure(aliases, microphone_id)

    def start(self) -> WakeRuntimeState:
        primary_state = self.primary.start()
        if primary_state.state != "unsupported":
            self.active_provider = self.primary
            self.runtime_state = primary_state
            return primary_state
        self.primary.stop()
        self.active_provider = self.fallback
        self.runtime_state = self.fallback.start()
        return self.runtime_state

    def stop(self) -> None:
        self.primary.stop()
        self.fallback.stop()

    def poll(self) -> bool:
        provider_state = getattr(self.active_provider, "runtime_state", None)
        if isinstance(provider_state, WakeRuntimeState):
            self.runtime_state = provider_state
        return self.active_provider.poll()

    def test_microphone(self, duration_seconds: float = 2.0) -> MicrophoneTestResult:
        tester = getattr(self.active_provider, "test_microphone", None)
        if callable(tester):
            return tester(duration_seconds)
        return MicrophoneTestResult("unsupported", "這個 wake backend 沒有麥克風輸入測試")

    def test_wake_word(self, duration_seconds: float = 8.0) -> WakeTestResult:
        tester = getattr(self.active_provider, "test_wake_word", None)
        if callable(tester):
            return tester(duration_seconds)
        return WakeTestResult("unsupported", "這個 wake backend 沒有喚醒詞測試")


class SoundDevicePrefixWakeWordProvider:
    """VAD-gated acoustic-template wake detector for arbitrary Chinese assistant names."""

    def __init__(
        self,
        transcriber: LocalWhisperTranscriber | None = None,
        matcher: BaselineAcousticKeywordMatcher | None = None,
        settings: WakeSettings | None = None,
        sample_rate: int = VOICE_SAMPLE_RATE,
        frame_ms: int = VOICE_FRAME_MS,
        prefix_seconds: float = 1.6,
        vad_aggressiveness: int = 2,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.aliases: tuple[str, ...] = ()
        self.settings = settings or WakeSettings()
        self.microphone_id = ""
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.prefix_seconds = prefix_seconds
        self.vad_aggressiveness = vad_aggressiveness
        self.status_callback = status_callback or (lambda _message: None)
        self.transcriber = transcriber or LocalWhisperTranscriber(status_callback=self.status_callback)
        self.matcher = matcher or BaselineAcousticKeywordMatcher()
        self.runtime_state = WakeRuntimeState()
        self.last_transcript = ""
        self.last_match: WakeMatchResult | None = None
        self.last_acoustic_match: AcousticMatchResult | None = None
        self.last_wake_at = 0.0
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=120)
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._lock = threading.Lock()

    def configure(self, aliases: tuple[str, ...], microphone_id: str = "") -> None:
        self.aliases = normalize_wake_aliases("", aliases)
        self.microphone_id = microphone_id

    def configure_settings(self, settings: WakeSettings) -> None:
        self.settings = settings
        self.configure(wake_aliases_for_settings(settings), settings.microphone_id)

    def start(self) -> WakeRuntimeState:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.runtime_state
            if not self.aliases:
                self.runtime_state = WakeRuntimeState("unsupported", "喚醒名稱尚未設定")
                return self.runtime_state
            template_status = self.matcher.template_status(self.settings)
            if not template_status.available:
                self.runtime_state = WakeRuntimeState("needs-training", template_status.message)
                return self.runtime_state
            component_state = detect_voice_components()
            if component_state.state != "ready":
                self.runtime_state = WakeRuntimeState(component_state.state, component_state.message, component_state.error)
                return self.runtime_state
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(target=self._run, name="voice-wake-detector", daemon=True)
            self._thread.start()
            self.runtime_state = WakeRuntimeState("starting", "語音元件啟動中")
            return self.runtime_state

    def stop(self) -> None:
        self._stop_event.set()
        stream = self._stream
        self._stream = None
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception:
            logger.debug("Ignoring wake stream close failure", exc_info=True)
        self.runtime_state = WakeRuntimeState("disabled", "語音助理未啟用")

    def poll(self) -> bool:
        if not self._wake_event.is_set():
            return False
        self._wake_event.clear()
        return True

    def test_microphone(self, duration_seconds: float = 2.0) -> MicrophoneTestResult:
        try:
            pcm = record_microphone_pcm(self.microphone_id, duration_seconds)
            return analyze_microphone_pcm(pcm, self.sample_rate)
        except Exception as exc:
            return MicrophoneTestResult("error", f"語音元件異常：{exc}")

    def test_wake_word(self, duration_seconds: float = 8.0) -> WakeTestResult:
        try:
            template_status = self.matcher.template_status(self.settings)
            if not template_status.available:
                return WakeTestResult("needs-training", "請先訓練喚醒詞", matched=False)
            pcm = record_microphone_pcm(self.microphone_id, duration_seconds)
            mic = analyze_microphone_pcm(pcm, self.sample_rate)
            if not mic.has_signal:
                return WakeTestResult("mic-no-signal", "麥克風無訊號", heard_audio=False)
            if not mic.has_voice:
                return WakeTestResult("no-voice", "有收到聲音，但沒有偵測到人聲", heard_audio=True)
            segment = wake_segment_from_pcm(pcm, self.sample_rate, self.prefix_seconds, self.vad_aggressiveness)
            acoustic = self.matcher.match(pcm, self.settings, self.sample_rate)
            transcript = self._debug_transcribe(segment.pcm)
            return WakeTestResult(
                "matched" if acoustic.matched else "not_matched",
                _format_acoustic_wake_test_message(True, True, self.settings.assistant_name, acoustic, transcript),
                heard_audio=True,
                heard_voice=True,
                transcript=transcript,
                matched=acoustic.matched,
                matched_alias=self.settings.assistant_name if acoustic.matched else "",
                similarity_label=acoustic.similarity_label,
                acoustic_score=acoustic.score,
                debug_only_stt=True,
                friendly_score=acoustic.friendly_score,
                threshold=acoustic.threshold,
                best_template_index=acoustic.best_template_index,
                template_count=acoustic.template_count,
                segment_seconds=acoustic.segment_seconds,
                segment_complete=acoustic.segment_complete,
            )
        except Exception as exc:
            return WakeTestResult("error", f"語音元件異常：{exc}")

    def enrollment_sample(self, duration_seconds: float = 2.5) -> EnrollmentSampleResult:
        pcm = record_microphone_pcm(self.microphone_id, duration_seconds)
        try:
            return enrollment_sample_from_pcm(pcm, self.sample_rate)
        finally:
            del pcm

    def _run(self) -> None:
        try:
            sounddevice = _sounddevice_module()
            webrtcvad = _webrtcvad_module()
            vad = webrtcvad.Vad(self.vad_aggressiveness)
            device_index = sounddevice_index_from_identifier(self.microphone_id)
            frame_samples = int(self.sample_rate * self.frame_ms / 1000)

            def callback(indata, frames, _time_info, status) -> None:
                if status:
                    logger.debug("Wake input stream status: %s", status)
                if frames != frame_samples:
                    return
                try:
                    self._audio_queue.put_nowait(bytes(indata))
                except queue.Full:
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        pass
                    self._audio_queue.put_nowait(bytes(indata))

            stream = sounddevice.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=frame_samples,
                device=device_index,
                channels=1,
                dtype="int16",
                callback=callback,
            )
            self._stream = stream
            stream.start()
            self.runtime_state = WakeRuntimeState("standby", "待機中")
            logger.info("Wake detector started with microphone_id=%s device_index=%s", self.microphone_id or "default", device_index)
            self._consume_audio(vad)
        except Exception as exc:
            self.runtime_state = WakeRuntimeState("error", f"語音元件異常：{exc}", str(exc))
            logger.exception("Wake detector failed before audio could be consumed")
        finally:
            self._stop_event.set()
            stream = self._stream
            self._stream = None
            try:
                if stream is not None:
                    stream.stop()
                    stream.close()
            except Exception:
                logger.debug("Ignoring wake stream close failure", exc_info=True)

    def _consume_audio(self, vad) -> None:
        speaking = False
        speech_started = 0.0
        last_voice_at = 0.0
        voiced_frames: list[bytes] = []
        pre_roll_frames: list[bytes] = []
        pre_roll_limit = max(1, int(WAKE_SEGMENT_PADDING_MS / self.frame_ms))
        max_frames = max(1, int(self.prefix_seconds * 1000 / self.frame_ms))
        silence_frames_to_close = max(1, int(450 / self.frame_ms))
        silent_after_voice = 0
        no_signal_since = time.monotonic()

        while not self._stop_event.is_set():
            try:
                frame = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() - no_signal_since > 2.5:
                    self.runtime_state = WakeRuntimeState("mic-no-signal", "麥克風無訊號")
                continue
            has_signal, _peak, _rms = pcm_has_signal(frame)
            if has_signal:
                no_signal_since = time.monotonic()
            try:
                is_voice = has_signal and vad.is_speech(frame, self.sample_rate)
            except Exception:
                is_voice = has_signal
            if is_voice:
                if not speaking:
                    speaking = True
                    speech_started = time.monotonic()
                    voiced_frames = list(pre_roll_frames)
                    self.runtime_state = WakeRuntimeState("voice", "偵測到人聲")
                last_voice_at = time.monotonic()
                silent_after_voice = 0
                voiced_frames.append(frame)
            elif speaking:
                silent_after_voice += 1
                if len(voiced_frames) < max_frames:
                    voiced_frames.append(frame)
            else:
                pre_roll_frames.append(frame)
                if len(pre_roll_frames) > pre_roll_limit:
                    pre_roll_frames = pre_roll_frames[-pre_roll_limit:]

            enough_audio = speaking and len(voiced_frames) >= max_frames
            speech_closed = speaking and silent_after_voice >= silence_frames_to_close and (last_voice_at - speech_started) >= 0.25
            if enough_audio or speech_closed:
                prefix = b"".join(voiced_frames[:max_frames])
                self._check_prefix(prefix)
                speaking = False
                voiced_frames = []
                silent_after_voice = 0
                if not self._wake_event.is_set():
                    self.runtime_state = WakeRuntimeState("standby", "待機中")

    def _check_prefix(self, pcm: bytes) -> None:
        self.runtime_state = WakeRuntimeState("recognizing", "比對喚醒詞")
        acoustic = self.matcher.match(pcm, self.settings, self.sample_rate)
        self.last_acoustic_match = acoustic
        logger.info("Wake acoustic checked: matched=%s score=%.2f threshold=%.2f", acoustic.matched, acoustic.score, acoustic.threshold)
        if acoustic.matched and time.monotonic() - self.last_wake_at >= WAKE_COOLDOWN_SECONDS:
            self.last_wake_at = time.monotonic()
            self.runtime_state = WakeRuntimeState("woken", "已喚醒")
            self._wake_event.set()

    def _debug_transcribe(self, pcm: bytes) -> str:
        try:
            return self.transcriber.transcribe_pcm(pcm, self.sample_rate)
        except Exception:
            logger.debug("Debug wake STT failed", exc_info=True)
            return ""


class FasterWhisperSTTProvider:
    def __init__(
        self,
        model_name: str = VOICE_MODEL_NAME,
        device: str = "auto",
        model_dir: Path | None = None,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.microphone_id = ""
        self.transcriber = LocalWhisperTranscriber(model_name, device, model_dir, status_callback)

    def configure(self, microphone_id: str = "") -> None:
        self.microphone_id = microphone_id

    def listen_once(self, timeout_seconds: int = 8) -> str:
        from .post_wake import record_command
        pcm = record_command(self.microphone_id, timeout_seconds, gain_mode=getattr(self, "microphone_gain", "auto"))
        return self.transcriber.transcribe_pcm(pcm)


class VoiceAssistantPipeline:
    def __init__(
        self,
        settings: WakeSettings,
        wake_provider: WakeWordProvider | None = None,
        stt_provider: SpeechToTextProvider | None = None,
        intent_runner: Callable[[str], bool] | None = None,
        privacy_enabled: Callable[[], bool] | None = None,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.status_callback = status_callback or (lambda _message: None)
        from .kws import StreamingKwsWakeWordProvider
        self.wake_provider = wake_provider or StreamingKwsWakeWordProvider(status_callback=self.status_callback)
        self.stt_provider = stt_provider or FasterWhisperSTTProvider(device=settings.stt_device, status_callback=self.status_callback)
        self.intent_runner = intent_runner
        self.privacy_enabled = privacy_enabled or (lambda: False)
        from .voice_text import VoiceDiagnostics
        self.diagnostics = VoiceDiagnostics()
        self.full_stt_invocations = 0
        self.hot_restart_count = 0
        self.active = False
        self._service_requested = False
        self._last_restart = -float("inf")
        self.runtime_state = WakeRuntimeState()
        self._handling_wake = False
        self._rearming = False
        self.state = 'disabled'
        self.last_activity = time.monotonic()
        self._cancel_event = threading.Event()
        self._session_lock = threading.RLock()
        self._capture_lock = threading.Lock()
        from concurrent.futures import ThreadPoolExecutor
        self._stt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='command-stt')
        self._session_finished = True
        self.session_id = 0
        self.context_provider = lambda: ()
        self.reload(settings)

    def transition(self, state):
        self.state = state
        self.last_activity = time.monotonic()

    def prepare_capture(self):
        self._cancel_event.set()
        self.session_id += 1
        self._session_finished = False
        self._cancel_event = threading.Event()
        self.transition('wake_detected')
        transcriber = getattr(self.stt_provider, 'transcriber', None)
        if transcriber is not None:
            # Local inventories belong exclusively to post-STT entity resolution.
            transcriber.context_names = ()
            transcriber.last_evidence = {}
        self.transition('listening')

    def cleanup(self):
        return self.finish_command_session('finished')

    def finish_command_session(self, result):
        with self._session_lock:
            if self._session_finished and self.state in {'standby', 'rearming', 'disabled', 'error'} and not self._handling_wake:
                return
            self._session_finished = True
            self.transition('cleanup')
            self._cancel_event.set()
            self._handling_wake = False
            self._trace_pending = False
            if self.active and not self.privacy_enabled():
                self.transition('rearming')
                try:
                    from .kws import StreamingKwsWakeWordProvider
                    if isinstance(self.wake_provider, StreamingKwsWakeWordProvider):
                        self.runtime_state = self.wake_provider.rearm()
                    else:
                        self.wake_provider.stop()
                        self.runtime_state = self.wake_provider.start()
                    self._rearming = True
                    if self.runtime_state.state in {'standby', 'voice', 'woken'}:
                        self.transition('standby')
                    elif self.runtime_state.state not in {'starting', 'verifying', 'loading_models', 'opening_microphone'}:
                        raise RuntimeError(self.runtime_state.message)
                except Exception as exc:
                    self.active = False
                    self.transition('error')
                    self.runtime_state = WakeRuntimeState('error_rearm', '語音喚醒未恢復，請重試', str(exc))
                    self.status_callback(self.runtime_state.message)
            else:
                self.transition('disabled')

    def watchdog(self, now=None):
        now = time.monotonic() if now is None else now
        if self.privacy_enabled():
            if self.active or self._service_requested:
                self.stop()
            return False
        from .kws import StreamingKwsWakeWordProvider
        provider = self.wake_provider
        # Command capture deliberately stops the mic. Recover only outside a session.
        if (self._service_requested and isinstance(provider, StreamingKwsWakeWordProvider)
                and self.state in {'standby', 'initializing', 'error', 'rearming'}
                and provider.runtime_state.state not in {'unsupported', 'missing_components', 'error_dependency'}
                and not provider.worker_alive() and now - self._last_restart >= 5):
            self._last_restart = now
            self.hot_restart_count += 1
            logger.warning('Restarting dead KWS worker: provider=%s, service=%s, count=%s',
                           provider.runtime_state.state, self.state, self.hot_restart_count)
            self.active = False
            self.start()
            return True
        if self.state in {'listening', 'processing'} and now-self.last_activity > 15:
            self.diagnostics.update(result='語音逾時，已恢復待機')
            self.cleanup()
            return True
        return False

    def readiness(self):
        if self.runtime_state.state in {"installing", "restart_required", "error_dependency", "error_rearm"} or (self.runtime_state.state == "verifying" and not self.active):
            return self.runtime_state
        state = getattr(self.wake_provider, "runtime_state", self.runtime_state)
        if self._rearming and (state.state.startswith('error') or state.state in {'unsupported', 'disabled'}):
            self.active = False
            self.runtime_state = WakeRuntimeState('error_rearm', '語音喚醒未恢復，請重試', state.message)
            return self.runtime_state
        if state.state in {'standby', 'voice', 'woken'}:
            if self._rearming or self.state == 'initializing':
                self.transition('standby')
            self._rearming = False
        mapping = {"standby": "ready", "voice": "ready", "woken": "ready", "starting": "loading_models", "error": "error_runtime", "unsupported": "error_model"}
        self.runtime_state = WakeRuntimeState(mapping.get(state.state, state.state), state.message)
        return self.runtime_state

    def reload(self, settings: WakeSettings) -> None:
        was_active = self.active
        microphone_changed = settings != self.settings
        self.settings = settings
        configure_settings = getattr(self.wake_provider, "configure_settings", None)
        if callable(configure_settings):
            configure_settings(settings)
        else:
            self.wake_provider.configure(wake_aliases_for_settings(settings), settings.microphone_id)
        if hasattr(self.stt_provider, "configure"):
            self.stt_provider.configure(settings.microphone_id)
        if isinstance(self.stt_provider, FasterWhisperSTTProvider):
            self.stt_provider.microphone_gain = settings.microphone_gain
        if was_active and microphone_changed:
            self.hot_restart_count += 1
            self.stop()
            self.start()

    def start(self) -> None:
        self._service_requested = True
        if self.active and not self.privacy_enabled():
            return
        if self.privacy_enabled():
            self.stop()
            self.active = False
            self.runtime_state = WakeRuntimeState("disabled", "隱私模式中，語音助理已暫停")
            return
        if not self.settings.assistant_name.strip():
            self.active = False
            self.runtime_state = WakeRuntimeState("unsupported", "助理名稱尚未設定")
            return
        self.transition('initializing')
        starter = getattr(self.wake_provider, "start", None)
        if callable(starter):
            self.runtime_state = starter()
        else:
            self.runtime_state = WakeRuntimeState("standby", "待機中")
        self.active = self.runtime_state.state in {"starting", "verifying", "loading_models", "opening_microphone", "standby", "voice", "recognizing", "woken"}
        if self.active and self.runtime_state.state in {'standby', 'voice', 'woken'}:
            self.transition('standby')
        elif not self.active:
            self.transition('error')
        if self.active and isinstance(self.stt_provider, FasterWhisperSTTProvider):
            self.stt_provider.transcriber.warm_background()

    def stop(self) -> None:
        self._service_requested = False
        self._rearming = False
        self._cancel_event.set()
        self.transition('disabled')
        stopper = getattr(self.wake_provider, "stop", None)
        if callable(stopper):
            stopper()
        self.active = False
        self.runtime_state = WakeRuntimeState("disabled", "語音助理未啟用")

    def idle_tick(self) -> bool:
        if not self.active:
            return False
        if self.privacy_enabled():
            self.stop()
            return False
        provider_state = getattr(self.wake_provider, "runtime_state", None)
        if isinstance(provider_state, WakeRuntimeState):
            self.runtime_state = provider_state
        if not self.wake_provider.poll():
            return False
        if self._handling_wake:
            return False
        self.status_callback(f"{self.settings.assistant_name}正在聽…")
        return True

    def handle_wake(self, spoken_text: str | None = None, timeout_seconds: int = 8) -> bool:
        if spoken_text == "" and self.diagnostics.data.get("discard_reason"):
            self.cleanup()
            return False
        if self._handling_wake:
            return False
        self._handling_wake = True
        self.transition('processing')
        try:
            if self.privacy_enabled():
                self.stop()
                return False
            if spoken_text is None:
                if not self.stt_provider:
                    self.status_callback("語音辨識尚未可用")
                    return False
                try:
                    spoken_text = self.transcribe_after_wake(timeout_seconds)
                except Exception as exc:
                    self.runtime_state = WakeRuntimeState("error", f"語音元件異常：{exc}", str(exc))
                    self.status_callback(self.runtime_state.message)
                    logger.exception("Full STT after wake failed")
                    return False
            if self.privacy_enabled():
                return False
            if not spoken_text and self.diagnostics.data.get('discard_reason'):
                return False
            from .voice_text import normalize_voice
            prepared = normalize_voice(spoken_text, wake_aliases_for_settings(self.settings))
            if not getattr(self, '_trace_pending', False):
                self.diagnostics.begin({})
                self.diagnostics.mark('wake_event_at')
                self.diagnostics.mark('stt_done')
            self._trace_pending = False
            self.diagnostics.update(**prepared)
            self.diagnostics.mark('normalize_done')
            if not prepared['normalized']:
                self.diagnostics.update(intent='unknown', target='', final_action='未聽清楚', result='未聽清楚')
                self.status_callback('剛剛沒有聽清楚，請再說一次')
                return False
            self.status_callback('正在處理：' + prepared['normalized'][:80])
            command = prepared["normalized"]
            self.transition('executing')
            return bool(self.intent_runner(command)) if self.intent_runner else True
        finally:
            try:
                if not self.privacy_enabled():
                    self.diagnostics.mark('action_done')
                    self.diagnostics.log()
                else:
                    self.diagnostics.clear()
            finally:
                self.cleanup()

    def _finish_capture(self, result):
        evidence = getattr(getattr(self.stt_provider, 'transcriber', None), 'last_evidence', {})
        if isinstance(evidence, dict):
            self.diagnostics.update(**evidence)
        if not result:
            reason = self.diagnostics.data.get('discard_reason') or 'no_speech'
            message = '疑似語音模型幻覺，已忽略' if reason == 'prompt_echo' else '未偵測到可靠人聲，已忽略' if reason == 'no_speech' else '語音信心不足，已忽略'
            self.diagnostics.update(speech_detected=self.diagnostics.data.get('speech_detected', False),
                                    stt_confidence=self.diagnostics.data.get('stt_confidence'),
                                    discard_reason=reason, result=message, final_action='未執行', raw='', normalized='')
            try:
                self.status_callback('沒有聽到指令')
                self.diagnostics.log()
            finally:
                self.cleanup()
        return result

    def transcribe_after_wake(self, timeout_seconds: int = 8) -> str:
        self.prepare_capture()
        session = self._cancel_event
        try:
            with self._capture_lock:
                if session.is_set():
                    return ''
                result = self._transcribe_after_wake(timeout_seconds)
            if self._cancel_event is not session or session.is_set():
                return ''
            result = self._finish_capture(result)
            if self._cancel_event is session and not session.is_set():
                self.transition('processing')
            return result
        except BaseException:
            if self._cancel_event is session:
                self.cleanup()
            raise

    def _transcribe_after_wake(self, timeout_seconds: int = 8) -> str:
        if self.privacy_enabled():
            self.stop()
            return ""
        self.diagnostics.begin({})
        self._trace_pending = True
        wake_at = getattr(self.wake_provider, 'last_wake_at', None)
        self.diagnostics.mark('wake_event_at', wake_at if isinstance(wake_at, (float, int)) and wake_at > 0 else None)
        self.diagnostics.mark('recording_start', wake_at if isinstance(wake_at, (float, int)) and wake_at > 0 else None)
        capture = getattr(self.wake_provider, "capture_command", None)
        from .kws import StreamingKwsWakeWordProvider
        if isinstance(self.wake_provider, StreamingKwsWakeWordProvider) and hasattr(self.stt_provider, 'transcriber'):
            from .post_wake import capture_post_wake
            cancel_event = self._cancel_event
            def transcribe(pcm):
                if cancel_event.is_set():
                    return ''
                self.full_stt_invocations += 1
                return self.stt_provider.transcriber.transcribe_pcm(pcm)
            return capture_post_wake(self.wake_provider, transcribe, wake_aliases_for_settings(self.settings),
                                     timeout_seconds, lambda: cancel_event.is_set() or self.privacy_enabled() or not self.active,
                                     lambda *args: self.diagnostics.mark(*args) if self._cancel_event is cancel_event and not cancel_event.is_set() else None,
                                     executor=self._stt_executor)
        if callable(capture) and hasattr(self.stt_provider, "transcriber"):
            pcm = capture(timeout_seconds)
            eos_at = getattr(self.wake_provider, 'command_eos_at', None)
            self.diagnostics.mark('end_of_speech_detected', eos_at if isinstance(eos_at, (float, int)) else None)
            speech_at = getattr(self.wake_provider, 'command_speech_ended_at', None)
            if isinstance(speech_at, (float, int)):
                self.diagnostics.mark('speech_ended_at', speech_at)
            if not pcm or self.privacy_enabled() or not self.active:
                return ""
            self.full_stt_invocations += 1
            self.diagnostics.mark('stt_start')
            try:
                return self.stt_provider.transcriber.transcribe_pcm(pcm)
            finally:
                self.diagnostics.mark('stt_done')
        self.full_stt_invocations += 1
        self.wake_provider.stop()
        if isinstance(self.stt_provider, FasterWhisperSTTProvider):
            from .post_wake import record_command
            cancel_event = self._cancel_event
            pcm = record_command(self.stt_provider.microphone_id, timeout_seconds, cancelled=cancel_event.is_set, gain_mode=self.settings.microphone_gain)
            self.diagnostics.mark('end_of_speech_detected')
            self.diagnostics.mark('stt_start')
            try:
                return self.stt_provider.transcriber.transcribe_pcm(pcm)
            finally:
                self.diagnostics.mark('stt_done')
        try:
            return self.stt_provider.listen_once(timeout_seconds)
        finally:
            self.diagnostics.mark('stt_done')

    def transcribe_retry(self, timeout_seconds=8):
        self.prepare_capture()
        session = self._cancel_event
        try:
            with self._capture_lock:
                if session.is_set():
                    return ''
                result = self._transcribe_retry(timeout_seconds)
            if self._cancel_event is not session or session.is_set():
                return ''
            result = self._finish_capture(result)
            if self._cancel_event is session and not session.is_set():
                self.transition('processing')
            return result
        except BaseException:
            if self._cancel_event is session:
                self.cleanup()
            raise

    def _transcribe_retry(self, timeout_seconds=8):
        if self.privacy_enabled():
            return ''
        from .kws import StreamingKwsWakeWordProvider
        if isinstance(self.wake_provider, StreamingKwsWakeWordProvider):
            self.wake_provider.begin_command()
            return self._transcribe_after_wake(timeout_seconds)
        from .post_wake import record_command
        self.diagnostics.begin({})
        self._trace_pending = True
        self.diagnostics.mark('wake_event_at')
        self.diagnostics.mark('recording_start')
        self.wake_provider.stop()
        cancel_event = self._cancel_event
        pcm = record_command(self.settings.microphone_id, timeout_seconds, cancelled=cancel_event.is_set, gain_mode=self.settings.microphone_gain)
        self.diagnostics.mark('end_of_speech_detected')
        if self.privacy_enabled():
            self.diagnostics.clear()
            return ''
        self.diagnostics.mark('stt_start')
        self.full_stt_invocations += 1
        try:
            return self.stt_provider.transcriber.transcribe_pcm(pcm)
        finally:
            self.diagnostics.mark('stt_done')

    def cancel(self) -> None:
        self.cleanup()
        self.status_callback("已取消語音指令")

    def test_microphone(self, duration_seconds: float = 2.0) -> MicrophoneTestResult:
        tester = getattr(self.wake_provider, "test_microphone", None)
        if callable(tester):
            return tester(duration_seconds)
        return MicrophoneTestResult("unsupported", "這個 wake backend 沒有麥克風輸入測試")

    def test_wake_word(self, duration_seconds: float = 8.0) -> WakeTestResult:
        state = self.readiness()
        if state.state != "ready":
            return WakeTestResult(state.state, state.message)
        tester = getattr(self.wake_provider, "test_wake_word", None)
        if callable(tester):
            return tester(duration_seconds)
        return WakeTestResult("unsupported", "這個 wake backend 沒有喚醒詞測試")

    def template_status(self) -> WakeTemplateStatus:
        matcher = getattr(self.wake_provider, "matcher", None)
        if matcher and hasattr(matcher, "template_status"):
            return matcher.template_status(self.settings)
        state = getattr(self.wake_provider, "runtime_state", self.runtime_state)
        return WakeTemplateStatus(state.state == "standby", state.state, state.message)

    def clear_wake_template(self) -> None:
        matcher = getattr(self.wake_provider, "matcher", None)
        store = getattr(matcher, "store", None)
        if store and hasattr(store, "clear"):
            store.clear(self.settings.template_id)

    def enroll_from_features(
        self,
        features: list[tuple[tuple[float, ...], ...]],
        validation_features: tuple[tuple[float, ...], ...] | None = None,
    ) -> EnrollmentResult:
        matcher = getattr(self.wake_provider, "matcher", None)
        if matcher and hasattr(matcher, "enroll_features"):
            return matcher.enroll_features(self.settings.assistant_name, features, validation_features)
        return EnrollmentResult(False, "這個 wake backend 不支援訓練")

    def enrollment_sample(self, duration_seconds: float = 2.5) -> EnrollmentSampleResult:
        sampler = getattr(self.wake_provider, "enrollment_sample", None)
        if callable(sampler):
            return sampler(duration_seconds)
        return EnrollmentSampleResult(False, "這個 wake backend 不支援訓練")


def voice_command_status(
    enabled: bool,
    component_state: str = "",
    error: str = "",
    runtime_state: WakeRuntimeState | None = None,
) -> VoiceCommandStatus:
    if not enabled:
        return VoiceCommandStatus(False, "語音助理未啟用", component_state or "disabled")
    if runtime_state and runtime_state.state in {"standby", "voice", "recognizing", "woken", "mic-no-signal"}:
        return VoiceCommandStatus(True, runtime_state.message, runtime_state.state)
    if runtime_state and runtime_state.state in {"starting", "error", "unsupported", "needs-training"}:
        return VoiceCommandStatus(False, runtime_state.message, runtime_state.state)
    state = component_state or detect_voice_components().state
    if state == "ready":
        return VoiceCommandStatus(False, "語音元件已安裝，正在確認麥克風輸入", "starting")
    if state == "installing":
        return VoiceCommandStatus(False, "語音元件安裝中；文字指令列可照常使用", "installing")
    if state == "error":
        suffix = f"：{error}" if error else ""
        return VoiceCommandStatus(False, f"語音元件安裝失敗，可重試{suffix}", "error")
    return VoiceCommandStatus(False, "首次啟用需下載語音元件；文字指令列可照常使用", "not_installed")


def detect_voice_components() -> VoiceInstallState:
    missing: list[str] = []
    incompatible = False
    for name in ("faster_whisper", "sounddevice", "sherpa_onnx", "pypinyin", "opencc", "sentencepiece"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            incompatible = incompatible or not isinstance(exc, ModuleNotFoundError)
            logger.exception("Voice dependency import failed: %s", name)
            missing.append(f"{name}：{type(exc).__name__}")
    if missing:
        return VoiceInstallState("error_dependency" if incompatible else "missing_components", "語音元件載入失敗：" + ", ".join(missing))
    try:
        from .vad import create_vad
        create_vad().is_speech(bytes(960), 16000)
    except Exception as exc:
        return VoiceInstallState("error_dependency", "VAD 元件載入失敗，請安裝／重試語音元件")
    return VoiceInstallState("ready", "Dependencies 驗證完成；STT 模型按需載入")


def install_voice_dependencies(
    project_root: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    progress: Callable[[WakeRuntimeState], None] | None = None,
) -> VoiceInstallState:
    if getattr(sys, "frozen", False):
        state = detect_voice_components()
        if state.state == "ready":
            return VoiceInstallState("verified", "內建語音元件驗證完成，正在重新載入語音管線")
        return VoiceInstallState("error_dependency", "內建語音元件不完整，請重新解壓完整朋友測試包")
    from importlib import metadata
    tracked = {"webrtcvad-wheels": "_webrtcvad", "sherpa-onnx": "sherpa_onnx", "ctranslate2": "ctranslate2", "sounddevice": "sounddevice", "numpy": "numpy"}
    def versions():
        result = {}
        for package in tracked:
            try:
                result[package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                result[package] = None
        return result
    before = versions()
    loaded = {package for package, module in tracked.items() if module in sys.modules}
    uv = project_root / "tools" / "uv" / "uv.exe"
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    if not executable.is_file():
        return VoiceInstallState("error_dependency", "找不到目前助理的 Python 環境")
    logger.debug("Voice installer runtime=%s version=%s", executable, sys.version.split()[0])
    command = [str(uv) if uv.exists() else "uv", "pip", "install", "--python", str(executable)]
    requirements = project_root / "requirements-voice.txt"
    command.extend(["-r", str(requirements)] if requirements.exists() else VOICE_OPTIONAL_DEPENDENCIES)
    try:
        completed = runner(command, cwd=str(project_root), text=True, capture_output=True, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode:
            logger.error("Voice installation failed (exit %s)", completed.returncode)
            return VoiceInstallState("error_dependency", "語音元件安裝失敗，請檢查網路後重試")
        if progress:
            progress(WakeRuntimeState("verifying", "正在驗證本機語音元件…"))
        probe = runner([str(executable), "-c", "from personal_ai_assistant.voice_command import detect_voice_components; import sys; r=detect_voice_components(); print(r.message); sys.exit(0 if r.state == 'ready' else 1)"],
                       cwd=str(project_root), text=True, capture_output=True, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if probe.returncode:
            return VoiceInstallState("error_dependency", "語音元件驗證失敗，請查看語音診斷")
        after = versions()
        if any(before[package] != after[package] for package in loaded):
            return VoiceInstallState("restart_required", "已更新正在使用的原生元件，請重新啟動助理完成驗證")
        importlib.invalidate_caches()
        local = detect_voice_components()
        if local.state != "ready":
            return VoiceInstallState("restart_required", "元件已更新，請重新啟動助理完成驗證")
        return VoiceInstallState("verified", "元件驗證完成，正在重新載入語音管線")
    except OSError:
        logger.exception("Voice installer failed")
        return VoiceInstallState("error_dependency", "無法啟動目前環境的語音元件安裝")


def normalize_phrase(value: str) -> str:
    return "".join((value or "").casefold().split()).strip("，,。.!！?？:：")


def normalize_wake_aliases(assistant_name: str, aliases: tuple[str, ...] = ()) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in (assistant_name, f"Hey {assistant_name}" if assistant_name else "", *aliases):
        cleaned = " ".join(str(item).strip().split())
        key = normalize_phrase(cleaned)
        if cleaned and key not in seen:
            values.append(cleaned)
            seen.add(key)
    return tuple(values)


def wake_aliases_for_settings(settings: WakeSettings) -> tuple[str, ...]:
    return normalize_wake_aliases(settings.assistant_name, settings.wake_aliases)


def canonical_wake_text(value: str) -> str:
    replacements = {
        "麗": "莉",
        "丽": "莉",
        "裡": "莉",
        "里": "莉",
        "哩": "莉",
        "利": "莉",
        "絲": "丝",
        "斯": "丝",
        "思": "丝",
        "私": "丝",
    }
    return "".join(replacements.get(char, char) for char in normalize_phrase(value))


def wake_phrase_matches(text: str, aliases: tuple[str, ...], threshold: float = 0.84) -> WakeMatchResult:
    normalized_text = normalize_phrase(text)
    canonical_text = canonical_wake_text(text)
    for alias in aliases:
        alias_normalized = normalize_phrase(alias)
        if alias_normalized and normalized_text.startswith(alias_normalized):
            return WakeMatchResult(True, normalized_text, alias, 1.0)
    for alias in aliases:
        alias_canonical = canonical_wake_text(alias)
        if not alias_canonical:
            continue
        candidate = canonical_text[: len(alias_canonical)]
        if candidate == alias_canonical:
            return WakeMatchResult(True, normalized_text, alias, 0.98)
        if len(alias_canonical) >= 4:
            score = difflib.SequenceMatcher(None, candidate, alias_canonical).ratio()
            if score >= threshold:
                return WakeMatchResult(True, normalized_text, alias, score)
    return WakeMatchResult(False, normalized_text, "", 0.0)


def strip_wake_phrase(text: str, aliases: tuple[str, ...]) -> str:
    stripped = " ".join((text or "").strip().split())
    normalized = normalize_phrase(stripped)
    for alias in sorted(aliases, key=len, reverse=True):
        alias_normalized = normalize_phrase(alias)
        if not alias_normalized:
            continue
        exact_prefix = normalized.startswith(alias_normalized)
        fuzzy_prefix = wake_phrase_matches(stripped, (alias,)).matched
        if not exact_prefix and not fuzzy_prefix:
            continue
        remainder = stripped
        for char_count in range(len(stripped) + 1):
            candidate = stripped[:char_count]
            candidate_normalized = normalize_phrase(candidate)
            if candidate_normalized == alias_normalized or wake_phrase_matches(candidate, (alias,)).matched:
                remainder = stripped[char_count:]
                break
        return remainder.strip(" ，,。.!！?？:：")
    return stripped


def validate_assistant_name(name: str) -> WakeNameValidation:
    normalized = normalize_phrase(name)
    risky = len(normalized) <= 1 or normalized in {"ai", "欸", "喂", "hey", "hi", "hello"}
    if risky:
        return WakeNameValidation(True, "這個名稱容易誤觸；你仍可確認後使用。")
    return WakeNameValidation(False, "")


def settings_from_assistant_settings(settings: object) -> WakeSettings:
    return WakeSettings(
        assistant_name=str(getattr(settings, "assistant_name", "") or ""),
        wake_aliases=tuple(getattr(settings, "wake_aliases", ()) or ()),
        microphone_id=str(getattr(settings, "voice_microphone_id", "") or ""),
        wake_sound_enabled=bool(getattr(settings, "wake_sound_enabled", True)),
        wake_display=str(getattr(settings, "voice_wake_display", "osd") or "osd"),
        allow_during_games=bool(getattr(settings, "voice_allow_during_games", False)),
        stt_device=str(getattr(settings, "voice_stt_device", "auto") or "auto"),
        sensitivity=str(getattr(settings, "voice_wake_sensitivity", "auto") or "auto"),
        microphone_gain=str(getattr(settings, "voice_microphone_gain", "auto") or "auto"),
        template_id=str(getattr(settings, "voice_wake_template_id", "") or ""),
    )


def with_assistant_name(settings: WakeSettings, name: str, aliases: tuple[str, ...] = ()) -> WakeSettings:
    return replace(settings, assistant_name=name.strip(), wake_aliases=aliases)


def sensitivity_threshold(level: str, template: dict[str, object] | None = None) -> float:
    if template and isinstance(template.get("baseline_threshold"), (int, float)):
        baseline = float(template.get("baseline_threshold"))
        offset = WAKE_SENSITIVITY_OFFSETS.get(level, WAKE_SENSITIVITY_OFFSETS["standard"])
        return max(0.50, min(0.97, baseline + offset))
    return WAKE_SENSITIVITY_THRESHOLDS.get(level, WAKE_SENSITIVITY_THRESHOLDS["standard"])


def similarity_label(score: float) -> str:
    if score >= 0.90:
        return "高"
    if score >= 0.75:
        return "中"
    return "低"


def friendly_score(score: float) -> int:
    return max(0, min(100, int(round(score * 100))))


def enrollment_sample_from_pcm(pcm: bytes, sample_rate: int = VOICE_SAMPLE_RATE) -> EnrollmentSampleResult:
    if not pcm:
        return EnrollmentSampleResult(False, "沒有抓到聲音")
    raw_voice_seconds = voice_activity_seconds(pcm, sample_rate)
    if raw_voice_seconds > 2.2:
        has_signal, peak, rms = pcm_has_signal(pcm)
        return EnrollmentSampleResult(False, "這次太長，請只唸助理名稱", peak=peak, duration_seconds=raw_voice_seconds, rms=rms)
    segment = wake_segment_from_pcm(pcm, sample_rate)
    has_signal, peak, rms = pcm_has_signal(segment.pcm)
    if not has_signal or peak < 0.018:
        return EnrollmentSampleResult(False, "音量太低，請靠近一點再說一次", peak=peak, rms=rms, segment_complete=segment.complete)
    if peak >= 0.98:
        return EnrollmentSampleResult(False, "音量太大或有爆音，請離麥克風遠一點再說一次", peak=peak, rms=rms, segment_complete=segment.complete)
    if not segment.has_voice:
        return EnrollmentSampleResult(False, "沒有偵測到人聲，請再說一次", peak=peak, rms=rms, segment_complete=segment.complete)
    duration_seconds = segment.duration_seconds
    if duration_seconds > 2.2:
        return EnrollmentSampleResult(False, "這次太長，請只唸助理名稱", peak=peak, duration_seconds=duration_seconds, rms=rms, segment_complete=segment.complete)
    if duration_seconds < 0.18:
        return EnrollmentSampleResult(False, "沒有抓到完整語音，請再說一次", peak=peak, duration_seconds=duration_seconds, rms=rms, segment_complete=segment.complete)
    features = acoustic_features_from_pcm(segment.pcm, sample_rate)
    if not features:
        return EnrollmentSampleResult(False, "沒有抓到有效語音，請再說一次", peak=peak, duration_seconds=duration_seconds, rms=rms, segment_complete=segment.complete)
    return EnrollmentSampleResult(True, "收到", features, peak, duration_seconds, rms, segment.complete)


def acoustic_features_from_pcm(pcm: bytes, sample_rate: int = VOICE_SAMPLE_RATE) -> tuple[tuple[float, ...], ...]:
    # Shared train/runtime contract: mono little-endian signed PCM16.
    # FFT energy integrated over triangular mel bands is less pitch-sensitive
    # than sampling eight isolated DFT frequencies.
    import numpy as np
    if sample_rate < 8000 or len(pcm) < 2:
        return ()
    x = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype='<i2').astype(np.float64)
    x -= np.median(x)
    scale = float(np.percentile(np.abs(x), 95))
    if scale < 1:
        return ()
    x = np.clip(x / scale, -3, 3)
    size, hop = int(sample_rate * .025), int(sample_rate * .010)
    if len(x) < size:
        return ()
    frames = np.lib.stride_tricks.sliding_window_view(x, size)[::hop].copy()
    energies = np.mean(frames ** 2, axis=1)
    # Relative gate follows microphone gain; keep quiet consonant frames.
    frames = frames[energies > max(float(energies.max()) * .0005, 1e-8)]
    if not len(frames):
        return ()
    nfft = 1 << (size - 1).bit_length()
    power = np.abs(np.fft.rfft(frames * np.hanning(size), n=nfft)) ** 2
    mel = lambda hz: 2595 * np.log10(1 + hz / 700)
    points = 700 * (10 ** (np.linspace(mel(80), mel(min(7600, sample_rate / 2)), 26) / 2595) - 1)
    frequencies = np.fft.rfftfreq(nfft, 1 / sample_rate)
    filters = np.maximum(0, np.minimum(
        (frequencies[None, :] - points[:-2, None]) / (points[1:-1, None] - points[:-2, None]),
        (points[2:, None] - frequencies[None, :]) / (points[2:, None] - points[1:-1, None])))
    bands = power @ filters.T
    # Relative log power removes AGC level while retaining spectral shape.
    bands /= np.maximum(bands.max(axis=1, keepdims=True), 1e-12)
    features = np.log(np.maximum(bands, .001)) / 6.907755
    return tuple(tuple(float(v) for v in frame) for frame in features)


def sequence_similarity(
    left: tuple[tuple[float, ...], ...],
    right: tuple[tuple[float, ...], ...],
) -> float:
    if not left or not right:
        return 0.0
    distance = _dtw_distance(left, right)
    length_penalty = abs(len(left) - len(right)) / max(len(left), len(right), 1)
    return max(0.0, min(1.0, math.exp(-2.8 * distance) * (1.0 - 0.25 * length_penalty)))


def multi_template_similarity(
    candidate: tuple[tuple[float, ...], ...],
    samples: list[tuple[tuple[float, ...], ...]],
) -> tuple[float, int, list[float]]:
    if not candidate or not samples:
        return 0.0, 0, []
    scores = [keyword_window_similarity(candidate, sample) for sample in samples]
    ranked = sorted(scores, reverse=True)
    top_k = ranked[: max(1, min(3, len(ranked)))]
    best = ranked[0] if ranked else 0.0
    aggregate = sum(top_k) / len(top_k)
    score = 0.65 * best + 0.35 * aggregate
    return score, scores.index(best) + 1, scores


def keyword_window_similarity(
    candidate: tuple[tuple[float, ...], ...],
    template: tuple[tuple[float, ...], ...],
) -> float:
    if not candidate or not template:
        return 0.0
    if len(candidate) <= max(len(template) * 1.45, len(template) + 12):
        return sequence_similarity(candidate, template)
    window = max(4, int(len(template) * 1.25))
    step = max(1, len(template) // 5)
    best = 0.0
    for start in range(0, max(1, len(candidate) - window + 1), step):
        best = max(best, sequence_similarity(candidate[start : start + window], template))
    return best


def enrollment_quality(samples: list[tuple[tuple[float, ...], ...]]) -> dict[str, object]:
    pairwise = _pairwise_similarities(samples)
    if not pairwise:
        return {"ready": False, "label": "建議重錄", "score": 0.0, "message": "至少需要更多有效錄音"}
    mean_score = sum(pairwise) / len(pairwise)
    min_score = min(pairwise)
    sample_scores = []
    for index, sample in enumerate(samples):
        others = [sequence_similarity(sample, other) for other_index, other in enumerate(samples) if other_index != index]
        sample_scores.append(sum(others) / len(others) if others else 0.0)
    median_score = sorted(sample_scores)[len(sample_scores) // 2]
    outliers = [index + 1 for index, score in enumerate(sample_scores) if score < median_score - 0.12 and score < WAKE_MIN_CONSISTENCY]
    if outliers:
        return {
            "ready": False,
            "label": "建議重錄",
            "score": mean_score,
            "message": f"請只重錄第 {outliers[0]} 次，其餘樣本已保留",
            "outliers": outliers,
        }
    label = "良好" if mean_score >= 0.88 and min_score >= 0.82 else "普通"
    return {"ready": True, "label": label, "score": mean_score, "message": "訓練品質可用"}


def calibrated_baseline_threshold(samples: list[tuple[tuple[float, ...], ...]]) -> float:
    pairwise = _pairwise_similarities(samples)
    if not pairwise:
        return WAKE_SENSITIVITY_THRESHOLDS["standard"]
    import statistics
    center = statistics.median(pairwise)
    spread = statistics.median(abs(value - center) for value in pairwise)
    return max(0.58, min(0.94, center - max(.06, 2.5 * spread)))


def _pairwise_similarities(samples: list[tuple[tuple[float, ...], ...]]) -> list[float]:
    scores: list[float] = []
    for left_index, left in enumerate(samples):
        for right in samples[left_index + 1 :]:
            scores.append(sequence_similarity(left, right))
    return scores


def _dtw_distance(left: tuple[tuple[float, ...], ...], right: tuple[tuple[float, ...], ...]) -> float:
    previous = [float("inf")] * (len(right) + 1)
    previous[0] = 0.0
    for left_frame in left:
        current = [float("inf")] * (len(right) + 1)
        for index, right_frame in enumerate(right, start=1):
            cost = _frame_distance(left_frame, right_frame)
            current[index] = cost + min(current[index - 1], previous[index], previous[index - 1])
        previous = current
    return previous[-1] / max(len(left), len(right), 1)


def _frame_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    count = min(len(left), len(right))
    if count <= 0:
        return 1.0
    return math.sqrt(sum((left[i] - right[i]) ** 2 for i in range(count)) / count)


def _pre_emphasis(samples: list[int], coefficient: float = 0.97) -> list[float]:
    if not samples:
        return []
    emphasized = [float(samples[0])]
    for current, previous in zip(samples[1:], samples):
        emphasized.append(float(current) - coefficient * float(previous))
    return emphasized


def _log_spectral_bands(frame: list[float], sample_rate: int) -> tuple[float, ...]:
    if not frame:
        return (0.0,) * 8
    windowed = _hann_window(frame)
    centers = (180, 280, 420, 620, 900, 1300, 1900, 2800)
    magnitudes: list[float] = []
    for frequency in centers:
        real = 0.0
        imag = 0.0
        angular = 2.0 * math.pi * frequency / sample_rate
        for index, value in enumerate(windowed):
            angle = angular * index
            real += value * math.cos(angle)
            imag -= value * math.sin(angle)
        magnitudes.append(math.sqrt(real * real + imag * imag))
    total = sum(magnitudes) or 1.0
    return tuple(round(math.log1p(value / total * 10.0), 4) for value in magnitudes)


def _hann_window(frame: list[float]) -> list[float]:
    if len(frame) <= 1:
        return frame
    scale = len(frame) - 1
    return [value * (0.5 - 0.5 * math.cos(2.0 * math.pi * index / scale)) for index, value in enumerate(frame)]


def _pcm_int16_samples(pcm: bytes) -> list[int]:
    return [int.from_bytes(pcm[index : index + 2], "little", signed=True) for index in range(0, len(pcm) - 1, 2)]


def record_microphone_pcm(microphone_id: str, duration_seconds: float, sample_rate: int = VOICE_SAMPLE_RATE) -> bytes:
    sounddevice = _sounddevice_module()
    device_index = sounddevice_index_from_identifier(microphone_id)
    frames_per_block = int(sample_rate * VOICE_FRAME_MS / 1000)
    chunks: list[bytes] = []
    errors: list[str] = []

    def callback(indata, _frames, _time_info, status) -> None:
        if status:
            errors.append(str(status))
        chunks.append(bytes(indata))

    stream = sounddevice.RawInputStream(
        samplerate=sample_rate,
        blocksize=frames_per_block,
        device=device_index,
        channels=1,
        dtype="int16",
        callback=callback,
    )
    try:
        stream.start()
        time.sleep(max(0.1, duration_seconds))
    finally:
        stream.stop()
        stream.close()
    if errors:
        logger.debug("Microphone test stream status: %s", "; ".join(errors[-3:]))
    return b"".join(chunks)


def analyze_microphone_pcm(pcm: bytes, sample_rate: int = VOICE_SAMPLE_RATE) -> MicrophoneTestResult:
    if not pcm:
        return MicrophoneTestResult("mic-no-signal", "麥克風無訊號")
    has_signal, peak, rms = pcm_has_signal(pcm)
    if not has_signal:
        return MicrophoneTestResult("mic-no-signal", "麥克風無訊號", peak=peak, rms=rms)
    has_voice = pcm_has_voice(pcm, sample_rate)
    message = f"有收到聲音，音量 {int(peak * 100)}%"
    if has_voice:
        message += "，偵測到人聲"
    else:
        message += "，尚未偵測到人聲"
    return MicrophoneTestResult("voice" if has_voice else "signal", message, True, has_voice, peak, rms)


def pcm_has_signal(pcm: bytes, threshold: float = 0.012) -> tuple[bool, float, float]:
    if not pcm:
        return False, 0.0, 0.0
    sample_count = len(pcm) // 2
    if sample_count <= 0:
        return False, 0.0, 0.0
    peak = 0
    square_sum = 0
    for index in range(0, len(pcm) - 1, 2):
        value = int.from_bytes(pcm[index : index + 2], "little", signed=True)
        absolute = abs(value)
        peak = max(peak, absolute)
        square_sum += value * value
    peak_level = peak / 32768.0
    rms_level = (square_sum / sample_count) ** 0.5 / 32768.0
    return peak_level >= threshold or rms_level >= threshold / 2, peak_level, rms_level


def pcm_has_voice(pcm: bytes, sample_rate: int = VOICE_SAMPLE_RATE, aggressiveness: int = 2) -> bool:
    try:
        webrtcvad = _webrtcvad_module()
        vad = webrtcvad.Vad(aggressiveness)
    except Exception:
        return pcm_has_signal(pcm)[0]
    voiced = 0
    total = 0
    for frame in _pcm_frames(pcm, int(sample_rate * VOICE_FRAME_MS / 1000) * 2):
        total += 1
        try:
            if vad.is_speech(frame, sample_rate):
                voiced += 1
        except Exception:
            pass
    return total > 0 and voiced / total >= 0.12


def voice_activity_seconds(pcm: bytes, sample_rate: int = VOICE_SAMPLE_RATE, aggressiveness: int = 2) -> float:
    frames = list(_pcm_frames(pcm, int(sample_rate * VOICE_FRAME_MS / 1000) * 2))
    if not frames:
        return 0.0
    flags = _voice_frame_flags(frames, sample_rate, aggressiveness)
    return sum(1 for flag in flags if flag) * VOICE_FRAME_MS / 1000.0


def wake_segment_from_pcm(
    pcm: bytes,
    sample_rate: int = VOICE_SAMPLE_RATE,
    prefix_seconds: float = 1.6,
    aggressiveness: int = 2,
    padding_ms: int = WAKE_SEGMENT_PADDING_MS,
) -> WakeSegment:
    if not pcm:
        return WakeSegment(b"", 0.0, False, False)
    frames = list(_pcm_frames(pcm, int(sample_rate * VOICE_FRAME_MS / 1000) * 2))
    if not frames:
        duration = len(pcm) / 2 / sample_rate
        has_signal, peak, rms = pcm_has_signal(pcm)
        return WakeSegment(pcm, duration, has_signal, True, peak, rms)
    flags = _voice_frame_flags(frames, sample_rate, aggressiveness)
    voiced_indices = [index for index, voiced in enumerate(flags) if voiced]
    if not voiced_indices:
        fallback = pcm[: int(sample_rate * prefix_seconds) * 2]
        has_signal, peak, rms = pcm_has_signal(fallback)
        return WakeSegment(fallback, len(fallback) / 2 / sample_rate, False, False, peak, rms)
    pad_frames = max(1, int(math.ceil(padding_ms / VOICE_FRAME_MS)))
    max_frames = max(1, int(prefix_seconds * 1000 / VOICE_FRAME_MS))
    start = max(0, voiced_indices[0] - pad_frames)
    end = min(len(frames), voiced_indices[-1] + pad_frames + 1)
    if end - start > max_frames:
        end = min(len(frames), start + max_frames)
    segment = b"".join(frames[start:end])
    has_signal, peak, rms = pcm_has_signal(segment)
    complete = start < voiced_indices[0] and end > voiced_indices[-1]
    return WakeSegment(segment, len(segment) / 2 / sample_rate, True, complete, peak, rms)


def _voice_frame_flags(frames: list[bytes], sample_rate: int, aggressiveness: int) -> list[bool]:
    try:
        webrtcvad = _webrtcvad_module()
        vad = webrtcvad.Vad(aggressiveness)
    except Exception:
        return [pcm_has_signal(frame, threshold=0.008)[0] for frame in frames]
    flags: list[bool] = []
    for frame in frames:
        has_signal, _peak, _rms = pcm_has_signal(frame, threshold=0.006)
        try:
            flags.append(has_signal and vad.is_speech(frame, sample_rate))
        except Exception:
            flags.append(has_signal)
    if not any(flags):
        return [pcm_has_signal(frame, threshold=0.008)[0] for frame in frames]
    return flags


def first_voice_prefix(
    pcm: bytes,
    sample_rate: int = VOICE_SAMPLE_RATE,
    prefix_seconds: float = 1.6,
    aggressiveness: int = 2,
) -> bytes:
    return wake_segment_from_pcm(pcm, sample_rate, prefix_seconds, aggressiveness).pcm


def _pcm_frames(pcm: bytes, frame_bytes: int = VOICE_FRAME_BYTES):
    for index in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        yield pcm[index : index + frame_bytes]


def _sounddevice_module():
    try:
        import sounddevice

        return sounddevice
    except Exception as exc:
        raise RuntimeError("缺少 sounddevice，請安裝語音元件") from exc


def _webrtcvad_module():
    from types import SimpleNamespace
    from .vad import create_vad
    return SimpleNamespace(Vad=create_vad)


def _format_wake_test_message(has_audio: bool, has_voice: bool, transcript: str, match: WakeMatchResult) -> str:
    if not has_audio:
        return "麥克風無訊號"
    if not has_voice:
        return "有收到聲音，但沒有偵測到人聲"
    if match.matched:
        return f"有聽到聲音 → 辨識為「{transcript or '空白'}」→ 喚醒匹配成功"
    return f"有聽到聲音 → 辨識為「{transcript or '空白'}」→ 未匹配喚醒詞"


def _format_acoustic_wake_test_message(
    has_audio: bool,
    has_voice: bool,
    assistant_name: str,
    acoustic: AcousticMatchResult,
    transcript: str = "",
) -> str:
    if not has_audio:
        return "麥克風無訊號"
    if not has_voice:
        return "有收到聲音，但沒有偵測到人聲"
    result = f"結果：匹配「{assistant_name}」" if acoustic.matched else "結果：未匹配"
    template = (
        f"\nbest template：{acoustic.best_template_index}/{acoustic.template_count}"
        if acoustic.template_count
        else "\nbest template：無"
    )
    segment_state = "完整" if acoustic.segment_complete else "可能缺少開頭或結尾"
    near_threshold = acoustic.score >= max(0.0, acoustic.threshold - 0.04)
    if acoustic.matched:
        hint = ""
    elif near_threshold:
        hint = "\n提示：分數接近門檻，可以提高靈敏度再試一次"
    else:
        hint = "\n提示：分數偏低，建議重新訓練"
    diagnostics = (
        f"\n匹配強度：{acoustic.friendly_score}/100"
        f"{template}"
        f"\nsegment：{acoustic.segment_seconds:.2f} 秒，{segment_state}"
        f"\nthreshold：{friendly_score(acoustic.threshold)}/100，{'已越過' if acoustic.crossed_threshold else '未越過'}"
    )
    stt = f"\nSTT 辨識：{transcript or '空白'}（僅供參考）" if transcript is not None else ""
    return f"有聽到聲音\n喚醒詞相似度：{acoustic.similarity_label}\n{result}{diagnostics}{hint}{stt}"


def write_debug_wav(path: Path, pcm: bytes, sample_rate: int = VOICE_SAMPLE_RATE) -> None:
    """Test helper only. Runtime code must not persist microphone audio."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
