import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from personal_ai_assistant.asr_guard import assess_segments, prompt_echo, speech_presence
from personal_ai_assistant.voice_command import LocalWhisperTranscriber, VoiceAssistantPipeline, WakeSettings, WakeRuntimeState
from personal_ai_assistant.post_wake import SpeechEndpoint
from personal_ai_assistant.voice_text import route_voice_command
from personal_ai_assistant.name_resolver import NameResolver, AliasStore, Entity
import test_command_onset as onset
from test_command_onset import QUIET, VOICE

class HallucinationTests(unittest.TestCase):
    def test_silence_candidates_never_call_decoder(self):
        stt=LocalWhisperTranscriber(); stt._model=Mock();stt.context_names=('NZXT CAM','pythonw','Steam')
        self.assertEqual(stt.transcribe_pcm(bytes(32000)), '')
        stt._model.transcribe.assert_not_called()
        self.assertEqual(stt.last_evidence['discard_reason'],'no_speech')

    def test_low_vad_app_list_echo(self):
        self.assertTrue(prompt_echo('NZXT CAM, pythonw, ChatGPT, Steam', (), .1))
        text,evidence=assess_segments([types.SimpleNamespace(text='LINE, Telegram, Steam', avg_logprob=-.1)], ('LINE','Telegram','Steam'), .1)
        self.assertEqual(text,'');self.assertEqual(evidence['discard_reason'],'prompt_echo')

    def test_future_small_bias_exact_echo_even_with_speech(self):
        self.assertTrue(prompt_echo('LINE, Telegram',('LINE','Telegram'),1))
        self.assertFalse(prompt_echo('幫我打開 LINE',('LINE',),1))

    def test_backend_confidence(self):
        for metric,value,reason in [('no_speech_prob',.8,'no_speech'),('avg_logprob',-1.5,'low_confidence'),('compression_ratio',3,'low_confidence')]:
            segment=types.SimpleNamespace(text='打開 LINE', **{metric:value})
            text,evidence=assess_segments([segment])
            self.assertEqual(text,'');self.assertEqual(evidence['discard_reason'],reason)

    def test_noise_is_not_accumulated_into_onset(self):
        vad=Mock();vad.is_speech.side_effect=([True,False]*100)
        ep=SpeechEndpoint(vad)
        for _ in range(200):ep.feed(QUIET)
        self.assertFalse(ep.speech_detected)

    def test_timeout_no_onset_no_stt(self):
        result,decoder,elapsed,_=onset.CommandOnsetTests().capture([QUIET]*300, [], timeout=1)
        self.assertEqual(result,'');decoder.assert_not_called();self.assertGreaterEqual(elapsed,1)

    def test_pre_wake_only_does_not_authorize_command(self):
        result,decoder,_,_=onset.CommandOnsetTests().capture([VOICE]*20+[QUIET]*50, [], initial=20,timeout=1)
        self.assertEqual(result,'');decoder.assert_not_called()

    def test_discard_rearms_without_router_or_processing_text(self):
        for reason in ('no_speech','prompt_echo','low_confidence'):
            wake=Mock();wake.start.return_value=WakeRuntimeState('standby','ready')
            stt=Mock();notice=Mock();runner=Mock()
            pipe=VoiceAssistantPipeline(WakeSettings('米米'),wake,stt,runner,status_callback=notice)
            pipe.start();wake.reset_mock()
            def capture(_):
                pipe.diagnostics.begin({});stt.transcriber.last_evidence={'discard_reason':reason,'speech_detected':False}
                return ''
            with patch.object(pipe,'_transcribe_after_wake',side_effect=capture):
                self.assertEqual(pipe.transcribe_after_wake(),'')
            self.assertFalse(pipe.handle_wake(''))
            wake.start.assert_called_once();runner.assert_not_called()
            self.assertEqual(pipe.state,'standby');self.assertEqual(pipe.diagnostics.snapshot()['discard_reason'],reason)
            self.assertFalse(any('正在處理' in str(c) for c in notice.call_args_list))

    def test_real_command_decoder_and_resolver(self):
        with tempfile.TemporaryDirectory() as directory:
            names=NameResolver(AliasStore(Path(directory)/'aliases.json'))
            game=Entity('game:553850','Helldivers 2','game','steam://rungameid/553850')
            for phrase in ('幫我打開 LINE','幫我打開 hell device'):
                stt=LocalWhisperTranscriber();stt._model=Mock()
                stt._model.transcribe.return_value=([types.SimpleNamespace(text=phrase,no_speech_prob=.01,avg_logprob=-.1,compression_ratio=1)],None)
                with patch('personal_ai_assistant.vad.create_vad',return_value=Mock(is_speech=Mock(return_value=True))):
                    text=stt.transcribe_pcm(VOICE*10)
                routed=route_voice_command(text)
                self.assertEqual(routed.intent,'activate_or_launch_app')
                self.assertNotIn('initial_prompt',stt._model.transcribe.call_args.kwargs)
                self.assertNotIn('hotwords',stt._model.transcribe.call_args.kwargs)
                if 'device' in phrase:self.assertEqual(names.resolve(routed.target,[game]).best,game)

    def test_capture_does_not_fetch_inventory(self):
        pipe=VoiceAssistantPipeline(WakeSettings('米米'),Mock(),Mock())
        pipe.context_provider=Mock(return_value=tuple(str(i) for i in range(500)))
        pipe.prepare_capture();pipe.context_provider.assert_not_called()
        self.assertEqual(pipe.stt_provider.transcriber.context_names,())
