import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from personal_ai_assistant.kws import SherpaKeywordEngine, StreamingKwsWakeWordProvider
from personal_ai_assistant.wake_sensitivity import AdaptiveWakeSensitivity


class ContinuousKwsTests(unittest.TestCase):
    def test_vad_transitions_and_long_silence_feed_exactly_once(self):
        provider = StreamingKwsWakeWordProvider()
        provider.engine = Mock(accept=Mock(return_value=''))
        expected = []
        for i, speech in enumerate([False] * 40 + [True] * 10 + [False] * 80 + [True] * 10):
            normalized = bytes([i]) * 960
            provider.consume(bytes(960), speech, kws_pcm=normalized)
            expected.append(normalized)
        self.assertEqual([c.args[0] for c in provider.engine.accept.call_args_list], expected)
        provider.engine.reset.assert_not_called()
        provider.engine.set_threshold.assert_not_called()
        self.assertFalse(provider.poll())

    def test_false_vad_can_wake_and_handoff_processed_command(self):
        provider = StreamingKwsWakeWordProvider()
        provider.engine = Mock(accept=Mock(return_value='Jarvis'))
        provider.consume(b'a' * 960, False, kws_pcm=b'b' * 960)
        self.assertTrue(provider.poll())
        provider.consume(b'c' * 960, False)
        provider.engine.accept.assert_called_once_with(b'b' * 960)
        self.assertEqual(provider._command, b'b' * 960 + b'c' * 960)

    def test_rearm_and_calibration_do_not_reset_decoder(self):
        provider = StreamingKwsWakeWordProvider()
        provider.engine = Mock()
        provider._thread = Mock(is_alive=Mock(return_value=True))
        provider._mic = Mock()
        provider.rearm()
        provider.calibrate(0)
        provider.engine.reset.assert_not_called()

    def engine(self):
        engine = SherpaKeywordEngine()
        engine.compiled = ('j i @Jarvis',)
        engine.spotter = Mock()
        engine.stream = Mock()
        return engine

    def test_no_result_never_resets_then_hit_resets_once(self):
        engine = self.engine()
        stream = engine.stream
        for result in ('', 'Jarvis'):
            engine.spotter.is_ready.side_effect = [True, False]
            engine.spotter.get_result.return_value = result
            self.assertEqual(engine.accept(bytes(960)), result)
            if not result:
                engine.spotter.reset_stream.assert_not_called()
        engine.spotter.reset_stream.assert_called_once_with(stream)
        engine.spotter.create_stream.assert_not_called()
        stream.accept_waveform.assert_called()
        self.assertEqual(stream.accept_waveform.call_args.args[0], 16000)

    def test_threshold_updates_deferred_until_keyword_hit(self):
        engine = self.engine()
        original = engine.stream
        for _ in range(100):
            engine.set_threshold(.10)
        self.assertIs(engine.stream, original)
        engine.spotter.create_stream.assert_not_called()
        engine.spotter.is_ready.side_effect = [True, False]
        engine.spotter.get_result.return_value = 'Jarvis'
        engine.accept(bytes(960))
        engine.spotter.create_stream.assert_called_once_with('j i #0.100 @Jarvis')
        self.assertEqual(engine.threshold, .10)
        engine.set_threshold(.10)
        self.assertIsNone(engine._pending_threshold)

    def test_constructor_parameters_match_stable_sensitivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            for mode, threshold in [('low', .12), ('standard', .11), ('high', .10), ('auto', .11)]:
                engine = SherpaKeywordEngine(tmp)
                def compile_aliases(aliases):
                    engine.compiled = ('j i @Jarvis',)
                    return engine.compiled
                module = Mock()
                with patch.dict(sys.modules, {'sherpa_onnx': module}), patch.object(engine, 'compile', side_effect=compile_aliases):
                    engine.configure(('Jarvis',), mode)
                kwargs = module.KeywordSpotter.call_args.kwargs
                self.assertEqual(kwargs['keywords_score'], 3.0)
                self.assertEqual(kwargs['keywords_threshold'], threshold)
                self.assertEqual(kwargs['num_trailing_blanks'], 1)
                sensitivity = AdaptiveWakeSensitivity(mode)
                for level in (.001, .5):
                    sensitivity.level = level
                    sensitivity.noise_floor = level
                    self.assertEqual(sensitivity.kws_threshold, threshold)


if __name__ == '__main__':
    unittest.main()
