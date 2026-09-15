import threading
import unittest
from array import array
from unittest.mock import Mock

from personal_ai_assistant.kws import StreamingKwsWakeWordProvider
from personal_ai_assistant.wake_sensitivity import AdaptiveWakeSensitivity


class LifecycleGainTests(unittest.TestCase):
    def test_abort_failure_still_closes_device(self):
        provider = StreamingKwsWakeWordProvider()
        mic = Mock()
        mic.abort.side_effect = RuntimeError("device disconnected")
        provider._mic = mic
        provider.stop()
        mic.close.assert_called_once()
        self.assertIsNone(provider._mic)

    def test_restart_waits_for_old_worker_and_discards_old_queue(self):
        provider = StreamingKwsWakeWordProvider()
        entered, release, restarted = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def run(stop):
            calls.append(stop)
            if len(calls) == 1:
                entered.set()
                release.wait(3)
            else:
                restarted.set()
                stop.wait(3)
        provider._run = run
        provider.start()
        self.assertTrue(entered.wait(1))
        old_queue = provider._queue
        try:
            provider.stop()
            self.assertEqual(provider.start().state, "starting")
            old_queue.put(b"stale")
            release.set()
            self.assertTrue(restarted.wait(2))
            self.assertIsNot(provider._queue, old_queue)
            self.assertTrue(provider._queue.empty())
            self.assertEqual(len(calls), 2)
            self.assertIsNot(calls[0], calls[1])
        finally:
            release.set()
            provider.stop()

    def test_sensitivity_is_passthrough_and_vad_sees_same_pcm(self):
        sensitivity = AdaptiveWakeSensitivity()
        pcm = array('h', [110, -110] * 240).tobytes()
        vad = Mock(is_speech=Mock(return_value=True))
        normalized, speech = sensitivity.process(pcm, vad)
        self.assertTrue(speech)
        self.assertIs(normalized, pcm)
        self.assertEqual(sensitivity.gain, 1)
        vad.is_speech.assert_called_once_with(pcm, 16000)

    def test_silence_cannot_force_speech(self):
        normalized, speech = AdaptiveWakeSensitivity().process(bytes(960), Mock(is_speech=Mock(return_value=True)))
        self.assertFalse(speech)
        self.assertEqual(normalized, bytes(960))
