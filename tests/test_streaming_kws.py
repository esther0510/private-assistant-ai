import os
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from personal_ai_assistant.kws import (
    PcmRingBuffer, StreamingKwsWakeWordProvider, SherpaKeywordEngine,
    UnsupportedKeyword, UNSUPPORTED,
)
from personal_ai_assistant.voice_command import (
    VoiceAssistantPipeline, WakeSettings, WakeRuntimeState, WakeTemplateStore,
)


class FakeEngine:
    def __init__(self):
        self.frames = []
        self.result = ""
        self.resets = 0

    def reset(self):
        self.resets += 1

    def accept(self, pcm):
        self.frames.append(pcm)
        return self.result


class StreamingKwsTests(unittest.TestCase):
    def provider(self):
        provider = StreamingKwsWakeWordProvider()
        provider.settings = WakeSettings("賈維斯")
        provider.engine = FakeEngine()
        return provider

    def test_default_provider_has_no_enrollment_dependency(self):
        pipeline = VoiceAssistantPipeline(WakeSettings("賈維斯"))
        self.assertIsInstance(pipeline.wake_provider, StreamingKwsWakeWordProvider)
        self.assertFalse(hasattr(pipeline.wake_provider, "matcher"))

    def test_old_template_metadata_does_not_block_migration(self):
        with patch.object(WakeTemplateStore, "status", side_effect=AssertionError("legacy gate")):
            pipeline = VoiceAssistantPipeline(WakeSettings("賈維斯", template_id="old-missing-template"))
            self.assertEqual(pipeline.wake_provider.settings.assistant_name, "賈維斯")

    def test_name_change_reconfigures_and_restarts(self):
        provider = Mock()
        provider.start.return_value = WakeRuntimeState("standby", "待機中")
        pipeline = VoiceAssistantPipeline(WakeSettings("賈維斯"), wake_provider=provider)
        pipeline.start()
        pipeline.reload(WakeSettings("莉莉絲"))
        provider.configure_settings.assert_called_with(WakeSettings("莉莉絲"))
        self.assertEqual(provider.start.call_count, 2)

    def test_ring_retains_exact_half_second(self):
        ring = PcmRingBuffer()
        ring.append(b"a" * 10000)
        ring.append(b"b" * 10000)
        self.assertEqual(ring.snapshot(), b"a" * 6000 + b"b" * 10000)

    def test_frames_arrive_once_in_order_across_vad_trigger(self):
        provider = self.provider()
        provider.consume(b"a" * 16000, False)
        provider.consume(b"b" * 960, True)
        self.assertEqual(provider.engine.frames, [b"a" * 16000, b"b" * 960])
        self.assertEqual(provider.engine.resets, 0)

    def test_silence_continuously_feeds_decoder(self):
        provider = self.provider()
        for _ in range(100):
            provider.consume(bytes(960), False)
        self.assertEqual(provider.engine.frames, [bytes(960)] * 100)
        self.assertEqual(provider.engine.resets, 0)
        self.assertEqual(len(provider.ring.snapshot()), 16000)

    def test_unrelated_speech_does_not_wake(self):
        provider = self.provider()
        provider.consume(bytes(960), True)
        self.assertFalse(provider.poll())

    def test_wake_emits_once_even_with_repeated_keyword(self):
        provider = self.provider()
        provider.engine.result = "賈維斯"
        provider.consume(bytes(960), True, now=10)
        self.assertTrue(provider.poll())
        for _ in range(10):
            provider.consume(bytes(960), True, now=20)
        self.assertFalse(provider.poll())

    def test_short_debounce_survives_stop(self):
        provider = self.provider()
        provider.engine.result = "賈維斯"
        provider.consume(bytes(960), True, now=10)
        provider.stop()
        provider._stop = threading.Event()
        provider.consume(bytes(960), True, now=10.2)
        self.assertFalse(provider.poll())
        provider.consume(bytes(960), True, now=13)
        self.assertTrue(provider.poll())

    def test_full_stt_starts_only_after_wake(self):
        provider = self.provider()
        provider.start = lambda: WakeRuntimeState("standby", "待機中")
        stt = Mock(spec=["configure", "listen_once"])
        stt.listen_once.return_value = "賈維斯 幫我開 Steam"
        pipeline = VoiceAssistantPipeline(WakeSettings("賈維斯"), wake_provider=provider, stt_provider=stt)
        pipeline.start()
        pipeline.idle_tick()
        stt.listen_once.assert_not_called()
        provider.engine.result = "賈維斯"
        provider.consume(bytes(960), True)
        self.assertTrue(pipeline.idle_tick())
        pipeline.handle_wake()
        stt.listen_once.assert_called_once()

    def test_handoff_preserves_command_audio(self):
        provider = self.provider()
        provider.engine.result = "賈維斯"
        provider.consume(b"a" * 960, True)
        provider.consume(b"b" * 960, True)
        self.assertEqual(provider.capture_command(0), b"a" * 960 + b"b" * 960)
        self.assertFalse(provider._command)

    def test_privacy_stops_stream_and_clears_audio(self):
        provider = self.provider()
        provider._mic = Mock()
        mic = provider._mic
        provider.start = lambda: WakeRuntimeState("standby", "待機中")
        privacy = [False]
        pipeline = VoiceAssistantPipeline(WakeSettings("賈維斯"), wake_provider=provider, privacy_enabled=lambda: privacy[0])
        pipeline.start()
        provider.consume(bytes(960), False)
        privacy[0] = True
        self.assertFalse(pipeline.idle_tick())
        mic.abort.assert_called_once()
        self.assertFalse(provider.ring.snapshot())
        self.assertFalse(pipeline.active)

    def test_disabled_stops_kws(self):
        provider = self.provider()
        pipeline = VoiceAssistantPipeline(WakeSettings("賈維斯"), wake_provider=provider)
        pipeline.stop()
        provider.engine.result = "賈維斯"
        provider.consume(bytes(960), True)
        self.assertFalse(provider.poll())

    def test_unsupported_runtime_never_reports_ready(self):
        provider = self.provider()
        engine = Mock()
        engine.configure.side_effect = UnsupportedKeyword(UNSUPPORTED)
        provider.engine_factory = lambda: engine
        with patch("personal_ai_assistant.kws.ensure_model"), patch("personal_ai_assistant.voice_command.detect_voice_components", return_value=Mock(state="ready")), patch("personal_ai_assistant.vad.create_vad", return_value=Mock()):
            provider._run(provider._stop)
        self.assertEqual(provider.runtime_state.state, "unsupported")
        self.assertEqual(provider.runtime_state.message, UNSUPPORTED)

    def test_normal_ui_contains_no_training_controls(self):
        source = (Path(__file__).parents[1] / "personal_ai_assistant/main_window.py").read_text(encoding="utf-8")
        for text in ("訓練喚醒詞", "清除訓練資料", "訓練品質", "best template"):
            self.assertNotIn(text, source)

    def test_text_router_works_when_kws_unavailable(self):
        from personal_ai_assistant.command_router import route_assistant_command
        with patch("personal_ai_assistant.kws.SherpaKeywordEngine", side_effect=ImportError):
            self.assertIsNotNone(route_assistant_command("幫我開 Steam"))


