"""
transcription/whisper_runner.py — Local Whisper speech-to-text.

Audio is transcribed entirely on-device. No audio data is ever sent to a
remote service. Whisper models are stored in a "models/" folder adjacent to
the executable so the full AutoDoc distribution remains portable.

IMPORTANT: We load the WAV file ourselves as a numpy float32 array and pass
it directly to model.transcribe(). This bypasses Whisper's default behaviour
of shelling out to ffmpeg, which is not installed on most consumer Windows
machines. Our recordings are always uncompressed PCM (int16) WAV files, so
the pure-Python wave module is all we need.

Model download happens automatically on first use. The base model is ~150 MB
and runs in roughly 1-2× real-time on a modern CPU.
"""

import sys
import wave as wave_module
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import whisper as _whisper


def get_model_cache_dir() -> Path:
    """Returns the models/ folder next to the .exe (or project root in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "models"
    return Path(__file__).parent.parent / "models"


def load_whisper_model(model_name: str = "base") -> "_whisper.Whisper":
    """
    Load (and if necessary, download) the requested Whisper model.

    Models are stored in get_model_cache_dir() so they survive alongside the
    portable exe rather than in the user's home directory.

    Raises RuntimeError with a user-friendly message if loading fails.
    """
    import whisper  # imported late to keep startup fast when not yet needed

    cache_dir = get_model_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        return whisper.load_model(model_name, download_root=str(cache_dir))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load Whisper model '{model_name}'.\n"
            f"Check your internet connection on first run (model must be downloaded).\n"
            f"Detail: {exc}"
        ) from exc


def is_model_cached(model_name: str = "base") -> bool:
    """Returns True if the model file already exists locally."""
    model_path = get_model_cache_dir() / f"{model_name}.pt"
    return model_path.exists()


def _load_wav_as_float32(wav_path: Path) -> np.ndarray:
    """
    Read a PCM WAV file and return a float32 numpy array normalised to [-1, 1].

    Whisper expects a 1-D float32 array at 16 kHz (mono). Our recordings are
    always 16 kHz mono int16 PCM, so no resampling is needed.
    Stereo files (unlikely, but handled) are downmixed to mono.
    """
    with wave_module.open(str(wav_path), "rb") as wf:
        n_channels = wf.getnchannels()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)

    return audio


def transcribe_audio(
    model: "_whisper.Whisper",
    wav_path: Path,
    session_started_at_iso: str,
) -> list[dict]:
    """
    Transcribe a WAV file and return a list of session events.

    Whisper returns timestamped segments. Each segment becomes a
    'transcript_segment' event whose timestamp_offset_ms is relative to the
    session start, matching the coordinate system used by screenshot events.

    Args:
        model:                    Loaded Whisper model.
        wav_path:                 Path to the session's audio_full.wav.
        session_started_at_iso:   ISO-format datetime string for session start.

    Returns:
        List of event dicts ready to be merged into session.json.
    """
    # Load audio without ffmpeg — pure Python WAV reader.
    audio = _load_wav_as_float32(wav_path)

    result = model.transcribe(audio, fp16=False, verbose=False)

    events = []
    for i, seg in enumerate(result.get("segments", [])):
        text = seg.get("text", "").strip()
        if not text:
            continue

        offset_ms = int(seg["start"] * 1000)

        # avg_logprob is typically in [-inf, 0]; map to a rough 0-1 confidence.
        avg_logprob = seg.get("avg_logprob", -0.5)
        confidence = round(min(1.0, max(0.0, 1.0 + avg_logprob)), 3)

        events.append(
            {
                "event_id": f"seg_{i + 1:04d}",
                "type": "transcript_segment",
                "timestamp_offset_ms": offset_ms,
                "data": {
                    "text": text,
                    "whisper_start_s": round(seg["start"], 3),
                    "whisper_end_s": round(seg["end"], 3),
                    "confidence": confidence,
                },
                "edited_text": None,
            }
        )

    return events
