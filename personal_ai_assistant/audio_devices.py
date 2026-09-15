from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from typing import Any, Protocol


SYSTEM_DEFAULT_MICROPHONE_ID = ""
SYSTEM_DEFAULT_MICROPHONE_LABEL = "系統預設麥克風"
NO_MICROPHONE_LABEL = "未偵測到可用麥克風"

_OUTPUT_HINTS = (
    "loopback",
    "monitor",
    "output",
    "render",
    "speaker",
    "speakers",
    "headphone",
    "headphones",
    "stereo mix",
    "what u hear",
    "youtube",
    "discord output",
    "game output",
)


@dataclass(frozen=True)
class AudioInputDevice:
    identifier: str
    display_name: str
    backend: str = "sounddevice"
    host_api: str = ""
    index: int | None = None
    max_input_channels: int = 0


class AudioInputDeviceProvider(Protocol):
    def list_input_devices(self) -> tuple[AudioInputDevice, ...]:
        ...


def looks_like_physical_microphone(name: str) -> bool:
    normalized = " ".join((name or "").casefold().split())
    if not normalized:
        return False
    return not any(hint in normalized for hint in _OUTPUT_HINTS)


def build_sounddevice_identifier(index: int, name: str, host_api: str) -> str:
    canonical = f"{host_api}|{name}".casefold()
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return f"sounddevice:{digest}:{index}"


def recoverable_sounddevice_identifier(identifier: str, devices: tuple[AudioInputDevice, ...]) -> str:
    if not identifier:
        return SYSTEM_DEFAULT_MICROPHONE_ID
    if any(device.identifier == identifier for device in devices):
        return identifier
    parts = identifier.split(":")
    if len(parts) < 3 or parts[0] != "sounddevice":
        return SYSTEM_DEFAULT_MICROPHONE_ID
    digest = parts[1]
    for device in devices:
        if device.identifier.split(":")[1:2] == [digest]:
            return device.identifier
    return SYSTEM_DEFAULT_MICROPHONE_ID


def sounddevice_index_from_identifier(identifier: str) -> int | None:
    if not identifier:
        return None
    parts = identifier.split(":")
    if len(parts) < 3 or parts[0] != "sounddevice":
        return None
    try:
        return int(parts[-1])
    except ValueError:
        return None


class SoundDeviceAudioInputProvider:
    """Enumerates microphone-capable input devices through sounddevice when available."""

    def list_input_devices(self) -> tuple[AudioInputDevice, ...]:
        try:
            sounddevice = importlib.import_module("sounddevice")
            raw_devices = sounddevice.query_devices()
            raw_hostapis = sounddevice.query_hostapis()
        except Exception:
            return ()
        hostapis = self._hostapi_names(raw_hostapis)
        devices: list[AudioInputDevice] = []
        for index, raw in enumerate(raw_devices or ()):
            parsed = self._parse_device(index, raw, hostapis)
            if parsed is not None:
                devices.append(parsed)
        return tuple(devices)

    def _hostapi_names(self, raw_hostapis: Any) -> dict[int, str]:
        names: dict[int, str] = {}
        for index, raw in enumerate(raw_hostapis or ()):
            if isinstance(raw, dict):
                names[index] = str(raw.get("name") or f"hostapi-{index}")
            else:
                names[index] = str(getattr(raw, "name", "") or f"hostapi-{index}")
        return names

    def _parse_device(
        self,
        index: int,
        raw: Any,
        hostapis: dict[int, str],
    ) -> AudioInputDevice | None:
        if isinstance(raw, dict):
            name = str(raw.get("name") or "").strip()
            hostapi_index = int(raw.get("hostapi") or 0)
            max_inputs = int(raw.get("max_input_channels") or 0)
        else:
            name = str(getattr(raw, "name", "") or "").strip()
            hostapi_index = int(getattr(raw, "hostapi", 0) or 0)
            max_inputs = int(getattr(raw, "max_input_channels", 0) or 0)
        if max_inputs <= 0 or not looks_like_physical_microphone(name):
            return None
        host_api = hostapis.get(hostapi_index, f"hostapi-{hostapi_index}")
        return AudioInputDevice(
            identifier=build_sounddevice_identifier(index, name, host_api),
            display_name=name,
            host_api=host_api,
            index=index,
            max_input_channels=max_inputs,
        )
