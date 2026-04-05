"""
sample_processor.py — Full processing pipeline for a single recorded sample
============================================================================
Reusable module.  Takes a raw stereo recording of one note+velocity and
returns a trimmed, click-free stereo array ready for export.

Pipeline
--------
1.  Mono working copy: mean(L, R)  — average, not sum.
2.  Noise floor estimation from the preroll (guaranteed-silent period
    at the start of the recording, before note-on).
3.  Onset detection: first window whose RMS exceeds the noise floor by
    onset_snr_db (noise-floor-relative, immune to normalization artifacts).
4.  Peak detection: loudest RMS window from onset onward.
5.  Fade-out detection: binary subdivision, threshold = noise floor +
    fadeout_snr_db (noise-floor-relative, works for all velocity layers).
6.  Trim the original stereo recording to [onset, fadeout].
7.  Zero-start: cosine fade-in if first sample is not near zero.
8.  Zero-end: cosine fade-out if last sample is not near zero.
9.  Discard mono copy, return trimmed stereo (or None if silent).

Key design decisions
--------------------
* No normalization.  Normalization amplifies the noise floor by up to
  40 dB for quiet velocity layers, pushing it above the onset threshold
  and causing the detector to fire on noise.  Thresholds are always
  relative to the measured noise floor, so normalization is unnecessary.

* mono = mean(L, R), not sum.  L+R doubles coherent signal amplitude
  but also doubles the noise amplitude — the SNR is unchanged, but the
  absolute level is 6 dB higher, which affects the dBFS numbers used in
  log messages.  Averaging keeps per-channel semantics consistent.

* Noise floor from preroll.  The sampler records PREROLL (150 ms) of
  silence before sending note-on.  Measuring the RMS of the first
  preroll_ms (default 120 ms) of that silence gives a reliable noise
  floor for this specific recording, interface, and velocity layer.

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

from .peak_detector import estimate_noise_rms, find_fadeout, find_onset, find_peak

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_mono(data: np.ndarray) -> np.ndarray:
    """Average all channels to mono (works for both mono and stereo input)."""
    if data.ndim == 1:
        return data.astype(np.float32)
    return (data.sum(axis=1) / data.shape[1]).astype(np.float32)


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

    # Use a fixed fade length — amplitude-proportional scaling (round(max × A))
    # gives 1–2 samples for realistic end-of-note amplitudes (0.001–0.05),
    # which is indistinguishable from a hard cut.
    fade_n = max(1, min(max_fade_samples, len(data)))
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
) -> tuple[np.ndarray | None, dict]:
    """
    Process a single raw recorded sample through the full pipeline.

    Parameters
    ----------
    data : np.ndarray
        Raw stereo (or mono) float32 audio, shape (N,) or (N, channels).
    fs : int
        Sample rate in Hz.
    preroll_ms : float
        Duration of preroll (guaranteed silence) at the start of the
        recording in ms (default 120 ms — within the 150 ms PREROLL of
        sampler.py).  Used to estimate the noise floor.
    onset_snr_db : float
        Onset fires when RMS exceeds the noise floor by this many dB
        (default 6 dB).  Lower values detect quieter notes; higher
        values reject more noise.  For instruments with very low SNR
        (e.g. velocity layer 0 on bass notes) try 4–5 dB.
    onset_window_ms : float
        RMS window length for onset detection in ms (default 10 ms).
        Larger values are more reliable for low-frequency content.
    peak_window_ms : float
        RMS window length for peak detection in ms (default 10 ms).
    fadeout_snr_db : float
        Fade-out fires when RMS drops to within this many dB of the
        noise floor (default 6 dB ≈ 2× noise amplitude).
    fadeout_coarse_chunks : int
        Number of initial windows for binary subdivision (default 16).
    fadeout_min_window_ms : float
        Minimum window during binary subdivision in ms (default 100 ms).
        Prevents sub-period windows for bass frequencies (A0 ≈ 36 ms).
    tail_fade_ms : float
        Cosine fade duration in ms applied at the end of the recording
        when the note has not decayed to the noise floor within the
        recording window (default 500 ms).  This prevents a hard cut
        when the binary subdivision fallback is triggered.
    max_fade_samples : int
        Fixed cosine fade length in samples for zero-edge protection
        at start and end (default 200 ≈ 4 ms at 48 kHz).
    zero_threshold : float
        Amplitude below which a sample edge is considered "at zero"
        (default 0.001 ≈ –60 dBFS).

    Returns
    -------
    (np.ndarray or None, dict)
        Trimmed stereo float32 array (or None if the recording is silent)
        and a stats dict with key ``peak_rms_db`` (float, 0.0 when silent).
    """
    _empty: dict = {"peak_rms_db": 0.0}

    if len(data) == 0:
        return None, _empty

    # ------------------------------------------------------------------
    # Step 1: mono working copy — average of all channels, no normalisation
    # ------------------------------------------------------------------
    mono = _to_mono(data)

    if np.max(np.abs(mono)) == 0.0:
        log.info("  → záznam je tichý, přeskočeno")
        return None, _empty

    # ------------------------------------------------------------------
    # Step 2: noise floor from preroll
    # ------------------------------------------------------------------
    noise_rms = estimate_noise_rms(mono, fs, preroll_ms=preroll_ms)

    # ------------------------------------------------------------------
    # Step 3: onset detection (noise-floor-relative, after preroll only)
    #
    # Search starts from the END of the preroll, not from frame 0.
    # Searching from 0 risks triggering on WASAPI initialisation bursts
    # or VST idle noise in the first few milliseconds — a 10 ms window
    # there can exceed noise_floor + snr_db and set start_frame = 0,
    # leaving the full preroll + instrument attack delay in the output.
    # The preroll is guaranteed silence by design; onset can only happen
    # after note-on, which is sent at the end of the preroll period.
    # ------------------------------------------------------------------
    preroll_samples = min(int(preroll_ms / 1000.0 * fs), len(mono))
    onset_rel = find_onset(
        mono[preroll_samples:], fs,
        noise_rms=noise_rms,
        snr_db=onset_snr_db,
        window_ms=onset_window_ms,
    )
    start_frame = preroll_samples + onset_rel

    if start_frame >= len(mono):
        log.info("  → onset nenalezen (celý záznam pod prahem), přeskočeno")
        return None, _empty

    # ------------------------------------------------------------------
    # Step 4: peak detection (from onset onward)
    # ------------------------------------------------------------------
    peak_frame, _peak_rms = find_peak(
        mono, fs,
        start_frame=start_frame,
        window_ms=peak_window_ms,
    )

    # ------------------------------------------------------------------
    # Step 5: fade-out detection (noise-floor-relative)
    # ------------------------------------------------------------------
    end_frame, fadeout_fallback = find_fadeout(
        mono, fs,
        peak_frame=peak_frame,
        noise_rms=noise_rms,
        snr_db=fadeout_snr_db,
        coarse_chunks=fadeout_coarse_chunks,
        min_window_ms=fadeout_min_window_ms,
    )

    peak_rms_db = 20.0 * np.log10(float(_peak_rms) + 1e-10)
    stats: dict = {"peak_rms_db": peak_rms_db}

    if end_frame <= start_frame:
        log.info("  → end_frame ≤ start_frame, přeskočeno")
        return None, stats

    # ------------------------------------------------------------------
    # Step 6: trim original stereo recording
    # ------------------------------------------------------------------
    result = data[start_frame:end_frame].copy()

    # ------------------------------------------------------------------
    # Step 6b: tail fade when note didn't decay to noise floor
    #
    # The fallback is triggered when the binary subdivision never found a
    # window below the fadeout threshold — the signal was still sustaining
    # at the end of the recording.  A hard cut here produces a loud click.
    # Apply a cosine fade over the last tail_fade_ms milliseconds instead.
    # ------------------------------------------------------------------
    if fadeout_fallback and len(result) > 0:
        fade_n = min(len(result), max(1, int(tail_fade_ms / 1000.0 * fs)))
        _cosine_fade(result, fade_n, at_end=True)
        log.info(
            "  Tail fade   : %.0f ms  (nota neodezněla do prahu šumu + %.0f dB)",
            tail_fade_ms, fadeout_snr_db,
        )

    # ------------------------------------------------------------------
    # Step 7: zero-start — cosine fade-in if first sample is not near zero
    # ------------------------------------------------------------------
    result = _zero_edge(result, max_fade_samples, zero_threshold, at_end=False)

    # ------------------------------------------------------------------
    # Step 8: zero-end — cosine fade-out if last sample is not near zero
    # ------------------------------------------------------------------
    result = _zero_edge(result, max_fade_samples, zero_threshold, at_end=True)

    dur_ms = len(result) / fs * 1000.0
    log.info("  Délka       : %.1f ms", dur_ms)

    trimmed = result if len(result) > 0 else None
    return trimmed, stats
