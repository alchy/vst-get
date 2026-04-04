"""
peak_detector.py — Onset, peak, and fade-out detection for audio samples
=========================================================================
Reusable standalone module. No dependencies beyond numpy.

All three functions share a single internal primitive — ``_window_power`` —
which computes average power (E/n_samples = RMS²) per window and always
includes the last partial window at segment boundaries.

Concepts
--------
find_onset
    Finds the first window whose RMS exceeds a threshold expressed in dB
    relative to the recording peak.  Uses the same 1 ms windowing as
    find_peak so results are directly comparable.

find_peak
    Returns the window with the highest RMS.  Built on ``_window_power``
    so the last partial window is never silently discarded.

find_fadeout (binary subdivision)
    Starting from the peak frame the remaining audio is divided into
    ``coarse_chunks`` equally-sized initial windows (default 16), adapting
    the window size to the actual recording length.  Windows are halved each
    round until a single sample is reached.

    At every level the algorithm picks the **earliest** window whose average
    power satisfies ``power ≤ peak_rms² × fadeout_ratio`` — i.e. the first
    transition into the fade-out zone, not the quietest moment within it.

    If no window satisfies the condition at the coarsest level the window
    with the overall minimum average power is used as a fallback.

Example
-------
    import numpy as np
    from peak_detector import find_onset, find_peak, find_fadeout

    onset = find_onset(mono, fs=48000, threshold_db=-42.0)
    peak_frame, peak_rms = find_peak(mono[onset:], fs=48000)
    peak_frame += onset                        # make absolute
    end_frame = find_fadeout(mono, fs=48000, peak_frame=peak_frame,
                             peak_rms=peak_rms, fadeout_ratio=0.1)
"""

import logging

import numpy as np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared primitive
# ---------------------------------------------------------------------------

def _window_power(segment: np.ndarray, hop: int) -> np.ndarray:
    """
    Return average power (E/n_samples = RMS²) for each non-overlapping window.

    The last window is included even when shorter than *hop* — its power is
    computed over its actual sample count so it is directly comparable with
    full windows.  This ensures signal at segment boundaries is never
    silently discarded.
    """
    n = len(segment)
    if n == 0:
        return np.array([], dtype=np.float64)

    n_full = n // hop
    powers: list[float] = []

    if n_full > 0:
        full = segment[: n_full * hop].reshape(n_full, hop)
        powers.extend(np.mean(full ** 2, axis=1).tolist())

    remainder = n % hop
    if remainder > 0:
        powers.append(float(np.mean(segment[n_full * hop:] ** 2)))

    return np.array(powers, dtype=np.float64)


# ---------------------------------------------------------------------------
# Onset detection
# ---------------------------------------------------------------------------

def find_onset(
    mono: np.ndarray,
    fs: int,
    threshold_db: float = -42.0,
    window_ms: float = 1.0,
) -> int:
    """
    Find the start of the signal as the first window whose RMS exceeds
    *threshold_db* relative to the recording peak.

    Parameters
    ----------
    mono : np.ndarray
        Mono float32 audio, shape (N,).
    fs : int
        Sample rate in Hz.
    threshold_db : float
        Onset threshold in dB relative to the peak RMS of the whole
        recording (default –42 dB).
    window_ms : float
        Analysis window length in milliseconds (default 1 ms).

    Returns
    -------
    int
        Sample index of the onset frame.  Returns 0 if the signal never
        exceeds the threshold (treat the whole recording as signal).
    """
    hop = max(1, int(window_ms / 1000.0 * fs))
    powers = _window_power(mono, hop)

    if len(powers) == 0:
        return 0

    peak_power = float(np.max(powers))
    if peak_power == 0.0:
        return 0

    # Convert dB threshold (relative to peak) to absolute power threshold
    threshold_power = peak_power * (10.0 ** (threshold_db / 10.0))

    above = np.where(powers >= threshold_power)[0]
    if len(above) == 0:
        return 0

    start_frame = int(above[0]) * hop
    t_ms = start_frame / fs * 1000.0
    log.info(
        "  Onset       : start_frame=%d  t=%.1f ms  (práh=%.0f dB rel. k peaku)",
        start_frame, t_ms, threshold_db,
    )
    return start_frame


# ---------------------------------------------------------------------------
# Peak detection
# ---------------------------------------------------------------------------

