import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

from personal_ai_assistant.voice_command import (
    BaselineAcousticKeywordMatcher, WakeTemplateStore, WakeSettings,
    acoustic_features_from_pcm, enrollment_sample_from_pcm, enrollment_quality,
    calibrated_baseline_threshold, sensitivity_threshold, sequence_similarity,
    wake_segment_from_pcm, SoundDevicePrefixWakeWordProvider,
)


def phrase(gain=1., speed=1., rates=(310, 530, 410), sample_rate=16000):
    samples = [0] * int(.25 * sample_rate)
    for frequency in rates:
        count = int(.19 * sample_rate / speed)
        for index in range(count):
            envelope = min(1., index / 180, (count - index) / 180)
            value = envelope * gain * (6000 * math.sin(2 * math.pi * frequency * index / sample_rate)
                                      + 1800 * math.sin(4 * math.pi * frequency * index / sample_rate))
            samples.append(int(value))
    samples.extend([0] * int(.25 * sample_rate))
    return b''.join(s.to_bytes(2, 'little', signed=True) for s in samples)


class WakeRegressionTests(unittest.TestCase):
    def test_train_and_match_pass_identical_features(self):
        raw = phrase()
        enrolled = enrollment_sample_from_pcm(raw)
        self.assertTrue(enrolled.accepted)
        self.assertEqual(enrolled.features, acoustic_features_from_pcm(wake_segment_from_pcm(raw).pcm))

    def test_vad_padding_is_150ms_each_side(self):
        pcm = bytes(480 * 2 * 20)
        flags = [False] * 7 + [True] * 4 + [False] * 9
        with patch('personal_ai_assistant.voice_command._voice_frame_flags', return_value=flags):
            segment = wake_segment_from_pcm(pcm)
        self.assertAlmostEqual(segment.duration_seconds, (4 + 5 + 5) * .03)
        self.assertTrue(segment.complete)

    def test_mel_dtw_speed_and_gain(self):
        baseline = acoustic_features_from_pcm(phrase())
        self.assertGreater(sequence_similarity(baseline, acoustic_features_from_pcm(phrase(gain=.35))), .96)
        self.assertGreater(sequence_similarity(baseline, acoustic_features_from_pcm(phrase(speed=1.12))), .85)

    def test_four_good_only_fifth_outlier_then_replace(self):
        samples = [acoustic_features_from_pcm(phrase(speed=s)) for s in (1., .98, 1.02, 1.01)]
        samples.append(acoustic_features_from_pcm(phrase(rates=(1000, 1300, 1700))))
        result = enrollment_quality(samples)
        self.assertEqual(result.get('outliers'), [5])
        saved = samples[:4]
        samples[4] = acoustic_features_from_pcm(phrase(speed=.99))
        self.assertTrue(enrollment_quality(samples)['ready'])
        self.assertEqual(saved, samples[:4])

    def test_intra_class_distance_alone_does_not_reject(self):
        # No isolated outlier; independent validation decides readiness.
        samples = [((v,) * 24,) * 10 for v in (0., .1, .2, .3, .4)]
        self.assertTrue(enrollment_quality(samples)['ready'])

    def test_missing_validation_never_saves_ready_template(self):
        with tempfile.TemporaryDirectory() as root:
            store = WakeTemplateStore(Path(root))
            matcher = BaselineAcousticKeywordMatcher(store)
            samples = [acoustic_features_from_pcm(phrase(speed=s)) for s in (1., .98, 1.02, 1.01, .99)]
            self.assertFalse(matcher.enroll_features('賈維斯', samples).success)
            result = matcher.enroll_features('賈維斯', samples, acoustic_features_from_pcm(phrase(speed=1.03)))
            self.assertTrue(result.success)
            self.assertEqual(len(store.load(result.template_id)['samples']), 5)
            self.assertTrue(store.status(WakeSettings('賈維斯', template_id=result.template_id)).available)

    def test_threshold_changes_with_distribution_and_sensitivity(self):
        same = [((0.,) * 24,) * 8] * 5
        varied = [((value,) * 24,) * 8 for value in (0., .015, .03, .045, .06)]
        self.assertGreater(calibrated_baseline_threshold(same), calibrated_baseline_threshold(varied))
        template = {'baseline_threshold': calibrated_baseline_threshold(varied)}
        self.assertGreater(sensitivity_threshold('low', template), sensitivity_threshold('standard', template))
        self.assertGreater(sensitivity_threshold('standard', template), sensitivity_threshold('high', template))

    def test_old_feature_schema_requires_retraining(self):
        with tempfile.TemporaryDirectory() as root:
            store = WakeTemplateStore(Path(root)); store.root.mkdir()
            store.template_path('old').write_text(json.dumps({'schema': 1, 'samples': [1], 'self_validation_passed': True}))
            self.assertFalse(store.status(WakeSettings('賈維斯', template_id='old')).available)

    def test_sample_rate_frame_duration(self):
        with patch('personal_ai_assistant.voice_command._voice_frame_flags', side_effect=lambda frames, *args: [True] * len(frames)):
            segment = wake_segment_from_pcm(bytes(48000 * 2), sample_rate=48000)
        self.assertAlmostEqual(segment.duration_seconds, .99)

    def test_legacy_training_ui_is_no_longer_available(self):
        from personal_ai_assistant.main_window import MainWindow
        self.assertFalse(hasattr(MainWindow, "train_voice_wake_word_now"))
        self.assertFalse(hasattr(MainWindow, "clear_voice_wake_training_now"))

    def test_test_mode_matches_raw_audio_without_double_vad(self):
        from personal_ai_assistant.voice_command import MicrophoneTestResult, AcousticMatchResult
        raw = phrase()
        matcher = Mock()
        matcher.template_status.return_value = SimpleNamespace(available=True)
        matcher.match.return_value = AcousticMatchResult(True, .95, '高', .9, friendly_score=95,
            best_template_index=1, template_count=5, segment_seconds=.87, crossed_threshold=True)
        provider = SoundDevicePrefixWakeWordProvider(matcher=matcher, settings=WakeSettings('賈維斯'))
        with patch('personal_ai_assistant.voice_command.record_microphone_pcm', return_value=raw), \
             patch('personal_ai_assistant.voice_command.analyze_microphone_pcm', return_value=MicrophoneTestResult('voice', '', True, True)), \
             patch.object(provider, '_debug_transcribe', return_value='無關字'):
            result = provider.test_wake_word()
        matcher.match.assert_called_once_with(raw, provider.settings, 16000)
        self.assertTrue(result.matched)
        for text in ('95/100', '1/5', '0.87', 'threshold', '僅供參考'):
            self.assertIn(text, result.message)


if __name__ == '__main__':
    unittest.main()
