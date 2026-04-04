"""
wav_io.py — WAV file I/O helpers
==================================
Reusable module. Requires numpy.

Example
-------
    import numpy as np
    from wav_io import save_wav

    data = np.zeros((48000, 2), dtype=np.float32)   # 1 s stereo silence
    save_wav(data, "out.wav", sample_rate=48000, channels=2)
"""

import wave
from pathlib import Path

import numpy as np


def save_wav(
    data: np.ndarray,
    path: Path | str,
    sample_rate: int,
    channels: int,
) -> None:
    """
    Save a float32 numpy array as a 16-bit PCM WAV file.

    Parameters
    ----------
    data : np.ndarray
        Audio data as float32, values in [-1.0, +1.0].
        Shape (N,) for mono or (N, channels) for multi-channel.
    path : Path or str
        Output file path.
    sample_rate : int
        Sample rate in Hz.
    channels : int
        Number of channels.
    """
    data_int = (data * 32767.0).clip(-32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)   # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(data_int.tobytes())