def find_peak(
    mono: np.ndarray,
    fs: int,
    window_ms: float = 1.0,
) -> tuple[int, float]:
    """
    Find the frame index and RMS of the loudest window in *mono*.

    Uses ``_window_power`` so the last partial window is always included.

    Parameters
    ----------
    mono : np.ndarray
        Mono float32 audio, shape (N,).
    fs : int
        Sample rate in Hz.
    window_ms : float
        Window length in milliseconds (default 1 ms).

    Returns
    -------
    (peak_frame, peak_rms)
        peak_frame — sample index of the window start with maximum RMS.
        peak_rms   — RMS of that window (linear amplitude, not dB).
    """
    hop = max(1, int(window_ms / 1000.0 * fs))
    powers = _window_power(mono, hop)

    if len(powers) == 0:
        return 0, 0.0

    peak_win = int(np.argmax(powers))
    peak_frame = peak_win * hop
    peak_rms = float(np.sqrt(powers[peak_win]))

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
    peak_rms: float,
    fadeout_ratio: float = 0.1,
    coarse_chunks: int = 16,
) -> int:
    """
    Locate the fade-out cut point using binary subdivision of average power.

    The search region is ``mono[peak_frame:]``.  The initial window size is
    ``len(search_region) // coarse_chunks`` samples so it scales with the
    actual recording length.  Windows are halved each round until a single
    sample is reached.

    At every level the algorithm selects the **earliest** window whose average
    power satisfies ``power ≤ peak_rms² × fadeout_ratio``.  This converges to
    the first moment where the signal transitions below the fade-out threshold,
    not the quietest point within the silence.

    Incomplete windows at segment boundaries are included and evaluated over
    their actual sample count (no padding, no truncation).

    If no window satisfies the condition at the coarsest level the window with
    the overall minimum average power is used as a fallback.

    Parameters
    ----------
    mono : np.ndarray
        Mono float32 audio, shape (N,).
    fs : int
        Sample rate in Hz.
    peak_frame : int
        Start of the peak window (output of ``find_peak``).
    peak_rms : float
        RMS of the peak window (output of ``find_peak``).
    fadeout_ratio : float
        Fade-out threshold as a fraction of peak power (default 0.1 = 1/10).
    coarse_chunks : int
        Number of initial windows the search region is divided into
        (default 16).  Larger values → finer initial resolution.

    Returns
    -------
    int
        Sample index of the fade-out transition — use as the exclusive end
        of the trimmed recording.
    """
    peak_power = peak_rms ** 2
    threshold_power = peak_power * fadeout_ratio
    threshold_db = 10.0 * np.log10(threshold_power + 1e-30)

    segment = mono[peak_frame:]
    if len(segment) == 0:
        log.info("    → prázdný úsek po peaku, end_frame=peak_frame=%d", peak_frame)
        return peak_frame

    initial_hop = max(1, len(segment) // coarse_chunks)
    initial_ms = initial_hop / fs * 1000.0

    log.info(
        "  Fade-out    : ratio=%.2f  P_threshold=%.2e (%.1f dB)  "
        "počáteční okno=%.1f ms (%d vzorků = 1/%d od peaku do konce)",
        fadeout_ratio, threshold_power, threshold_db,
        initial_ms, initial_hop, coarse_chunks,
    )

    seg_start = 0
    seg_end = len(segment)
    hop = initial_hop
    round_num = 0
    fallback_used = False

    while hop >= 1:
        round_num += 1
        win_ms = hop / fs * 1000.0
        seg = segment[seg_start:seg_end]
        powers = _window_power(seg, hop)
        n_win = len(powers)

        if n_win == 0:
            break

        candidates = np.where(powers <= threshold_power)[0]

        if len(candidates) == 0:
            # Fallback: no window meets condition — use window with min power
            best_win = int(np.argmin(powers))
            best_db = 10.0 * np.log10(float(powers[best_win]) + 1e-30)
            if round_num == 1:
                fallback_used = True
                log.info(
                    "    kolo %d  [%6.2f ms / %d vzorků]  %2d oken  "
                    "podmínka nesplněna → fallback min-power okno č.%2d  P=%.1f dB",
                    round_num, win_ms, hop, n_win, best_win + 1, best_db,
                )
            else:
                log.info(
                    "    kolo %d  [%6.2f ms / %d vzorků]  %2d oken  "
                    "min-power okno č.%2d  P=%.1f dB  (fallback)",
                    round_num, win_ms, hop, n_win, best_win + 1, best_db,
                )
        else:
            # Take the EARLIEST window below threshold — this is the
            # transition point into the fade-out zone, not the quietest moment.
            best_win = int(candidates[0])
            best_db = 10.0 * np.log10(float(powers[best_win]) + 1e-30)
            log.info(
                "    kolo %d  [%6.2f ms / %d vzorků]  %2d oken  "
                "nejdřívější okno pod prahem č.%2d  P=%.1f dB",
                round_num, win_ms, hop, n_win, best_win + 1, best_db,
            )

        new_start = seg_start + best_win * hop
        new_end = min(new_start + hop, len(segment))
        seg_start = new_start
        seg_end = new_end
        hop = hop // 2

    end_frame = peak_frame + seg_start
    t_ms = end_frame / fs * 1000.0
    suffix = "  (fallback: min-power)" if fallback_used else ""
    log.info("    → end_frame=%d  t=%.1f ms%s", end_frame, t_ms, suffix)

    return end_frame
