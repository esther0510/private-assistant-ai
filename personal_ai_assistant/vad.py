"""VAD provider boundary; production uses the maintained WebRTC wheel implementation."""
import importlib

class NativeWebRtcVad:
    """Use the same wheel's native engine if its Python metadata wrapper fails."""
    def __init__(self, mode=1):
        self.module = importlib.import_module("_webrtcvad")
        self.handle = self.module.create()
        self.module.init(self.handle)
        self.module.set_mode(self.handle, mode)

    def is_speech(self, pcm, sample_rate):
        return self.module.process(self.handle, sample_rate, pcm, len(pcm) // 2)

def create_vad(aggressiveness=1, providers=None):
    factories = providers if providers is not None else (lambda: importlib.import_module("webrtcvad").Vad(aggressiveness), lambda: NativeWebRtcVad(aggressiveness))
    errors = []
    for factory in factories:
        try:
            vad = factory()
            vad.is_speech(bytes(960), 16000)
            return vad
        except Exception as exc:
            errors.append(exc)
    raise RuntimeError("VAD 元件載入失敗，請安裝或重試語音元件") from (errors[-1] if errors else None)
