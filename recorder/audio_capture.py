"""
recorder/audio_capture.py — Continuously records microphone audio to a WAV file.

Audio is captured at 16kHz mono (Whisper's native format) and written directly
to disk in real time. The sounddevice PortAudio callback is kept minimal to
avoid dropped frames.
"""

import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd


class AudioCapture:
    SAMPLE_RATE = 16000
    CHANNELS = 1
    DTYPE = "int16"
    BLOCK_SIZE = 1024  # frames per callback

    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._wav_file: wave.Wave_write | None = None
        self._lock = threading.Lock()
        self._running = False

    def start(self, wav_path: Path) -> None:
        """Open the WAV file and begin streaming microphone audio into it."""
        wav_path.parent.mkdir(parents=True, exist_ok=True)

        self._wav_file = wave.open(str(wav_path), "wb")
        self._wav_file.setnchannels(self.CHANNELS)
        self._wav_file.setsampwidth(2)  # int16 = 2 bytes per sample
        self._wav_file.setframerate(self.SAMPLE_RATE)

        self._running = True
        self._stream = sd.InputStream(
            samplerate=self.SAMPLE_RATE,
            channels=self.CHANNELS,
            dtype=self.DTYPE,
            blocksize=self.BLOCK_SIZE,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """PortAudio callback — writes raw PCM bytes directly to the WAV file."""
        with self._lock:
            if self._running and self._wav_file:
                self._wav_file.writeframes(indata.tobytes())

    def stop(self) -> None:
        """Stop the stream and flush + close the WAV file."""
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            if self._wav_file:
                self._wav_file.close()
                self._wav_file = None
