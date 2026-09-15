import json
import math
import tempfile
import threading
import unittest
from array import array
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from personal_ai_assistant.game_resolver import SteamProvider, GameResolver
from personal_ai_assistant.name_resolver import AliasStore, Entity, NameResolver
from personal_ai_assistant.kws import StreamingKwsWakeWordProvider, SherpaKeywordEngine
from personal_ai_assistant.voice_command import VoiceAssistantPipeline, WakeSettings, WakeRuntimeState
from personal_ai_assistant.wake_sensitivity import AdaptiveWakeSensitivity
from personal_ai_assistant.settings import AssistantSettings


class SteamIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.steam = SteamProvider(self.root, open_uri=Mock())
        self.names = NameResolver(AliasStore(self.root/'aliases.json'))

    def manifest(self, appid='553850', library=None, name='Helldivers 2'):
        library = library or self.root
        (library/'steamapps/common'/appid).mkdir(parents=True, exist_ok=True)
        path = library/f'steamapps/appmanifest_{appid}.acf'
        path.write_text(f'"AppState" {{ "appid" "{appid}" "name" "{name}" "StateFlags" "4" "installdir" "{appid}" }}', encoding='utf-8')
        return path

    def resolver(self):
        result = GameResolver(self.names, [self.steam])
        self.addCleanup(result.close)
        return result

    def test_multiple_libraries_build_canonical_records(self):
        self.manifest()
        other = self.root/'second'
        self.manifest('7', other, 'Another Game')
        (self.root/'steamapps/libraryfolders.vdf').write_text('"libraryfolders" { "1" { "path" "'+other.as_posix()+'" } }')
        games = self.resolver().candidates()
        self.assertEqual({g.metadata['appid'] for g in games}, {'553850', '7'})
        self.assertTrue(all(g.metadata['installed'] and g.metadata['launcher']=='Steam' for g in games))

    def test_add_remove_and_unchanged_manifests_not_read(self):
        first = self.manifest()
        games = self.resolver()
        original = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            reads.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read), patch.object(Path, 'rglob', side_effect=AssertionError('full disk scan')):
            games.refresh()
            self.assertEqual(reads, [])
            self.manifest('8')
            games.refresh()
            self.assertEqual(len(games.candidates()), 2)
            self.assertNotIn(first, reads)
            first.unlink()
            games.refresh()
            self.assertEqual([e.metadata['appid'] for e in games.candidates()], ['8'])

    def test_changed_manifest_reparsed(self):
        self.manifest(); games=self.resolver()
        self.manifest(name='Updated Game Name')
        games.refresh()
        self.assertEqual(games.candidates()[0].name, 'Updated Game Name')

    def test_queries_only_read_snapshot(self):
        self.manifest(); games=self.resolver()
        with patch.object(self.steam, 'discover', side_effect=AssertionError('query discovery')):
            for query in ('Helldivers 2', 'HELLDIVERS 2', 'HD2'):
                self.assertEqual(games.resolve(query).best.metadata['appid'], '553850')

    def test_local_display_and_confirmed_chinese_same_id(self):
        self.manifest()
        with patch.object(self.steam, 'local_display_names', return_value=('絕地戰兵 2',)):
            games=self.resolver()
        self.assertEqual(games.resolve('絕地戰兵2').best.metadata['appid'], '553850')
        self.names.store.confirm('我的中文遊戲', games.candidates()[0])
        reloaded=NameResolver(AliasStore(self.names.store.path))
        self.assertEqual(reloaded.resolve('我的中文遊戲', games.candidates()).best.id, 'game:steam:553850')
        self.assertEqual(len(games.candidates()), 1)

    def test_unconfirmed_descriptive_chinese_requires_selection(self):
        self.manifest(); games=self.resolver()
        result=games.resolve('絕地戰兵2')
        self.assertTrue(result.needs_confirmation)
        self.assertEqual(result.candidates[0].entity.metadata['appid'], '553850')
        self.names.store.confirm('絕地戰兵 2', result.candidates[0].entity)
        self.assertEqual(games.resolve('絕地戰兵2').best.id, 'game:steam:553850')

    def test_provider_duplicate_appid_merges_names(self):
        english=Entity('game:steam:1', 'English', 'game', 'steam://rungameid/1', 'steam')
        localized=replace(english, name='中文', aliases=('縮寫',))
        with patch.object(self.steam, 'discover', return_value=[english, localized]):
            games=self.resolver()
        self.assertEqual(len(games.candidates()), 1)
        self.assertEqual(games.resolve('中文').best.id, english.id)
        self.assertEqual(games.resolve('English').best.id, english.id)

    def test_known_appid_forces_protocol_even_if_target_exe(self):
        self.manifest(); games=self.resolver()
        entity=replace(games.candidates()[0], target='bad.exe')
        self.assertTrue(self.steam.launch(entity))
        self.steam.open_uri.assert_called_once_with('steam://rungameid/553850')

    def test_launch_does_not_rescan_inventory(self):
        self.manifest(); games=self.resolver()
        with patch.object(self.steam, 'discover', side_effect=AssertionError('launch rescan')):
            self.assertTrue(games.launch(games.candidates()[0]))

    def test_removed_game_not_launched_from_stale_alias(self):
        path=self.manifest(); games=self.resolver(); entity=games.candidates()[0]
        path.unlink()
        self.assertFalse(games.launch(entity))
        self.steam.open_uri.assert_not_called()


