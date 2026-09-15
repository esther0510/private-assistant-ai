from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai_assistant.audio_devices import SoundDeviceAudioInputProvider, sounddevice_index_from_identifier
from personal_ai_assistant.voice_command import (
    PrefixWakeWordProvider,
    VoiceAssistantPipeline,
    WakeSettings,
    detect_voice_components,
    strip_wake_phrase,
    wake_aliases_for_settings,
    wake_phrase_matches,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice wake diagnostic without saving audio.")
    parser.add_argument("--assistant-name", default="莉莉絲")
    parser.add_argument("--microphone-id", default="")
    args = parser.parse_args()

    settings = WakeSettings(args.assistant_name, microphone_id=args.microphone_id)
    aliases = wake_aliases_for_settings(settings)
    components = detect_voice_components()
    devices = SoundDeviceAudioInputProvider().list_input_devices()

    print(f"components: {components.state} - {components.message}")
    print(f"selected_microphone_id: {args.microphone_id or 'system-default'}")
    print(f"selected_sounddevice_index: {sounddevice_index_from_identifier(args.microphone_id)}")
    print(f"input_devices: {len(devices)}")
    for device in devices:
        print(f"- {device.display_name} [{device.identifier}]")

    import time
    pipeline = VoiceAssistantPipeline(settings)
    pipeline.start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = pipeline.readiness()
        if state.state not in {"verifying", "loading_models", "opening_microphone"}:
            break
        time.sleep(0.1)
    state = pipeline.readiness()
    print(f"KWS / Mic / Pipeline: {state.state} - {state.message}")
    print("STT model: lazy-loaded after wake; raw audio is never saved")
    pipeline.stop()
    return 0 if state.state == "ready" else 1



if __name__ == "__main__":
    raise SystemExit(main())
