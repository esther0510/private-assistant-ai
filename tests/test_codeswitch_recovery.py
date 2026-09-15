import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from personal_ai_assistant.window_activation import ActivateOrLaunchService, AppWindow, WindowsWindowController, rank_matching_window
from personal_ai_assistant.app_resolver import ResolveCandidate, ResolveResult
from personal_ai_assistant.name_resolver import AliasStore, Entity, NameResolver
from personal_ai_assistant.voice_command import VoiceAssistantPipeline, WakeSettings, WakeRuntimeState, LocalWhisperTranscriber
from personal_ai_assistant.voice_text import route_voice_command

class VerifiedLaunchTests(unittest.TestCase):
    def service(self, name='LINE'):
        controller=Mock()
        controller.list_windows.return_value=()
        controller.verify.return_value=False
        resolver=Mock()
        resolver.resolve_app.return_value=ResolveResult(name,(ResolveCandidate(name,name+'.exe','test'),))
        service=ActivateOrLaunchService(resolver, controller, Mock(return_value=True))
        return service,controller

    def test_line_spawn_without_main_window_fails(self):
        service, controller=self.service()
        with patch.object(service,'_client_running',return_value=False),patch('personal_ai_assistant.window_activation.time.sleep'):
            self.assertFalse(service.activate_or_launch('LINE').ok)

    def test_line_hidden_requires_actual_verification(self):
        for verified in (False,True):
            service,controller=self.service()
            controller.list_windows.return_value=(AppWindow(7,'LINE.exe','LINE',True,0,True),)
            controller.verify.return_value=verified
            with patch.object(service,'_wait_window',return_value=False):
                result=service.activate_or_launch('LINE')
            self.assertEqual(result.ok,verified)
            self.assertEqual(result.message,'已切換到 LINE' if verified else '找到 LINE，但主視窗沒有成功喚回')

    def test_telegram_hidden_restore_verified(self):
        service,controller=self.service('Telegram')
        controller.list_windows.return_value=(AppWindow(7,'Telegram.exe','chat',True,0,True),)
        controller.verify.return_value=True
        self.assertTrue(service.activate_or_launch('Telegram').ok)
        service.launcher.assert_not_called()

    def test_fallback_launch_waits_for_new_verified_window(self):
        service,controller=self.service()
        controller.list_windows.side_effect=[(),(),(),(AppWindow(7,'LINE.exe','LINE'),)]
        controller.verify.return_value=True
        with patch.object(service,'_client_running',return_value=True):
            self.assertTrue(service.activate_or_launch('LINE').ok)
        service.launcher.assert_called_once()

    def test_helper_and_tray_handles_excluded(self):
        for window,query in [(AppWindow(1,'steamwebhelper.exe','Steam'),'Steam'),
                             (AppWindow(2,'LINE.exe','LINE',main=False),'LINE'),
                             (AppWindow(3,'Update.exe','Telegram'),'Telegram')]:
            self.assertIsNone(rank_matching_window((window,),query))

    def test_existing_controller_success_without_verifier_is_not_success(self):
        service,controller=self.service()
        controller.list_windows.return_value=(AppWindow(1,'LINE.exe','LINE'),)
        controller.verify.return_value=Mock()
        with patch.object(service,'_wait_window',return_value=False):
            self.assertFalse(service.activate_or_launch('LINE').ok)

