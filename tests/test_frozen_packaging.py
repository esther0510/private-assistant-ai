import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from personal_ai_assistant.startup import startup_command
from personal_ai_assistant.voice_command import install_voice_dependencies, VoiceInstallState

class FrozenPackagingTests(unittest.TestCase):
    def test_startup_uses_exe_without_source_launcher(self):
        with patch('sys.frozen', True, create=True), patch('sys.executable', 'C:/Friend/PrivateAssistantAI.exe'):
            self.assertEqual(startup_command(Path('C:/Friend/_internal')), '"C:/Friend/PrivateAssistantAI.exe" --background')

    def test_frozen_verification_never_invokes_pip(self):
        runner = Mock(side_effect=AssertionError('must not launch installer'))
        with patch('sys.frozen', True, create=True), patch('personal_ai_assistant.voice_command.detect_voice_components', return_value=VoiceInstallState('ready', 'ready')):
            self.assertEqual(install_voice_dependencies(Path('.'), runner=runner).state, 'verified')
        runner.assert_not_called()

    def test_incomplete_bundle_has_reextract_message(self):
        with patch('sys.frozen', True, create=True), patch('personal_ai_assistant.voice_command.detect_voice_components', return_value=VoiceInstallState('missing_components', 'missing')):
            state = install_voice_dependencies(Path('.'))
            self.assertEqual(state.state, 'error_dependency')
            self.assertIn('重新解壓', state.message)
