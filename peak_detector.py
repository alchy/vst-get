"""
peak_detector.py — Onset, peak, and fade-out detection for audio samples
=========================================================================
Reusable standalone module. No dependencies beyond numpy.

All thresholds are relative to the noise floor estimated from the
recording's preroll — the guaranteed-silent period before the MIDI
note-on is sent.  This makes detection robust across all velocity
layers, including very quiet ones where peak-relative thresholds fail.

Concepts
--------
estimate_noise_rms
    Computes noise floor RMS from the first ``preroll_ms`` milliseconds
    of the recording (guaranteed silence — before note-on).

find_onset
    Finds the first window whose RMS exceeds the noise floor by
    ``snr_db`` decibels.  No normalization, no peak-relative maths.

find_peak
    Returns the window (and its RMS) with the highest energy from
    ``start_frame`` onward.

find_fadeout  (binary subdivision)
    Starting from the peak frame the remaining audio is divided into
    ``coarse_chunks`` initial windows.  Windows are halved each round
    until ``min_window_ms`` is reached.  The EARLIEST window whose RMS
    drops to within ``snr_db`` of the noise floor is selected.
    Fallback: window with minimum RMS when no window meets the condition.

Example
-------
    from peak_detector import estimate_noise_rms, find_onset, find_peak, find_fadeout

    mono = (stereo[:, 0] + stereo[:, 1]) / 2
    noise = estimate_noise_rms(mono, fs=48000)
    onset = find_onset(mono, fs=48000, noise_rms=noise)
    peak_frame, peak_rms = find_peak(mono, fs=48000, start_frame=onset)
    end = find_fadeout(mono, fs=48000, peak_frame=peak_frame, noise_rms=noise)
"""

import logging

import numpy as np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared primitive
# ---------------------------------------------------------------------------

def _rms_windows(segment: np.ndarray, hop: int) -> np.ndarray:
    """
    Return RMS for each non-overlapping *hop*-sized window.

    The last partial window (if any) is always included and its RMS is
    computed over its actual sample count — no zero-padding, no truncation.
    This ensures boundary signal is never silently discarded.
    """
    n = len(segment)
    if n == 0:
        return np.array([], dtype=np.float64)

    n_full = n // hop
    rms_list: list[float] = []

    if n_full > 0:
        full = segment[: n_full * hop].reshape(n_full, hop)
        rms_list.extend(np.sqrt(np.mean(full ** 2, axis=1)).tolist())

    remainder = n % hop
    if remainder > 0:
        rms_list.append(float(np.sqrt(np.mean(segment[n_full * hop:] ** 2))))

    return np.array(rms_list, dtype=np.float64)


# ---------------------------------------------------------------------------
# Noise floor estimation
# ---------------------------------------------------------------------------

def estimate_noise_rms(
    mono: np.ndarray,
    fs: int,
    preroll_ms: float = 120.0,
) -> float:
    """
    Estimate the noise floor RMS from the preroll (pre-note silence).

    The sampler records ``PREROLL`` seconds of silence before sending
    note-on.  Using that silence as a reference makes onset and fade-out
    thresholds noise-aware: a quiet velocity-0 note that is only 10 dB
    above the noise floor is still detected correctly, whereas a
    peak-relative threshold set to –42 dB relative to a normalised copy
    fails when the noise is amplified above that level.

    Parameters
    ----------
    mono : np.ndarray
        Mono float32 audio (full raw recording, not trimmed).
    fs : int
        Sample rate in Hz.
    preroll_ms : float
        Duration of the preroll to measure in ms (default 120 ms —
        safely within the 150 ms PREROLL constant of sampler.py).

    Returns
    -------
    float
        Noise floor RMS (always > 0).
    """
    preroll_samples = max(1, int(preroll_ms / 1000.0 * fs))
    segment = mono[:preroll_samples]
    if len(segment) == 0:
        return 1e-7

    rms = float(np.sqrt(np.mean(segment ** 2)))
    rms = max(rms, 1e-7)  # prevent log(0)

    rms_db = 20.0 * np.log10(rms)
    log.info(
        "  Šum podlahy : %.1f dBFS  (preroll %.0f ms, %d vzorků)",
        rms_db, preroll_ms, preroll_samples,
    )
    return rms


