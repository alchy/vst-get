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
import logging.config
import time
from pathlib import Path

import mido
import numpy as np

from .sample_processor import process_sample
from .wasapi_recorder import Recorder
from .wav_io import save_wav

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

PIANO_LOW = 21        # A0
PIANO_HIGH = 108      # C8

NOTE_HOLD = 29.0      # seconds note-on is held
NOTE_RELEASE = 1.0    # seconds after note-off (decay / silence tail)
TOTAL_DURATION = NOTE_HOLD + NOTE_RELEASE
PREROLL = 0.15        # seconds captured before note-on (protects attack)

# --prevent-damper-sound timing
# After recording ends the note is still held; note-off (damper) is sent
# only after DAMPER_PRE_DELAY seconds — outside the recorded window.
# After note-off a further DAMPER_POST_DELAY pause lets the instrument
# settle before the next note begins.
DAMPER_PRE_DELAY = 2.5    # s between end of recording and note-off
DAMPER_POST_DELAY = 2.5   # s between note-off and start of next note

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
    prevent_damper: bool = False,
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
    prevent_damper : bool
        When True the note is held for the entire recording window so the
        damper never falls during capture.  After recording finishes:
        wait DAMPER_PRE_DELAY s, send note-off (damper falls, not recorded),
        wait DAMPER_POST_DELAY s before returning.  Total wall-clock time
        per note increases by DAMPER_PRE_DELAY + DAMPER_POST_DELAY seconds.

    Returns
    -------
    np.ndarray
        Raw float32 audio, shape (N,) or (N, channels).
    """
    recorder.start(TOTAL_DURATION + PREROLL)
    time.sleep(PREROLL)

    midi_out.send(mido.Message("note_on", note=note, velocity=velocity, channel=midi_channel))

    if prevent_damper:
        # Hold the note for the full recording window — damper never fires
        # during capture.
        time.sleep(TOTAL_DURATION)
        data = recorder.get()
        # Send note-off (damper) only after recording is done.
        time.sleep(DAMPER_PRE_DELAY)
        midi_out.send(mido.Message("note_off", note=note, velocity=0, channel=midi_channel))
        time.sleep(DAMPER_POST_DELAY)
    else:
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
    verbose: bool = True,
    prevent_damper: bool = False,
    # process_sample kwargs forwarded verbatim
    preroll_ms: float = 120.0,
    onset_snr_db: float = 6.0,
    onset_window_ms: float = 10.0,
    peak_window_ms: float = 10.0,
    fadeout_snr_db: float = 6.0,
    fadeout_coarse_chunks: int = 16,
    fadeout_min_window_ms: float = 100.0,
    tail_fade_ms: float = 500.0,
    max_fade_samples: int = 96,
    zero_threshold: float = 0.001,
) -> None:
    """
    Record all notes and velocity layers and save processed WAV files.

    Each raw recording is passed through process_sample() which handles
    onset detection, fade-out detection (binary subdivision), zero-start
    and zero-end protection.  If a processed result is silent, the file
    is not saved.

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
    verbose : bool
        When True (default), log the full processing pipeline for every
        sample.  When False, suppress detailed vstget logger output and
        print a single compact line per sample instead.
    prevent_damper : bool
        When True, note-off is deferred until after recording ends so the
        damper sound is never captured.  Adds DAMPER_PRE_DELAY +
        DAMPER_POST_DELAY seconds of unrecorded pause per note.
    preroll_ms : float
        Duration of guaranteed-silent preroll used for noise floor
        estimation (default 120 ms, within the 150 ms PREROLL).
    onset_snr_db : float
        Onset fires when RMS exceeds noise floor by this many dB (default 6).
    onset_window_ms : float
        RMS window for onset detection in ms (default 10 ms).
    peak_window_ms : float
        RMS window for peak detection in ms (default 10 ms).
    fadeout_snr_db : float
        Fade-out fires when RMS drops to within this many dB of noise
        floor (default 6 dB).
    fadeout_coarse_chunks : int
        Initial window count for binary subdivision (default 16).
    fadeout_min_window_ms : float
        Minimum subdivision window in ms (default 100 ms).
    max_fade_samples : int
        Maximum cosine fade length for start and end edges (default 30).
    zero_threshold : float
        Amplitude below which an edge is considered "at zero" (default 0.001).
    """
    if velocity_layers is None:
        velocity_layers = VELOCITY_LAYERS

    if not verbose:
        logging.getLogger("vstget").setLevel(logging.WARNING)

    freq_tag = f"f{sample_rate // 1000}"
    total = (note_end - note_start + 1) * len(velocity_layers)
    n = 0
    saved = 0
    skipped = 0

    print(
        f"Rozsah not: {note_start}–{note_end}  ×  {len(velocity_layers)} velocity vrstev"
        f"  =  {total} vzorků",
        flush=True,
    )
    extra = DAMPER_PRE_DELAY + DAMPER_POST_DELAY if prevent_damper else 0.0
    print(f"Odhadovaný čas: {total * (TOTAL_DURATION + extra) / 60:.0f} min", flush=True)
    if prevent_damper:
        print(
            f"--prevent-damper-sound: note-off odložen o {DAMPER_PRE_DELAY:.1f} s po záznamu,"
            f" pauza {DAMPER_POST_DELAY:.1f} s před další notou",
            flush=True,
        )

    for note in range(note_start, note_end + 1):
        for vel_layer, velocity in enumerate(velocity_layers):
            n += 1
            filename = f"m{note:03d}-vel{vel_layer}-{freq_tag}.wav"

            if verbose:
                print(
                    f"[{n:>4}/{total}]  nota={note:>3}  vrstva={vel_layer}  vel={velocity:>3}",
                    flush=True,
                )

            raw = record_one(
                recorder, midi_out, note, velocity, midi_channel, sample_rate,
                prevent_damper=prevent_damper,
            )

            processed, stats = process_sample(
                raw, sample_rate,
                preroll_ms=preroll_ms,
                onset_snr_db=onset_snr_db,
                onset_window_ms=onset_window_ms,
                peak_window_ms=peak_window_ms,
                fadeout_snr_db=fadeout_snr_db,
                fadeout_coarse_chunks=fadeout_coarse_chunks,
                fadeout_min_window_ms=fadeout_min_window_ms,
                tail_fade_ms=tail_fade_ms,
                max_fade_samples=max_fade_samples,
                zero_threshold=zero_threshold,
            )

            if processed is None:
                if verbose:
                    log.info("  → ticho, přeskočeno")
                else:
                    print(
                        f"[{n:>4}/{total}]  file={filename}  SKIP (ticho)",
                        flush=True,
                    )
                skipped += 1
                continue

            save_wav(processed, output_dir / filename, sample_rate, channels)
            dur_ms = len(processed) / sample_rate * 1000.0

            if verbose:
                log.info("  Uloženo     : %s  (%.1f ms)", filename, dur_ms)
            else:
                peak_db = stats.get("peak_rms_db", 0.0)
                print(
                    f"[{n:>4}/{total}]  file={filename}"
                    f"  peak={peak_db:+.1f} dBFS  t={dur_ms:.0f} ms",
                    flush=True,
                )
            saved += 1

    print()
    print(f"Hotovo. Uloženo: {saved}  Přeskočeno: {skipped}", flush=True)
