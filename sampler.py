"""
sampler.py — VST instrument sampling logic
===========================================
Reusable module.  Handles MIDI note playback and raw audio capture.
Audio processing (onset, fade-out, zero-start) is delegated to
sample_processor.process_sample().

Requires: numpy, mido, wasapi_recorder, sample_processor, wav_io.

Example
-------
    import pyaudiowpatch as pyaudio
    from wasapi_recorder import Recorder, select_loopback_device
    from midi_utils import open_midi_port
    from sampler import sample_all, VELOCITY_LAYERS

    p = pyaudio.PyAudio()
    dev_idx, rate, ch = select_loopback_device(p)
    rec = Recorder(p, dev_idx, rate, ch)
    midi_out = open_midi_port("loopMIDI port")

    sample_all(rec, midi_out, output_dir=Path("out"), sample_rate=rate, channels=ch)

    midi_out.close()
    p.terminate()
"""

import logging
import time
from pathlib import Path

import mido
import numpy as np

from sample_processor import process_sample
from wasapi_recorder import Recorder
from wav_io import save_wav

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

PIANO_LOW = 21        # A0
PIANO_HIGH = 108      # C8

NOTE_HOLD = 29.0      # seconds note-on is held
NOTE_RELEASE = 1.0    # seconds after note-off (decay / silence tail)
TOTAL_DURATION = NOTE_HOLD + NOTE_RELEASE
PREROLL = 0.15        # seconds captured before note-on (protects attack)

# 8 velocity layers evenly spaced across 1–127
# Formula: round(127/8 * (i+1)) for i in 0..7
VELOCITY_LAYERS: list[int] = [round(127 / 8 * (i + 1)) for i in range(8)]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def record_one(
    recorder: Recorder,
    midi_out: mido.ports.BaseOutput,
    note: int,
    velocity: int,
    midi_channel: int,
    sample_rate: int,
) -> np.ndarray:
    """
    Record a single note+velocity combination and return raw audio.

    Starts the recorder, waits for a preroll, sends note-on, holds for
    NOTE_HOLD seconds, sends note-off, waits for NOTE_RELEASE seconds.
    DC offset is removed before returning.  Silence trimming and fade-out
    detection are NOT performed here — call process_sample() separately.

    Parameters
    ----------
    recorder : Recorder
    midi_out : mido.ports.BaseOutput
    note : int
        MIDI note number (0–127).
    velocity : int
        MIDI velocity (1–127).
    midi_channel : int
        MIDI channel (0–15).
    sample_rate : int

    Returns
    -------
    np.ndarray
        Raw float32 audio, shape (N,) or (N, channels).
    """
    recorder.start(TOTAL_DURATION + PREROLL)
    time.sleep(PREROLL)

    midi_out.send(mido.Message("note_on", note=note, velocity=velocity, channel=midi_channel))
    time.sleep(NOTE_HOLD)
    midi_out.send(mido.Message("note_off", note=note, velocity=0, channel=midi_channel))
    time.sleep(NOTE_RELEASE)

    data = recorder.get()

    # Remove DC offset
    if data.ndim == 1:
        data -= np.mean(data)
    else:
        data -= np.mean(data, axis=0)

    return data


def sample_all(
    recorder: Recorder,
    midi_out: mido.ports.BaseOutput,
    output_dir: Path,
    sample_rate: int,
    channels: int,
    note_start: int = PIANO_LOW,
    note_end: int = PIANO_HIGH,
    midi_channel: int = 0,
    velocity_layers: list[int] | None = None,
    # process_sample kwargs forwarded verbatim
    threshold_db: float = -42.0,
    fadeout_ratio: float = 0.1,
    fadeout_coarse_chunks: int = 16,
    max_fade_in: int = 30,
    zero_threshold: float = 0.001,
) -> None:
    """
    Record all notes and velocity layers and save processed WAV files.

    Each raw recording is passed through process_sample() which handles
    onset detection, fade-out detection (binary subdivision of average power),
    and zero-start protection.  If a processed result is silent, the file is
    not saved.

    Output filename format: ``m<NNN>-vel<V>-f<SR>.wav``
    where NNN = zero-padded MIDI note number, V = layer index,
    SR = sample rate in kHz (e.g. f48 or f44).

    Parameters
    ----------
    recorder : Recorder
    midi_out : mido.ports.BaseOutput
    output_dir : Path
        Directory where WAV files are written (must already exist).
    sample_rate : int
    channels : int
    note_start : int
    note_end : int
    midi_channel : int
    velocity_layers : list[int] or None
        MIDI velocity values per layer.  Defaults to ``VELOCITY_LAYERS``.
    threshold_db : float
        Onset RMS threshold in dB relative to normalised peak (default –42 dB).
    fadeout_ratio : float
        Fade-out power threshold as fraction of peak power (default 0.1).
    fadeout_coarse_chunks : int
        Initial window count for binary subdivision (default 16).
    max_fade_in : int
        Maximum cosine fade-in length in samples (default 30).
    zero_threshold : float
        Amplitude below which start is considered "at zero" (default 0.001).
    """
    if velocity_layers is None:
        velocity_layers = VELOCITY_LAYERS

    freq_tag = f"f{sample_rate // 1000}"
    total = (note_end - note_start + 1) * len(velocity_layers)
    n = 0
    saved = 0
    skipped = 0

    log.info(
        "Rozsah not: %d–%d  ×  %d velocity vrstev  =  %d vzorků",
        note_start, note_end, len(velocity_layers), total,
    )
    log.info("Odhadovaný čas: %.0f min", total * TOTAL_DURATION / 60)
    log.info("Velocity vrstvy: %s", velocity_layers)

    for note in range(note_start, note_end + 1):
        for vel_layer, velocity in enumerate(velocity_layers):
            n += 1
            print(
                f"[{n:>4}/{total}]  nota={note:>3}  vrstva={vel_layer}  vel={velocity:>3}",
                flush=True,
            )

            raw = record_one(
                recorder, midi_out, note, velocity, midi_channel, sample_rate,
            )

            processed = process_sample(
                raw, sample_rate,
                threshold_db=threshold_db,
                fadeout_ratio=fadeout_ratio,
                fadeout_coarse_chunks=fadeout_coarse_chunks,
                max_fade_in=max_fade_in,
                zero_threshold=zero_threshold,
            )

            if processed is None:
                log.info("  → ticho, přeskočeno")
                skipped += 1
                continue

            filename = f"m{note:03d}-vel{vel_layer}-{freq_tag}.wav"
            save_wav(processed, output_dir / filename, sample_rate, channels)
            dur_ms = len(processed) / sample_rate * 1000.0
            log.info("  Uloženo     : %s  (%.1f ms)", filename, dur_ms)
            saved += 1

    print()
    log.info("Hotovo. Uloženo: %d  Přeskočeno: %d", saved, skipped)
