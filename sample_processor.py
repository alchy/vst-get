"""
sample_processor.py — Full processing pipeline for a single recorded sample
============================================================================
Reusable module.  Takes a raw stereo recording of one note+velocity and
returns a trimmed, click-free stereo array ready for export.

Pipeline
--------
1.  Create a mono working copy:  mono = L + R
2.  Normalize mono to –6 dBFS peak.
3.  Onset detection on mono (find_onset, RMS threshold, default –42 dB).
4.  Peak detection on mono from onset forward (find_peak, 1 ms windows).
5.  Fade-out detection on mono from peak forward (find_fadeout, earliest
    window below power threshold; fallback: min-power window).
6.  Apply start_frame / end_frame to the original stereo recording.
7.  Zero-start protection: ensure both channels begin at (or very near)
    zero to prevent audible clicks when the sample is triggered.
    a.  If amplitude at frame 0 < zero_threshold → nothing to do.
    b.  Apply a cosine fade-in whose length scales linearly with the start
        amplitude: fade_n = max(1, round(max_fade_in × A)).
8.  Discard mono copy, return trimmed stereo original (or None if silent).

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


def _normalize(mono: np.ndarray) -> tuple[np.ndarray, float, float]:
    """
    Normalise *mono* so that its peak amplitude equals _TARGET_PEAK_AMP.

    Returns
    -------
    (normalised, original_peak_db, gain_db)
    """
    peak = float(np.max(np.abs(mono)))
    if peak == 0.0:
        return mono.copy(), -np.inf, 0.0
    original_peak_db = 20.0 * np.log10(peak)
    gain = _TARGET_PEAK_AMP / peak
    gain_db = 20.0 * np.log10(gain)
    return (mono * gain).astype(np.float32), original_peak_db, gain_db


def _zero_start(
    data: np.ndarray,
    max_fade_in: int,
    zero_threshold: float,
) -> np.ndarray:
    """
    Ensure the trimmed sample begins at (or very near) zero on all channels
    to prevent audible clicks when the sample is triggered.

    If the start amplitude is already below *zero_threshold* nothing is done.
    Otherwise a cosine fade-in is applied whose length scales with the start
    amplitude: ``fade_n = max(1, round(max_fade_in × A))``.  At full
    amplitude (A ≈ 1) the full *max_fade_in* samples are used; at lower
    amplitudes the fade-in is proportionally shorter.

    Note: a forward zero-crossing search is intentionally NOT performed.
    Shifting the start forward risks clipping the attack transient, which is
    more audibly damaging than a short fade-in.

    Parameters
    ----------
    data : np.ndarray
        Trimmed stereo (or mono) float32 array.
    max_fade_in : int
        Maximum cosine fade-in length in samples.
    zero_threshold : float
        Amplitude below which a sample is considered "at zero".

    Returns
    -------
    np.ndarray
        Data with zero-start protection applied in-place copy.
    """
    if len(data) == 0:
        return data

    if data.ndim == 1:
        amp = float(abs(data[0]))
    else:
        amp = float(np.max(np.abs(data[0])))

    if amp < zero_threshold:
        log.info(
            "  Zero-start  : OK (A=%.5f < threshold=%.4f)",
            amp, zero_threshold,
        )
        return data

    fade_n = max(1, min(round(max_fade_in * amp), len(data)))
    t = np.arange(fade_n, dtype=np.float32)
    ramp = ((1.0 - np.cos(np.pi * t / fade_n)) / 2.0).astype(np.float32)

    result = data.copy()
    if result.ndim == 1:
        result[:fade_n] *= ramp
    else:
        result[:fade_n] *= ramp[:, np.newaxis]

    log.info(
        "  Zero-start  : cosine fade-in %d vzorků  (A=%.4f, max_fade_in=%d)",
        fade_n, amp, max_fade_in,
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
    peak_window_ms: float = 1.0,
    max_fade_in: int = 30,
    zero_threshold: float = 0.001,
) -> np.ndarray | None:
    """
    Process a single raw recorded sample through the full pipeline.

    Parameters
    ----------
    data : np.ndarray
        Raw stereo (or mono) float32 audio from the recorder,
        shape (N,) or (N, channels).
    fs : int
        Sample rate in Hz.
    threshold_db : float
        Onset detection RMS threshold in dB relative to the normalised
        mono peak (default –42 dB).
    fadeout_ratio : float
        Fade-out condition: ``power_window ≤ peak_rms² × fadeout_ratio``
        (default 0.1 = 1/10 of peak power).
    fadeout_coarse_chunks : int
        Number of initial windows for binary subdivision (default 16).
    peak_window_ms : float
        Window size for RMS calculations in ms (default 1 ms).
    max_fade_in : int
        Maximum cosine fade-in length in samples; actual length scales
        with the start amplitude (default 30).
    zero_threshold : float
        Amplitude below which a sample is considered "at zero" for
        zero-start detection (default 0.001 ≈ –60 dBFS).

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
    mono, orig_peak_db, gain_db = _normalize(mono_raw)

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
    start_frame = find_onset(mono, fs,
                             threshold_db=threshold_db,
                             window_ms=peak_window_ms)

    if start_frame >= len(mono):
        log.info("  → onset nenalezen (celý záznam pod prahem), přeskočeno")
        return None

    # ------------------------------------------------------------------
    # Step 3: peak detection (search from onset onward)
    # ------------------------------------------------------------------
    peak_frame_rel, peak_rms = find_peak(mono[start_frame:], fs,
                                         window_ms=peak_window_ms)
    peak_frame_abs = start_frame + peak_frame_rel
    t_peak_ms = peak_frame_abs / fs * 1000.0
    log.info("  Peak (abs)  : frame=%d  t=%.1f ms", peak_frame_abs, t_peak_ms)

    # ------------------------------------------------------------------
    # Step 4: fade-out detection
    # ------------------------------------------------------------------
    end_frame = find_fadeout(
        mono, fs,
        peak_frame=peak_frame_abs,
        peak_rms=peak_rms,
        fadeout_ratio=fadeout_ratio,
        coarse_chunks=fadeout_coarse_chunks,
    )

    if end_frame <= start_frame:
        log.info("  → end_frame ≤ start_frame, přeskočeno")
        return None

    # ------------------------------------------------------------------
    # Step 5: trim original stereo
    # ------------------------------------------------------------------
    result = data[start_frame:end_frame].copy()

    # ------------------------------------------------------------------
    # Step 6: zero-start protection
    # ------------------------------------------------------------------
    result = _zero_start(result, max_fade_in, zero_threshold)

    dur_ms = len(result) / fs * 1000.0
    log.info("  Délka       : %.1f ms", dur_ms)

    return result if len(result) > 0 else None