class SensitivityTests(unittest.TestCase):
    def pcm(self, level):
        return array('h', [int(32767*level*math.sin(i*.15)) for i in range(480)]).tobytes()

    def test_auto_default(self):
        self.assertEqual(AssistantSettings().voice_wake_sensitivity, 'auto')
        self.assertEqual(WakeSettings().sensitivity, 'auto')

    def test_auto_bounded_across_noise_and_learning(self):
        model=AdaptiveWakeSensitivity()
        for noise in (0., .001, .01, .03, 1.):
            model.noise_floor=noise
            for level in (.0001, .01, .5):
                model.level=level
                self.assertTrue(.10<=model.kws_threshold<=.12)
                self.assertTrue(.002<=model.vad_threshold<=.08)

    def test_quiet_speech_auto_passes_standard_does_not(self):
        auto, standard=AdaptiveWakeSensitivity(), AdaptiveWakeSensitivity('standard')
        vad=Mock();vad.is_speech.return_value=True
        self.assertTrue(auto.process(self.pcm(.004), vad)[1])
        self.assertFalse(standard.process(self.pcm(.004), vad)[1])

    def test_modes_have_distinct_thresholds_never_bypass_vad(self):
        models=[AdaptiveWakeSensitivity(mode) for mode in ('low','standard','high')]
        self.assertEqual(sorted(m.kws_threshold for m in models), [.10,.11,.12])
        self.assertEqual(len(set(m.vad_threshold for m in models)),3)
        vad=Mock();vad.is_speech.return_value=False
        for model in models:
            self.assertFalse(model.process(self.pcm(.1),vad)[1])

    def test_noise_tracks_and_silence_never_passes(self):
        model=AdaptiveWakeSensitivity();vad=Mock();vad.is_speech.return_value=False
        for _ in range(250):
            self.assertFalse(model.process(self.pcm(.006),vad)[1])
        self.assertGreater(model.noise_floor,.003)
        vad.is_speech.return_value=True
        self.assertFalse(model.process(bytes(960),vad)[1])

    def test_calibration_only_numeric_no_raw_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'stats.json'
            model=AdaptiveWakeSensitivity(path=path)
            vad=Mock();vad.is_speech.return_value=True
            for _ in range(10): model.process(self.pcm(.012),vad)
            self.assertIn('麥克風音量',model.calibration_result())
            data=json.loads(path.read_text())
            self.assertEqual(set(data),{'noise_floor','speech_level','successes'})
            self.assertTrue(all(isinstance(v,(int,float)) for v in data.values()))
            self.assertEqual(list(Path(tmp).iterdir()),[path])
            self.assertAlmostEqual(AdaptiveWakeSensitivity(path=path).speech_level,model.speech_level)

    def test_no_speech_does_not_save_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'stats.json';model=AdaptiveWakeSensitivity(path=path)
            self.assertIn('未更新',model.calibration_result())
            self.assertFalse(path.exists())

    def test_keyword_dynamic_stream_threshold(self):
        engine=SherpaKeywordEngine();engine.spotter=Mock();engine.compiled=('j i @test',)
        engine.set_threshold(.21)
        engine.spotter.create_stream.assert_not_called()
        engine.reset(reason='keyword hit')
        self.assertIn('#0.210 @test',engine.spotter.create_stream.call_args.args[0])

    def test_gain_bounded_and_raw_not_modified(self):
        model=AdaptiveWakeSensitivity();vad=Mock();vad.is_speech.return_value=True
        raw=self.pcm(.004);before=bytes(raw)
        normalized,_=model.process(raw,vad)
        self.assertEqual(raw,before)
        self.assertLessEqual(max(array('h',normalized)),max(array('h',raw))*18)


