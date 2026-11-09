"""Records microphone audio to a 16 kHz mono WAV, the format Whisper wants."""

import logging
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

# PortAudio lists every microphone once per host API. We show one API's devices
# rather than de-duplicating, because the same device is named differently under
# each (MME truncates to 31 chars). WASAPI and WDM-KS are last resorts: neither
# resamples, so a 48 kHz-only microphone fails to open at 16 kHz.
_HOST_APIS_BEST_FIRST = ("directsound", "mme", "wasapi", "wdm-ks")

# Each host API's "let Windows choose" entry, which is what "System default"
# already offers.
_ROUTER_DEVICE_PREFIXES = ("microsoft sound mapper", "primary sound capture driver")

# WDM-KS exposes raw driver pins, e.g.
# "Input (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free%0 ;(TOZO O2))".
_RAW_DRIVER_PIN_MARKERS = ("@system32", "\\drivers\\", ";%1")


def _is_selectable(device: dict) -> bool:
    name = (device.get("name") or "").strip().lower()
    if not name or device.get("max_input_channels", 0) <= 0:
        return False
    if any(name.startswith(prefix) for prefix in _ROUTER_DEVICE_PREFIXES):
        return False
    return not any(marker in name for marker in _RAW_DRIVER_PIN_MARKERS)


def _host_api_rank(host_apis: list, index: int) -> int:
    if not 0 <= index < len(host_apis):
        return len(_HOST_APIS_BEST_FIRST)
    name = (host_apis[index].get("name") or "").lower().replace(" ", "")
    for rank, wanted in enumerate(_HOST_APIS_BEST_FIRST):
        if wanted in name:
            return rank
    return len(_HOST_APIS_BEST_FIRST)


def _best_host_api(devices: list, host_apis: list) -> int:
    """The host API index whose devices we present, or -1 if none has any."""
    candidates = {d.get("hostapi", -1) for d in devices if _is_selectable(d)}
    if not candidates:
        return -1
    return min(candidates, key=lambda i: (_host_api_rank(host_apis, i), i))


def list_input_devices() -> list[dict]:
    """[{"name", "hostapi"}] for the dropdown. Returns [] rather than raising."""
    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
    except Exception:
        log.exception("audio: could not enumerate input devices")
        return []

    api = _best_host_api(devices, host_apis)
    if api < 0:
        return []
    api_name = host_apis[api].get("name", "")

    out, seen = [], set()
    for dev in devices:
        name = (dev.get("name") or "").strip()
        if dev.get("hostapi") != api or not _is_selectable(dev) or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "hostapi": api_name})
    log.debug("audio: %d input device(s) via %s", len(out), api_name)
    return out


def resolve_input_device(name: str) -> "int | None":
    """
    A saved device name -> PortAudio index, or None meaning "system default".

    config.audio_device holds a name rather than an index because indices are
    positional and shift when hardware is replugged. Falls back to a prefix
    match because MME truncates names to 31 characters.
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
    except Exception:
        log.exception("audio: could not enumerate devices to resolve %r", name)
        return None

    inputs = [
        (i, (d.get("name") or "").strip())
        for i, d in enumerate(devices)
        if d.get("max_input_channels", 0) > 0
    ]
    matches = [i for i, n in inputs if n == name]
    if not matches:
        matches = [
            i for i, n in inputs
            if n and (n.startswith(name) or name.startswith(n))
        ]
    if not matches:
        return None
    return min(matches, key=lambda i: _host_api_rank(host_apis, devices[i].get("hostapi", -1)))


class AudioCapture:
    SAMPLE_RATE = 16000
    CHANNELS = 1
    DTYPE = "int16"
    BLOCK_SIZE = 1024

    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._wav_file: wave.Wave_write | None = None
        self._lock = threading.Lock()
        self._running = False
        self._paused = False

    def start(self, wav_path: Path, device_name: str = "") -> None:
        """Open the WAV and stream audio into it. An unknown device_name falls
        back to the system default rather than failing the recording."""
        wav_path.parent.mkdir(parents=True, exist_ok=True)

        device = None
        if device_name:
            device = resolve_input_device(device_name)
            if device is None:
                log.warning(
                    "audio: configured input device %r not found — using the system default",
                    device_name,
                )
            else:
                log.info("audio: recording from %r (device %d)", device_name, device)

        self._wav_file = wave.open(str(wav_path), "wb")
        self._wav_file.setnchannels(self.CHANNELS)
        self._wav_file.setsampwidth(2)
        self._wav_file.setframerate(self.SAMPLE_RATE)

        self._running = True
        try:
            self._stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                dtype=self.DTYPE,
                blocksize=self.BLOCK_SIZE,
                device=device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception:
            # Leave no 44-byte header behind: later readers treat it as a real
            # but silent recording.
            self._running = False
            if self._wav_file:
                self._wav_file.close()
                self._wav_file = None
            raise

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        with self._lock:
            if self._running and not self._paused and self._wav_file:
                self._wav_file.writeframes(indata.tobytes())

    def set_paused(self, paused: bool) -> None:
        """Drops frames; the stream stays open. Pairs with
        Session._now_offset_ms() to keep audio and event offsets on one clock."""
        with self._lock:
            self._paused = paused

    def stop(self) -> None:
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            if self._wav_file:
                self._wav_file.close()
                self._wav_file = None
