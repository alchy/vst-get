"""
audio_trim.py — Silence trimmer for instrument samples
=======================================================
Reusable module. Import SilenceTrimmer and call .trim().
"""

import numpy as np


class SilenceTrimmer:
    """
    Trims silence from the start and end of an audio recording.

    Both start and end use RMS threshold relative to the recording peak,
    evaluated in fine-grained 1 ms windows. This reliably removes preroll
    silence and decay tails without depending on slope heuristics.

    Parameters
    ----------
    threshold_db : float
        Trim threshold in dB relative to the recording peak (e.g. -50.0).
    window_ms : float
        RMS window length in milliseconds (default 1 ms).
    """

    def __init__(
        self,
        threshold_db: float = -50.0,
        window_ms: float = 1.0,
    ):
        self.threshold_db = threshold_db
        self.window_ms = window_ms

    def trim(
        self,
        data: np.ndarray,
        fs: int,
    ) -> tuple[np.ndarray, int, int]:
        """
        Trim silence from start and end.

        Parameters
        ----------
        data : np.ndarray
            Audio as float32 array, shape (N,) or (N, channels).
        fs : int
            Sample rate in Hz.

        Returns
        -------
        (trimmed_data, start_sample, end_sample)
        Returns (empty_array, 0, 0) when the whole recording is silent.
        """
        if len(data) == 0:
            return np.array([]), 0, 0

        mono = np.mean(data, axis=1) if data.ndim > 1 else data.copy()

        max_abs = np.max(np.abs(mono))
        if max_abs == 0:
            return np.array([]), 0, 0

        hop = max(1, int(self.window_ms / 1000.0 * fs))
        n_win = len(mono) // hop
        if n_win == 0:
            return np.array([]), 0, 0

        rms = np.sqrt(np.mean(mono[: n_win * hop].reshape(-1, hop) ** 2, axis=1))
        rms_db = 20 * np.log10(rms / max_abs + 1e-10)
        active = np.where(rms_db >= self.threshold_db)[0]

        if len(active) == 0:
            return np.array([]), 0, 0

        start_idx = int(active[0]) * hop
        end_idx = min(int(active[-1] + 1) * hop, len(data))

        return data[start_idx:end_idx], start_idx, end_idx