# ---------------------------------------------------------------------------
# Onset detection
# ---------------------------------------------------------------------------

def find_onset(
    mono: np.ndarray,
    fs: int,
    noise_rms: float,
    snr_db: float = 15.0,
    window_ms: float = 5.0,
) -> int:
    """
    Find the onset as the first window whose RMS exceeds the noise floor
    by *snr_db* decibels.

    Parameters
    ----------
    mono : np.ndarray
        Mono float32 audio.
    fs : int
        Sample rate in Hz.
    noise_rms : float
        Noise floor RMS from ``estimate_noise_rms()``.
    snr_db : float
        Minimum SNR above the noise floor to declare onset (default 15 dB).
        Lower values detect quieter notes; higher values are more selective.
    window_ms : float
        Analysis window length in ms (default 5 ms).

    Returns
    -------
    int
        Sample index of the onset frame.  Returns 0 if nothing exceeds
        the threshold (conservative: treat whole recording as signal).
    """
    hop = max(1, int(window_ms / 1000.0 * fs))
    rms_curve = _rms_windows(mono, hop)

    if len(rms_curve) == 0:
        return 0

    threshold = noise_rms * (10.0 ** (snr_db / 20.0))
    threshold_db = 20.0 * np.log10(threshold)

    above = np.where(rms_curve >= threshold)[0]

    if len(above) == 0:
        log.info(
            "  Onset       : práh nepřekročen (threshold=%.1f dBFS), start_frame=0",
            threshold_db,
        )
        return 0

    start_frame = int(above[0]) * hop
    t_ms = start_frame / fs * 1000.0
    log.info(
        "  Onset       : start_frame=%d  t=%.1f ms  "
        "(práh = šum + %.0f dB = %.1f dBFS)",
        start_frame, t_ms, snr_db, threshold_db,
    )
    return start_frame


# ---------------------------------------------------------------------------
# Peak detection
# ---------------------------------------------------------------------------

def find_peak(
    mono: np.ndarray,
    fs: int,
    start_frame: int = 0,
    window_ms: float = 10.0,
) -> tuple[int, float]:
    """
    Find the frame and RMS of the loudest window from *start_frame* onward.

    Parameters
    ----------
    mono : np.ndarray
        Mono float32 audio (full recording, not sliced).
    fs : int
        Sample rate in Hz.
    start_frame : int
        Search starts here (onset frame).
    window_ms : float
        Window length in ms (default 10 ms).

    Returns
    -------
    (peak_frame, peak_rms)
        peak_frame — absolute sample index of the window with max RMS.
        peak_rms   — RMS of that window (linear amplitude, not dB).
    """
    segment = mono[start_frame:]
    hop = max(1, int(window_ms / 1000.0 * fs))
    rms_curve = _rms_windows(segment, hop)

    if len(rms_curve) == 0:
        return start_frame, 0.0

    peak_win = int(np.argmax(rms_curve))
    peak_frame = start_frame + peak_win * hop
    peak_rms = float(rms_curve[peak_win])

    peak_db = 20.0 * np.log10(peak_rms + 1e-10)
    t_ms = peak_frame / fs * 1000.0
    log.info("  Peak        : frame=%d  t=%.1f ms  RMS=%.1f dBFS", peak_frame, t_ms, peak_db)

    return peak_frame, peak_rms


# ---------------------------------------------------------------------------
# Fade-out detection
# ---------------------------------------------------------------------------

