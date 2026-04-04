"""
sample_processor.py — Full processing pipeline for a single recorded sample
============================================================================
Reusable module.  Takes a raw stereo recording of one note+velocity and
returns a trimmed, click-free stereo array ready for export.

Pipeline
--------
1.  Create a mono working copy:  mono = L + R
2.  Normalize mono to –6 dBFS peak, gain capped at max_gain_db to prevent
    excessive amplification of noise on very soft velocity layers.
3.  Onset detection on mono (find_onset, RMS threshold, default –42 dB).
4.  Peak detection on mono from onset forward (find_peak, 1 ms windows).
5.  Fade-out detection on mono from peak forward (find_fadeout, earliest
    window below power threshold; min window 100 ms; fallback: min-power).
6.  Apply start_frame / end_frame to the original stereo recording.
7.  Zero-start protection: cosine fade-in if first sample is not near zero.
8.  Zero-end protection: cosine fade-out if last sample is not near zero.
9.  Discard mono copy, return trimmed stereo original (or None if silent).

Example
-------
    from sample_processor import process_sample

    raw = recorder.get()           # stereo float32, shape (N, 2)
    result = process_sample(raw, fs=48000)
    if result is not None:
        save_wav(result, "note.wav", 48000, 2)
"""

import logging

import numpy as np

from peak_detector import find_fadeout, find_onset, find_peak

log = logging.getLogger(__name__)

# Target peak level for the normalised mono working copy
_TARGET_PEAK_DB = -6.0
_TARGET_PEAK_AMP = 10.0 ** (_TARGET_PEAK_DB / 20.0)   # ≈ 0.5012


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_mono(data: np.ndarray) -> np.ndarray:
    """Sum all channels to mono (works for both mono and stereo input)."""
    if data.ndim == 1:
        return data.copy().astype(np.float32)
    return data.sum(axis=1).astype(np.float32)


def _normalize(
    mono: np.ndarray,
    max_gain_db: float = 40.0,
) -> tuple[np.ndarray, float, float]:
    """
    Normalise *mono* so that its peak amplitude equals _TARGET_PEAK_AMP,
    with gain capped at *max_gain_db*.

    The gain cap prevents extreme amplification of noise on very quiet
    velocity layers where the signal-to-noise ratio is poor.  When the cap
    is applied the resulting peak will be below –6 dBFS.

    Returns
    -------
    (normalised, original_peak_db, gain_db)
    """
    peak = float(np.max(np.abs(mono)))
    if peak == 0.0:
        return mono.copy(), -np.inf, 0.0

    original_peak_db = 20.0 * np.log10(peak)
    ideal_gain = _TARGET_PEAK_AMP / peak
    max_gain_amp = 10.0 ** (max_gain_db / 20.0)

    if ideal_gain > max_gain_amp:
        gain = max_gain_amp
        actual_peak_db = original_peak_db + max_gain_db
        log.info(
            "  Normalizace : gain omezen na +%.0f dB  "
            "(ideální gain %+.1f dB → výsledný peak=%.1f dBFS)",
            max_gain_db, 20.0 * np.log10(ideal_gain), actual_peak_db,
        )
    else:
        gain = ideal_gain

    gain_db = 20.0 * np.log10(gain)
    return (mono * gain).astype(np.float32), original_peak_db, gain_db


def _cosine_fade(
    data: np.ndarray,
    fade_n: int,
    at_end: bool,
) -> np.ndarray:
    """
    Apply a cosine fade-in (at_end=False) or fade-out (at_end=True).

    Fade-in  ramp: (1 − cos(π·i/N)) / 2  →  0 … 1
    Fade-out ramp: (1 + cos(π·i/N)) / 2  →  1 … 0
    """
    t = np.arange(fade_n, dtype=np.float32)
    if at_end:
        ramp = ((1.0 + np.cos(np.pi * t / fade_n)) / 2.0).astype(np.float32)
        if data.ndim == 1:
            data[-fade_n:] *= ramp
        else:
            data[-fade_n:] *= ramp[:, np.newaxis]
    else:
        ramp = ((1.0 - np.cos(np.pi * t / fade_n)) / 2.0).astype(np.float32)
        if data.ndim == 1:
            data[:fade_n] *= ramp
        else:
            data[:fade_n] *= ramp[:, np.newaxis]
    return data


