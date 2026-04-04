"""
wasapi_recorder.py — WASAPI loopback audio recorder
====================================================
Reusable module. Requires pyaudiowpatch (Windows only).

Example
-------
    import pyaudiowpatch as pyaudio
    from wasapi_recorder import Recorder, list_loopback_devices, select_loopback_device

    p = pyaudio.PyAudio()
    dev_idx, sample_rate, channels = select_loopback_device(p)
    rec = Recorder(p, dev_idx, sample_rate, channels)
    rec.start(duration=5.0)
    data = rec.get()   # float32 numpy array
    p.terminate()
"""

import sys
import threading

import numpy as np
import pyaudiowpatch as pyaudio


def list_loopback_devices(p: pyaudio.PyAudio) -> list[tuple[int, dict]]:
    """Return list of (device_index, device_info) for all WASAPI loopback devices."""
    return [
        (i, p.get_device_info_by_index(i))
        for i in range(p.get_device_count())
        if p.get_device_info_by_index(i).get("isLoopbackDevice", False)
    ]


def select_loopback_device(
    p: pyaudio.PyAudio,
    max_channels: int = 2,
) -> tuple[int, int, int]:
    """
    Interactive CLI selection of a WASAPI loopback device.

    Parameters
    ----------
    p : pyaudio.PyAudio
    max_channels : int
        Cap the returned channel count (default 2 = stereo).

    Returns
    -------
    (device_index, sample_rate, channels)
    """
    loopbacks = list_loopback_devices(p)
    if not loopbacks:
        print(
            "Nebyla nalezena žádná WASAPI loopback zařízení.\n"
            "Ujistěte se, že máte nainstalován pyaudiowpatch a používáte Windows 10+.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\n=== Dostupná WASAPI loopback zařízení ===")
    for i, (idx, info) in enumerate(loopbacks):
        rate = int(info["defaultSampleRate"])
        ch = int(info["maxInputChannels"])
        print(f"  [{i}]  {info['name']:<50}  dev#{idx}  {rate} Hz  {ch} ch")

    sel = input(f"\nVyberte zařízení [0–{len(loopbacks) - 1}] (Enter = 0): ").strip()
    choice = int(sel) if sel.isdigit() and int(sel) < len(loopbacks) else 0

    dev_idx, dev_info = loopbacks[choice]
    dev_rate = int(dev_info["defaultSampleRate"])
    dev_ch = min(int(dev_info["maxInputChannels"]), max_channels)

    print(f"\nZvoleno: {dev_info['name']}")
    print(f"Parametry zachycení: {dev_rate} Hz, {dev_ch} ch")
    return dev_idx, dev_rate, dev_ch


class Recorder:
    """
    Captures audio from a WASAPI loopback device into a float32 numpy array.

    Parameters
    ----------
    p : pyaudio.PyAudio
    device_index : int
    sample_rate : int
    channels : int

    Usage
    -----
        rec = Recorder(p, device_index, sample_rate, channels)
        rec.start(duration=30.0)   # non-blocking; stream opens before returning
        # ... do other work (e.g. send MIDI) ...
        data = rec.get()           # float32, shape (N,) or (N, channels)
    """

    def __init__(
        self,
        p: pyaudio.PyAudio,
        device_index: int,
        sample_rate: int,
        channels: int,
    ):
        self._p = p
        self._device_index = device_index
        self._sample_rate = sample_rate
        self._channels = channels
        self._frames: list[bytes] = []
        self._thread: threading.Thread | None = None
        self._stream_ready = threading.Event()

    def _record(self, duration: float) -> None:
        chunk = 1024
        total_frames = int(self._sample_rate * duration)

        stream = self._p.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._sample_rate,
            input=True,
            input_device_index=self._device_index,
            frames_per_buffer=chunk,
        )
        self._stream_ready.set()

        recorded = 0
        while recorded < total_frames:
            to_read = min(chunk, total_frames - recorded)
            data = stream.read(to_read, exception_on_overflow=False)
            self._frames.append(data)
            recorded += to_read

        stream.stop_stream()
        stream.close()

    def start(self, duration: float) -> None:
        """Start recording asynchronously; returns only after the stream is open."""
        self._frames = []
        self._stream_ready.clear()
        self._thread = threading.Thread(
            target=self._record, args=(duration,), daemon=True
        )
        self._thread.start()
        if not self._stream_ready.wait(timeout=10):
            raise RuntimeError("WASAPI loopback stream did not open within 10 s")

    def get(self, join_timeout: float = 300.0) -> np.ndarray:
        """
        Wait for the recording to finish and return a float32 numpy array.

        Shape is (N,) for mono or (N, channels) for multi-channel.
        Values are in [-1.0, +1.0].
        """
        if self._thread:
            self._thread.join(timeout=join_timeout)
        raw = b"".join(self._frames)
        data = np.frombuffer(raw, dtype=np.int16)
        if self._channels > 1:
            data = data.reshape(-1, self._channels)
        return data.astype(np.float32) / 32767.0