def find_fadeout(
    mono: np.ndarray,
    fs: int,
    peak_frame: int,
    noise_rms: float,
    snr_db: float = 6.0,
    coarse_chunks: int = 16,
    min_window_ms: float = 100.0,
) -> int:
    """
    Locate the fade-out cut point using binary subdivision.

    The threshold is ``noise_rms × 10^(snr_db/20)``: the RMS level at
    which the signal is considered to have decayed to near the noise floor.
    Using the noise floor as a reference (instead of peak power) makes
    this robust for all velocity layers.

    The search region is ``mono[peak_frame:]``.  The initial window size
    is ``len(search_region) // coarse_chunks`` samples, adapting to the
    recording length.  Windows are halved each round until *min_window_ms*
    — preventing sub-period windows for bass frequencies (A0 period ≈ 36 ms).

    At every level the EARLIEST window below threshold is selected (first
    transition into the fade-out zone, not the quietest point).
    Fallback: window with minimum RMS if no window meets the condition.

    Parameters
    ----------
    mono : np.ndarray
        Mono float32 audio (full recording).
    fs : int
        Sample rate in Hz.
    peak_frame : int
        Start of the search region (output of ``find_peak``).
    noise_rms : float
        Noise floor RMS from ``estimate_noise_rms()``.
    snr_db : float
        Signal is considered faded when RMS drops to within *snr_db*
        above the noise floor (default 6 dB ≈ 2× noise).
    coarse_chunks : int
        Number of initial windows (default 16).
    min_window_ms : float
        Minimum window size in ms (default 100 ms).

    Returns
    -------
    (end_frame, fallback_used)
        end_frame    — sample index of the fade-out transition.
        fallback_used — True when the signal never dropped below the threshold
                       within the recording; caller should apply a long tail fade.
    """
    threshold_rms = noise_rms * (10.0 ** (snr_db / 20.0))
    threshold_db = 20.0 * np.log10(threshold_rms + 1e-10)

    segment = mono[peak_frame:]
    if len(segment) == 0:
        log.info("    → prázdný úsek po peaku, end_frame=peak_frame=%d", peak_frame)
        return peak_frame, False

    min_hop = max(1, int(min_window_ms / 1000.0 * fs))
    initial_hop = max(min_hop, len(segment) // coarse_chunks)
    initial_ms = initial_hop / fs * 1000.0

    log.info(
        "  Fade-out    : práh = šum + %.0f dB = %.1f dBFS  "
        "počáteční okno=%.1f ms (%d vzorků = 1/%d od peaku)  min okno=%.0f ms",
        snr_db, threshold_db, initial_ms, initial_hop, coarse_chunks, min_window_ms,
    )

    seg_start = 0
    seg_end = len(segment)
    hop = initial_hop
    round_num = 0
    fallback_used = False

    while hop >= min_hop:
        round_num += 1
        win_ms = hop / fs * 1000.0
        seg = segment[seg_start:seg_end]
        rms_curve = _rms_windows(seg, hop)
        n_win = len(rms_curve)

        if n_win == 0:
            break

        candidates = np.where(rms_curve <= threshold_rms)[0]

        if len(candidates) == 0:
            # No window met the threshold.  Take the LAST window (= end of
            # segment) rather than the minimum-RMS window.  The minimum-RMS
            # approach converges via binary subdivision to a zero-crossing of
            # an oscillating waveform, producing a hard cut at full amplitude.
            # Returning the end of the segment lets the caller apply an
            # explicit tail fade instead.
            best_win = n_win - 1
            best_db = 20.0 * np.log10(float(rms_curve[best_win]) + 1e-10)
            if round_num == 1:
                fallback_used = True
            log.info(
                "    kolo %d  [%6.2f ms / %d vzorků]  %2d oken  "
                "práh nesplněn → konec záznamu (okno č.%2d  %.1f dBFS)  %s",
                round_num, win_ms, hop, n_win, best_win + 1, best_db,
                "(fallback)" if fallback_used else "",
            )
        else:
            best_win = int(candidates[0])
            best_db = 20.0 * np.log10(float(rms_curve[best_win]) + 1e-10)
            log.info(
                "    kolo %d  [%6.2f ms / %d vzorků]  %2d oken  "
                "nejdřívější okno pod prahem č.%2d  %.1f dBFS",
                round_num, win_ms, hop, n_win, best_win + 1, best_db,
            )

        new_start = seg_start + best_win * hop
        new_end = min(new_start + hop, len(segment))
        seg_start = new_start
        seg_end = new_end
        hop = hop // 2

    end_frame = peak_frame + seg_start
    t_ms = end_frame / fs * 1000.0
    suffix = "  (fallback: tail fade needed)" if fallback_used else ""
    log.info("    → end_frame=%d  t=%.1f ms%s", end_frame, t_ms, suffix)

    return end_frame, fallback_used