def _zero_edge(
    data: np.ndarray,
    max_fade_samples: int,
    zero_threshold: float,
    at_end: bool,
) -> np.ndarray:
    """
    Ensure the sample starts or ends at (or very near) zero on all channels.

    If the edge amplitude is already below *zero_threshold* nothing is done.
    Otherwise a cosine fade of length ``max(1, round(max_fade_samples × A))``
    samples is applied, scaling with the actual edge amplitude.

    Parameters
    ----------
    data : np.ndarray
        Stereo or mono float32 array.
    max_fade_samples : int
        Maximum fade length in samples.
    zero_threshold : float
        Amplitude below which the edge is considered "at zero".
    at_end : bool
        True → fade-out at end; False → fade-in at start.
    """
    if len(data) == 0:
        return data

    edge = data[-1] if at_end else data[0]
    if data.ndim == 1:
        amp = float(abs(edge))
    else:
        amp = float(np.max(np.abs(edge)))

    label = "Zero-end  " if at_end else "Zero-start"

    if amp < zero_threshold:
        log.info("  %s  : OK (A=%.5f < threshold=%.4f)", label, amp, zero_threshold)
        return data

    fade_n = max(1, min(round(max_fade_samples * amp), len(data)))
    result = data.copy()
    _cosine_fade(result, fade_n, at_end=at_end)

    fade_type = "fade-out" if at_end else "fade-in"
    log.info(
        "  %s  : cosine %s %d vzorků  (A=%.4f, max=%d)",
        label, fade_type, fade_n, amp, max_fade_samples,
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_sample(
    data: np.ndarray,
    fs: int,
    threshold_db: float = -42.0,
    fadeout_ratio: float = 0.1,
    fadeout_coarse_chunks: int = 16,
    fadeout_min_window_ms: float = 100.0,
    peak_window_ms: float = 1.0,
    max_fade_samples: int = 30,
    zero_threshold: float = 0.001,
    max_gain_db: float = 40.0,
) -> np.ndarray | None:
    """
    Process a single raw recorded sample through the full pipeline.

    Parameters
    ----------
    data : np.ndarray
        Raw stereo (or mono) float32 audio, shape (N,) or (N, channels).
    fs : int
        Sample rate in Hz.
    threshold_db : float
        Onset detection RMS threshold in dB relative to the normalised
        mono peak (default –42 dB).
    fadeout_ratio : float
        Fade-out condition: ``power ≤ peak_rms² × fadeout_ratio``
        (default 0.1 = 1/10 of peak power).
    fadeout_coarse_chunks : int
        Number of initial windows for binary subdivision (default 16).
    fadeout_min_window_ms : float
        Minimum window size during binary subdivision in ms (default 100 ms).
        Prevents sub-period analysis for bass frequencies.
    peak_window_ms : float
        Window size for onset/peak RMS calculations in ms (default 1 ms).
    max_fade_samples : int
        Maximum cosine fade length in samples for both start and end;
        actual length scales with edge amplitude (default 30).
    zero_threshold : float
        Amplitude below which a sample edge is considered "at zero"
        (default 0.001 ≈ –60 dBFS).
    max_gain_db : float
        Maximum normalisation gain in dB (default 40 dB).  Caps amplification
        for very quiet velocity layers to limit noise floor boost.

    Returns
    -------
    np.ndarray or None
        Trimmed stereo float32 array, or None if the recording is silent.
    """
    if len(data) == 0:
        return None

    # ------------------------------------------------------------------
    # Step 1: mono working copy + normalise
    # ------------------------------------------------------------------
    mono_raw = _to_mono(data)
    mono, orig_peak_db, gain_db = _normalize(mono_raw, max_gain_db=max_gain_db)

    log.info(
        "  Normalizace : gain=%+.1f dB  (originální peak=%.1f dBFS → cíl %.1f dBFS)",
        gain_db, orig_peak_db, _TARGET_PEAK_DB,
    )

    if np.max(np.abs(mono)) == 0.0:
        log.info("  → záznam je tichý, přeskočeno")
        return None

    # ------------------------------------------------------------------
    # Step 2: onset detection
    # ------------------------------------------------------------------
    start_frame = find_onset(
        mono, fs,
        threshold_db=threshold_db,
        window_ms=peak_window_ms,
    )

    if start_frame >= len(mono):
        log.info("  → onset nenalezen (celý záznam pod prahem), přeskočeno")
        return None

    # ------------------------------------------------------------------
    # Step 3: peak detection (from onset onward)
    # ------------------------------------------------------------------
    peak_frame_rel, peak_rms = find_peak(
        mono[start_frame:], fs,
        window_ms=peak_window_ms,
    )
    peak_frame_abs = start_frame + peak_frame_rel
    log.info("  Peak (abs)  : frame=%d  t=%.1f ms", peak_frame_abs, peak_frame_abs / fs * 1000.0)

    # ------------------------------------------------------------------
    # Step 4: fade-out detection
    # ------------------------------------------------------------------
    end_frame = find_fadeout(
        mono, fs,
        peak_frame=peak_frame_abs,
        peak_rms=peak_rms,
        fadeout_ratio=fadeout_ratio,
        coarse_chunks=fadeout_coarse_chunks,
        min_window_ms=fadeout_min_window_ms,
    )

    if end_frame <= start_frame:
        log.info("  → end_frame ≤ start_frame, přeskočeno")
        return None

    # ------------------------------------------------------------------
    # Step 5: trim original stereo
    # ------------------------------------------------------------------
    result = data[start_frame:end_frame].copy()

    # ------------------------------------------------------------------
    # Step 6: zero-start — cosine fade-in if first sample is not near zero
    # ------------------------------------------------------------------
    result = _zero_edge(result, max_fade_samples, zero_threshold, at_end=False)

    # ------------------------------------------------------------------
    # Step 7: zero-end — cosine fade-out if last sample is not near zero
    # ------------------------------------------------------------------
    result = _zero_edge(result, max_fade_samples, zero_threshold, at_end=True)

    dur_ms = len(result) / fs * 1000.0
    log.info("  Délka       : %.1f ms", dur_ms)

    return result if len(result) > 0 else None