class ServiceWatchdogTests(unittest.TestCase):
    def pipe(self):
        provider=StreamingKwsWakeWordProvider()
        pipe=VoiceAssistantPipeline(WakeSettings('賈維斯'),wake_provider=provider,stt_provider=Mock())
        provider.start=Mock(return_value=WakeRuntimeState('standby','ready'))
        provider.runtime_state=WakeRuntimeState('standby','ready')
        pipe.start()
        provider.start.reset_mock()
        return pipe,provider

    def test_dead_worker_restarts_once_with_backoff(self):
        pipe,provider=self.pipe()
        self.assertTrue(pipe.watchdog(100))
        self.assertFalse(pipe.watchdog(101))
        provider.start.assert_called_once()

    def test_live_worker_never_duplicates(self):
        pipe,provider=self.pipe();provider._thread=Mock();provider._thread.is_alive.return_value=True
        for i in range(10):self.assertFalse(pipe.watchdog(100+i))
        provider.start.assert_not_called()

    def test_stopped_unwinding_thread_cannot_overlap(self):
        provider=StreamingKwsWakeWordProvider();provider._thread=Mock();provider._thread.is_alive.return_value=True
        provider._stop.set()
        old=provider._thread
        provider.start()
        self.assertIs(provider._thread,old)

    def test_voice_off_or_quit_disarms_watchdog(self):
        pipe,provider=self.pipe();pipe.stop()
        self.assertFalse(pipe.watchdog(100))
        provider.start.assert_not_called()

    def test_privacy_stops_and_blocks_restart(self):
        pipe,provider=self.pipe();pipe.privacy_enabled=lambda:True
        self.assertFalse(pipe.watchdog(100))
        self.assertFalse(pipe.active)
        self.assertTrue(provider._stop.is_set())
        provider.start.assert_not_called()

    def test_command_capture_not_mistaken_for_dead_standby(self):
        pipe,provider=self.pipe();pipe.prepare_capture()
        self.assertFalse(pipe.watchdog(pipe.last_activity+1))
        provider.start.assert_not_called()

    def test_dependency_error_does_not_restart_loop(self):
        pipe,provider=self.pipe();provider.runtime_state=WakeRuntimeState('error_dependency','missing')
        self.assertFalse(pipe.watchdog(100))
        provider.start.assert_not_called()


import os
import wave
@unittest.skipUnless(os.environ.get('KWS_TEST_MODEL'), 'Set KWS_TEST_MODEL for real offline fixtures')
class AdaptiveRealAudioTests(unittest.TestCase):
    def provider(self):
        root=Path(os.environ['KWS_TEST_MODEL'])
        engine=SherpaKeywordEngine(root);engine.configure(('周望軍',),'auto')
        provider=StreamingKwsWakeWordProvider();provider.engine=engine
        from personal_ai_assistant.vad import create_vad
        return root,provider,create_vad()

    def feed(self, provider, vad, pcm):
        for i in range(0,len(pcm)-959,960):
            frame=pcm[i:i+960]
            normalized,speech=provider.sensitivity.process(frame,vad)
            provider.consume(frame,speech,kws_pcm=normalized)

    def test_quiet_real_keyword_at_tenth_amplitude(self):
        root,provider,vad=self.provider()
        with wave.open(str(root/'test_wavs/zh_5.wav'),'rb') as f:
            samples=array('h',f.readframes(f.getnframes()))
        quiet=array('h',(int(x*.1) for x in samples)).tobytes()+bytes(32000)
        self.feed(provider,vad,quiet)
        self.assertEqual(provider.last_keyword,'周望軍')

    def test_real_unrelated_speech_does_not_wake(self):
        root,provider,vad=self.provider()
        with wave.open(str(root/'test_wavs/zh_4.wav'),'rb') as f:
            pcm=f.readframes(f.getnframes())+bytes(32000)
        self.feed(provider,vad,pcm)
        self.assertFalse(provider.last_keyword)

    def test_real_vad_and_kws_pure_noise_do_not_wake(self):
        import random
        _,provider,vad=self.provider()
        rng=random.Random(42)
        noise=array('h',(rng.randint(-500,500) for _ in range(16000*4))).tobytes()
        self.feed(provider,vad,noise)
        self.assertFalse(provider.last_keyword)
