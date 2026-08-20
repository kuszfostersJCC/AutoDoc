"""Local Whisper speech-to-text; models live in models/ beside the executable."""

import logging
import sys
import time
import wave as wave_module
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import whisper as _whisper

log = logging.getLogger(__name__)


def get_model_cache_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "models"
    return Path(__file__).parent.parent / "models"


def load_whisper_model(model_name: str = "base") -> "_whisper.Whisper":
    """Load a Whisper model, preferring the cached local file. Any failure, including
    a frozen torch import failing without the VC++ runtime, raises RuntimeError."""
    t0 = time.monotonic()
    try:
        log.info("load_whisper_model(%r): importing torch ...", model_name)
        import torch
        log.info(
            "import torch OK in %.1fs (version=%s, from %s)",
            time.monotonic() - t0, torch.__version__, getattr(torch, "__file__", "?"),
        )

        t1 = time.monotonic()
        log.info("load_whisper_model(%r): importing whisper ...", model_name)
        import whisper
        log.info(
            "import whisper OK in %.1fs (from %s)",
            time.monotonic() - t1, getattr(whisper, "__file__", "?"),
        )
    except Exception as exc:
        log.exception(
            "load_whisper_model(%r): FAILED during import after %.1fs. "
            "If this is OSError [WinError 126/127] ('DLL load failed' / "
            "'specified module could not be found') the target machine is "
            "missing the Microsoft Visual C++ Redistributable (x64) that "
            "torch's native DLLs require — install it from "
            "https://aka.ms/vs/17/release/vc_redist.x64.exe and retry.",
            model_name, time.monotonic() - t0,
        )
        raise RuntimeError(
            f"Failed to load the speech engine (torch/whisper import): {exc}\n"
            "If this machine is missing the Microsoft Visual C++ Redistributable, "
            "install vc_redist.x64.exe from Microsoft and try again."
        ) from exc

    cache_dir = get_model_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_path = cache_dir / f"{model_name}.pt"
    have_local_file = local_path.exists()
    if have_local_file:
        log.info(
            "load_whisper_model(%r): cache_dir=%s local_file_exists=True size=%d bytes",
            model_name, cache_dir, local_path.stat().st_size,
        )
    else:
        log.info("load_whisper_model(%r): cache_dir=%s local_file_exists=False", model_name, cache_dir)
    t0 = time.monotonic()
    try:
        if have_local_file:
            # By path, not name: the name-based form checksums the file against
            # whisper's registry and silently redownloads on any mismatch.
            model = whisper.load_model(str(local_path))
        else:
            model = whisper.load_model(model_name, download_root=str(cache_dir))
    except Exception as exc:
        log.exception("load_whisper_model(%r) failed after %.1fs", model_name, time.monotonic() - t0)
        raise RuntimeError(
            f"Failed to load Whisper model '{model_name}'.\n"
            f"Check your internet connection on first run (model must be downloaded).\n"
            f"Detail: {exc}"
        ) from exc
    elapsed = time.monotonic() - t0
    log.info("load_whisper_model(%r) succeeded in %.1fs", model_name, elapsed)
    if elapsed > 15:
        log.warning(
            "load_whisper_model(%r) took %.1fs, much longer than a warm local load "
            "(usually a few seconds) — worth investigating if this recurs.",
            model_name, elapsed,
        )
    return model


def is_model_cached(model_name: str = "base") -> bool:
    """True if the model file exists locally. Existence only: no hashing, no imports."""
    model_path = get_model_cache_dir() / f"{model_name}.pt"
    return model_path.exists()


def _load_wav_as_float32(wav_path: Path) -> np.ndarray:
    """Read a 16 kHz int16 PCM WAV as a float32 array in [-1, 1]; stereo is downmixed.
    Passing Whisper an array rather than a path avoids its ffmpeg shell-out."""
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
    """Transcribe a WAV into 'transcript_segment' events for session.json.
    timestamp_offset_ms is relative to session start, as for screenshot events."""
    audio = _load_wav_as_float32(wav_path)
    audio_seconds = len(audio) / 16000

    log.info("transcribe_audio: %s (%.1fs of audio)", wav_path, audio_seconds)
    t0 = time.monotonic()
    # verbose=None, NOT False: whisper builds its tqdm bar with
    # `disable=verbose is not False`, and it writes to sys.stderr, None when frozen.
    result = model.transcribe(audio, fp16=False, verbose=None)
    elapsed = time.monotonic() - t0
    log.info(
        "transcribe_audio: done in %.1fs for %.1fs of audio (%.1fx real-time)",
        elapsed, audio_seconds, (elapsed / audio_seconds) if audio_seconds else 0.0,
    )

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
