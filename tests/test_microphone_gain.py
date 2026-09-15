import unittest
from array import array
from unittest.mock import Mock, patch
from personal_ai_assistant.microphone_gain import MicrophoneGain, rms, db
from personal_ai_assistant.kws import StreamingKwsWakeWordProvider


def frame(value):
    return array('h', [value, -value]*240).tobytes()


class MicrophoneGainTests(unittest.TestCase):
    def test_quiet_hyperx_gain_and_slow_adjustment(self):
        preamp = MicrophoneGain()
        raw = frame(207)  # approximately -44 dBFS
        for _ in range(40):
            processed = preamp.process(raw)
        self.assertAlmostEqual(db(rms(processed))-db(rms(raw)), 18, delta=.1)
        self.assertAlmostEqual(db(rms(processed)), -26, delta=.2)
        before = preamp.gain_db
        preamp.process(frame(3000))
        self.assertLessEqual(abs(preamp.gain_db-before), .031)

    def test_limiter_including_negative_full_scale(self):
        preamp = MicrophoneGain()
        preamp.process(frame(207))
        out = preamp.process(array('h', [-32768,32767]*240).tobytes())
        self.assertLessEqual(max(abs(x) for x in array('h',out)), preamp.ceiling)

    def test_silence_and_low_noise_do_not_build_gain(self):
        preamp = MicrophoneGain()
        for _ in range(1000):
            self.assertEqual(preamp.process(bytes(960)), bytes(960))
            self.assertEqual(preamp.process(frame(20)), frame(20))
        preamp.process(frame(207))
        for _ in range(100):
            out = preamp.process(frame(20))
        self.assertEqual(out, frame(20))

    def test_off_exact_passthrough_even_full_scale(self):
        raw = array('h', [-32768,32767]*240).tobytes()
        self.assertIs(MicrophoneGain('off').process(raw), raw)

    def test_shared_pcm_reaches_vad_kws_command_and_capture(self):
        provider = StreamingKwsWakeWordProvider()
        provider.engine = Mock(accept=Mock(return_value='Jarvis'))
        vad = Mock(is_speech=Mock(return_value=False))
        raw = frame(207)
        processed = provider.process_microphone_frame(raw, vad)
        self.assertNotEqual(processed, raw)
        vad.is_speech.assert_called_once_with(processed,16000)
        provider.engine.accept.assert_called_once_with(processed)
        self.assertEqual(bytes(provider._command),processed)
        second = provider.process_microphone_frame(raw,vad)
        self.assertEqual(provider._history.snapshot(),processed+second)
        with patch('personal_ai_assistant.post_wake.SpeechEndpoint'):
            captured = provider.capture_command(0)
        self.assertEqual(captured,processed+second)

    def test_off_shared_path(self):
        provider=StreamingKwsWakeWordProvider()
        provider.preamp=MicrophoneGain('off')
        provider.engine=Mock(accept=Mock(return_value=''))
        raw=frame(207)
        self.assertIs(provider.process_microphone_frame(raw,Mock()),raw)
        provider.engine.accept.assert_called_once_with(raw)

    def test_post_wake_transcriber_receives_processed_buffer(self):
        import time
        from personal_ai_assistant.post_wake import capture_post_wake
        provider = StreamingKwsWakeWordProvider()
        provider.engine = Mock(accept=Mock(return_value='Jarvis'))
        vad = Mock(is_speech=Mock(return_value=True))
        expected = b''.join(provider.process_microphone_frame(frame(207), vad) for _ in range(20))
        provider.last_wake_at = time.monotonic()-2
        endpoint = Mock(speech_revision=20, speech_detected=True, ended=True,
                        quiet_ms=750, voiced_ms=300)
        transcribe = Mock(return_value='打開 LINE')
        with patch('personal_ai_assistant.post_wake.SpeechEndpoint', return_value=endpoint):
            result = capture_post_wake(provider, transcribe, ('Jarvis',), timeout_seconds=3)
        self.assertEqual(result,'打開 LINE')
        transcribe.assert_called_once_with(expected)

    def test_standalone_command_uses_preamp_before_endpoint(self):
        from personal_ai_assistant.post_wake import record_command
        raw = frame(207)
        expected = MicrophoneGain().process(raw)
        def stream(**kwargs):
            kwargs['callback'](raw,480,None,None)
            from unittest.mock import MagicMock
            return MagicMock()
        for mode, audio in [('auto',expected),('off',raw)]:
            endpoint=Mock(speech_detected=True)
            with patch('personal_ai_assistant.voice_command._sounddevice_module', return_value=Mock(RawInputStream=stream)), patch('personal_ai_assistant.post_wake.SpeechEndpoint', return_value=endpoint), patch('personal_ai_assistant.audio_devices.sounddevice_index_from_identifier',return_value=None):
                self.assertEqual(record_command('',gain_mode=mode),audio)
            endpoint.feed.assert_called_once_with(audio)