class RecoveryTests(unittest.TestCase):
    def pipeline(self):
        wake=Mock();wake.start.return_value=WakeRuntimeState('standby','ready')
        pipe=VoiceAssistantPipeline(WakeSettings('賈維斯'),wake_provider=wake,stt_provider=Mock(),intent_runner=Mock(return_value=False))
        pipe.start();wake.reset_mock()
        return pipe,wake

    def test_unknown_and_action_failure_rearm(self):
        for phrase in ('','無法理解','打開 LINE'):
            pipe,wake=self.pipeline()
            self.assertFalse(pipe.handle_wake(phrase))
            wake.stop.assert_called_once();wake.start.assert_called_once()
            self.assertEqual(pipe.state,'standby');self.assertFalse(pipe._handling_wake)

    def test_stt_error_timeout_and_resolver_exception_rearm(self):
        for error in (RuntimeError('stt'),TimeoutError('timeout')):
            pipe,wake=self.pipeline()
            with patch.object(pipe,'_transcribe_after_wake',side_effect=error):
                with self.assertRaises(type(error)):pipe.transcribe_after_wake()
            wake.start.assert_called_once();self.assertEqual(pipe.state,'standby')
        pipe,wake=self.pipeline();pipe.intent_runner.side_effect=ValueError('resolver')
        with self.assertRaises(ValueError):pipe.handle_wake('打開 LINE')
        wake.start.assert_called_once();self.assertFalse(pipe._handling_wake)

    def test_cancel_rearms_and_retains_warm_stt(self):
        pipe,wake=self.pipeline();transcriber=pipe.stt_provider.transcriber
        pipe.transition('listening');pipe.cancel()
        wake.start.assert_called_once();self.assertIs(pipe.stt_provider.transcriber,transcriber)

    def test_watchdog_recovers_processing_and_listening(self):
        for state in ('processing','listening'):
            pipe,wake=self.pipeline();pipe.transition(state)
            self.assertTrue(pipe.watchdog(pipe.last_activity+16))
            self.assertEqual(pipe.state,'standby');wake.start.assert_called_once()
            self.assertFalse(pipe.watchdog(pipe.last_activity+16))

    def test_rearm_failure_explicit_status(self):
        pipe,wake=self.pipeline();wake.start.side_effect=RuntimeError('mic unavailable')
        pipe.handle_wake('')
        self.assertFalse(pipe.active)
        self.assertEqual(pipe.readiness().message,'語音喚醒未恢復，請重試')

    def test_diagnostic_failure_cannot_skip_cleanup(self):
        pipe,wake=self.pipeline();pipe.diagnostics.log=Mock(side_effect=ValueError('log'))
        with self.assertRaises(ValueError):pipe.handle_wake('')
        wake.start.assert_called_once()

class CodeSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=AliasStore(Path(self.temp.name)/'aliases.json');self.names=NameResolver(self.store)
        self.game=Entity('game:steam:553850','HELLDIVERS™ 2','game','steam://rungameid/553850','steam')
        self.apps=[Entity('telegram','Telegram','app','Telegram.exe'),Entity('opera','Opera','app','Opera.exe')]

    def test_hell_device_uses_installed_candidate(self):
        for spoken in ('HELL DEVICE','helldiver','hell divers'):
            self.assertEqual(self.names.resolve(spoken,[self.game,*self.apps]).best,self.game)
        self.assertNotEqual(self.names.resolve('HELL DEVICE',self.apps).best,self.game)

    def test_phonetic_app_variants(self):
        for spoken,target in [('tele gram','telegram'),('特勒格拉姆','telegram'),('歐佩拉','opera'),('op啦','opera')]:
            self.assertEqual(self.names.resolve(spoken,self.apps).best.id,target)

    def test_ambiguity_and_confirmed_alias_persistence(self):
        entities=[*self.apps,Entity('telegram-beta','Telegram Beta','app','beta.exe')]
        result=self.names.resolve('telegr',entities)
        self.assertTrue(result.needs_confirmation);self.assertGreaterEqual(len(result.candidates),2)
        self.assertFalse(self.store.data['aliases'])
        self.store.confirm('telegr',self.apps[0])
        restored=NameResolver(AliasStore(self.store.path))
        self.assertEqual(restored.resolve('telegr',entities).best,self.apps[0])

    def test_action_independent_of_unknown_target(self):
        for text in ('幫我打開 HELL DEVICE','我要開 tele gram','麻煩切到 歐佩拉','我想開 ZX_UNKNOWN'):
            self.assertEqual(route_voice_command(text).intent,'activate_or_launch_app')

    def test_multilingual_context_supported_backend(self):
        calls=[]
        class Model:
            def transcribe(self,audio,language='zh',beam_size=1,vad_filter=False,initial_prompt=None,hotwords=None):
                calls.append((language,initial_prompt,hotwords))
                return [types.SimpleNamespace(text='打開 HELL DEVICE')],None
        stt=LocalWhisperTranscriber();stt._model=Model();stt.context_names=('LINE','Helldivers 2','Telegram')
        with patch('personal_ai_assistant.asr_guard.speech_presence', return_value={'speech_detected': True}):
            self.assertEqual(stt.transcribe_pcm(bytes(960)),'打開 HELL DEVICE')
        self.assertEqual(calls,[(None,None,None)])

    def test_unsupported_context_backend_still_transcribes(self):
        class Model:
            def transcribe(self,audio,language,beam_size,vad_filter):
                assert language is None
                return [types.SimpleNamespace(text='LINE')],None
        stt=LocalWhisperTranscriber();stt._model=Model();stt.context_names=('LINE',)
        with patch('personal_ai_assistant.asr_guard.speech_presence', return_value={'speech_detected': True}):
            self.assertEqual(stt.transcribe_pcm(bytes(960)),'LINE')

    def test_dynamic_local_prompt_bounded_cached(self):
        from personal_ai_assistant.entity_navigation import EntityNavigator
        from personal_ai_assistant.app_resolver import AppResolver
        with patch.object(AppResolver,'refresh_background'):
            windows=Mock();windows.list_windows.return_value=(AppWindow(7,'LINE.exe','LINE'),)
            games=types.SimpleNamespace(_entities=[self.game])
            nav=EntityNavigator(store=self.store,windows=windows,games=games)
        nav.apps._index=[ResolveCandidate('Telegram','t.exe','test')]
        names=nav.context_names()
        self.assertIn('Telegram',names);self.assertIn(self.game.name,names);self.assertIn('LINE',names)
        self.assertIs(nav.context_names(),names)
        nav.apps._index.extend(ResolveCandidate('App'+str(i),'x','test') for i in range(100))
        self.assertLessEqual(len(nav.context_names()),32)


