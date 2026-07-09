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

No network access, ever, when the model file is already present: Whisper's
own load_model() has two code paths — pass it a bare model name ("base") and
it looks the name up in its internal _MODELS registry, verifies the local
file's checksum against what that registry expects, and silently redownloads
over the network on any mismatch (no visible progress in a console=False
build, and whisper's own urlopen() call has no timeout — a network that
silently drops the connection instead of rejecting it can hang this forever).
Pass it a real file PATH instead and it skips all of that entirely, loading
exactly that file with zero network involvement. Since we already control
and ship the exact model file at models/base.pt, we always use the
path-based form when that file exists, and only fall back to the name-based
(network-capable) form if it's genuinely missing.
"""

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
    """Returns the models/ folder next to the .exe (or project root in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "models"
    return Path(__file__).parent.parent / "models"


def load_whisper_model(model_name: str = "base") -> "_whisper.Whisper":
    """
    Load the requested Whisper model, preferring an already-cached local file
    and only falling back to whisper's own name-based (network-capable)
    loader when nothing local exists yet. See module docstring for why.

    Models are stored in get_model_cache_dir() so they survive alongside the
    portable exe rather than in the user's home directory.

    Raises RuntimeError with a user-friendly message if loading fails.
    """
    # ---- Import phase, instrumented step by step. -------------------------
    # torch and whisper are imported late to keep startup fast, and imported
    # SEPARATELY with timing because in a frozen build `import torch` is the
    # single most fragile step on a machine that isn't the dev laptop: it
    # loads a chain of native DLLs (torch_cpu.dll, fbgemm.dll, ...) that
    # depend on the Microsoft VC++ runtime being installed system-wide.
    # On a clean PC that raises OSError [WinError 126] — which is NOT a
    # RuntimeError, so before 2026-07-08 it escaped every handler and killed
    # the preload thread with no log trace at all. Everything below is inside
    # try/except so *any* failure is logged with a traceback and re-raised as
    # the RuntimeError the callers expect.
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

    # ---- Model load phase. ------------------------------------------------
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
            # Load by path: guaranteed no network call, regardless of what
            # whisper's internal registry thinks this file's checksum should be.
            model = whisper.load_model(str(local_path))
        else:
            # Nothing local — genuine first-run download via whisper's own
            # name-based loader. This is the ONLY path that can touch the network.
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
    """Returns True if the model file already exists locally. Existence only —
    no hash verification, no import of whisper/torch, nothing that can fail
    or touch the network. load_whisper_model() above never trusts Whisper's
    own checksum logic anyway once a local file is present, so this doesn't
    need to either; it's purely used to decide whether to show a one-time
    "Downloading speech model" notification."""
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
    audio_seconds = len(audio) / 16000

    log.info("transcribe_audio: %s (%.1fs of audio)", wav_path, audio_seconds)
    t0 = time.monotonic()
    # verbose=None, NOT False: whisper's transcribe() builds its tqdm progress
    # bar with `disable=verbose is not False`, so False ENABLES the bar and
    # None disables it. The bar writes to sys.stderr, which is None in a
    # console=False frozen build — crashed with AttributeError on 2026-07-08.
    # A tray app has no console to show a progress bar in anyway. (main.py
    # also nulls out sys.stdout/stderr as a second line of defense.)
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