@unittest.skipUnless(os.environ.get("KWS_TEST_MODEL"), "Set KWS_TEST_MODEL for real offline model fixtures")
class RealKwsProviderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["KWS_TEST_MODEL"])
        self.engine = SherpaKeywordEngine(self.root)

    def decode(self, filename):
        with wave.open(str(self.root / "test_wavs" / filename), "rb") as source:
            self.assertEqual(source.getframerate(), 16000)
            pcm = source.readframes(source.getnframes()) + bytes(32000)
        found = []
        for i in range(0, len(pcm), 960):
            result = self.engine.accept(pcm[i:i+960])
            if result:
                found.append(result)
        return found

    def test_chinese_custom_keyword_detected_in_real_audio(self):
        self.engine.configure(("周望軍",))
        self.assertEqual(self.decode("zh_5.wav"), ["周望軍"])

    def test_real_normalized_audio_wakes_even_when_vad_always_false(self):
        self.engine.configure(("周望軍",))
        provider = StreamingKwsWakeWordProvider()
        provider.engine = self.engine
        vad = Mock(is_speech=Mock(return_value=False))
        with wave.open(str(self.root / "test_wavs" / "zh_5.wav"), "rb") as source:
            pcm = source.readframes(source.getnframes()) + bytes(32000)
        with patch.object(self.engine.spotter, 'reset_stream', wraps=self.engine.spotter.reset_stream) as reset:
            for i in range(0, len(pcm), 960):
                raw = pcm[i:i + 960]
                provider.process_microphone_frame(raw, vad)
                self.assertFalse(provider.sensitivity.speech)
            self.assertTrue(provider.poll())
            self.assertEqual(provider.last_keyword, "周望軍")
            reset.assert_called_once()

    def test_unrelated_chinese_and_english_do_not_wake(self):
        self.engine.configure(("周望軍",))
        self.assertEqual(self.decode("zh_4.wav"), [])
        self.engine.reset()
        self.assertEqual(self.decode("en_0.wav"), [])

    def test_english_custom_keyword_detected(self):
        self.engine.configure(("LIGHT UP",))
        self.assertIn("LIGHT_UP", self.decode("en_0.wav"))

    def test_traditional_english_and_mixed_names_compile(self):
        compiled = self.engine.compile(("賈維斯", "莉莉絲", "Hey賈維斯", "Jarvis"))
        self.assertEqual(len(compiled), 4)
        self.assertTrue(compiled[0].startswith("j iǎ w éi s ī"))

    def test_unsupported_name_is_explicit(self):
        for name in ("🤖", "xqzvblorp", "賈維斯/", "123", ""):
            with self.assertRaisesRegex(UnsupportedKeyword, "此名稱無法建立"):
                self.engine.compile((name,))

    def test_renaming_replaces_compiled_keywords(self):
        self.engine.configure(("周望軍",))
        self.engine.configure(("莉莉絲",))
        self.assertEqual(self.decode("zh_5.wav"), [])
        self.assertNotIn("周望軍", "".join(self.engine.compiled))


if __name__ == "__main__":
    unittest.main()