class WindowEvidenceTests(unittest.TestCase):
    def enumerate(self, process, title, cls, iconic=False, parents=()):
        gui=Mock()
        gui.EnumWindows.side_effect=lambda callback,arg: callback(77,arg)
        gui.GetWindowText.return_value=title;gui.GetClassName.return_value=cls
        gui.GetWindow.return_value=0;gui.GetWindowLong.return_value=0
        gui.IsWindowVisible.return_value=False;gui.IsIconic.return_value=iconic
        gui.GetWindowRect.return_value=(0,0,159,27) if iconic else (0,0,800,600)
        ps=Mock();ps.Process.return_value.name.return_value=process
        ps.Process.return_value.parents.return_value=[types.SimpleNamespace(name=Mock(return_value=n)) for n in parents]
        wp=Mock();wp.GetWindowThreadProcessId.return_value=(1,2)
        with patch.dict('sys.modules',{'win32gui':gui,'psutil':ps,'win32process':wp}):
            return WindowsWindowController().list_windows()

    def test_large_line_tray_message_handle_is_not_main(self):
        self.assertEqual(self.enumerate('LINE.exe','LINE','Qt663TrayIconMessageWindowClass'),())

    def test_minimized_opera_small_rect_can_be_restored(self):
        self.assertEqual(len(self.enumerate('opera.exe','Opera','Chrome_WidgetWin_1',True)),1)

    def test_steam_helper_needs_main_ui_and_steam_ancestor(self):
        self.assertEqual(self.enumerate('steamwebhelper.exe','Steam','SDL_app'),())
        windows=self.enumerate('steamwebhelper.exe','Steam','SDL_app',parents=('steam.exe',))
        self.assertEqual(len(windows),1)
        self.assertEqual(rank_matching_window(windows,'Steam'),windows[0])

    def test_visible_but_unfocused_window_does_not_verify(self):
        gui=Mock();gui.IsWindow.return_value=True;gui.IsWindowVisible.return_value=True
        gui.IsIconic.return_value=False;gui.GetWindow.return_value=0
        gui.GetWindowLong.return_value=0;gui.GetWindowRect.return_value=(0,0,600,400)
        gui.GetForegroundWindow.return_value=999
        wp=Mock();wp.GetWindowThreadProcessId.return_value=(1,2)
        ps=Mock();ps.Process.return_value.name.return_value='LINE.exe'
        with patch.dict('sys.modules',{'win32gui':gui,'win32process':wp,'psutil':ps}), patch('ctypes.windll.user32.GetGUIThreadInfo',return_value=0):
            self.assertFalse(WindowsWindowController().verify(AppWindow(77,'LINE.exe','LINE')))

    def test_running_steam_dispatches_protocol_without_waiting(self):
        from personal_ai_assistant.game_resolver import SteamProvider
        start=Mock();uri=Mock()
        steam=SteamProvider(Path('.'),running=lambda:True,start=start,open_uri=uri,ready=lambda:False,timeout=0)
        self.assertTrue(steam.launch(Entity('g','g','game','steam://rungameid/1','steam')))
        start.assert_not_called();uri.assert_called_once_with('steam://rungameid/1')


class ReleaseAuditTests(unittest.TestCase):
    def test_release_parent_does_not_bypass_privacy_audit(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'release'/'package';root.mkdir(parents=True)
            (root/'entity_memory.json').write_text('{}',encoding='utf-8')
            result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/privacy_audit.py'),str(root)],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertIn('personal resolver/settings data',result.stdout)

if __name__=='__main__':unittest.main()
