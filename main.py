#!/usr/bin/env python3
"""
vstget - VST Instrument Sampler
================================
Samples all piano-range MIDI notes across 8 velocity layers.

MIDI output : loopMIDI port  (Tobias Erichsen loopMIDI)
Audio input : WASAPI loopback (pyaudiowpatch)
Output      : stereo interleaved 16-bit PCM WAV, 48 kHz
Filename    : m<NNN>-vel<V>-f<SR>.wav
              NNN = MIDI note 000-127
              V   = velocity layer 0-7
              SR  = sample rate kHz (48 or 44)

Usage:
    python main.py --output-dir samples_out [options]
"""

import argparse
import wave
import time
import threading
import sys
import numpy as np
from pathlib import Path
import logging

import mido
import pyaudiowpatch as pyaudio

from audio_trim import SilenceTrimmer

# Force UTF-8 output on Windows consoles
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PIANO_LOW = 21       # A0
PIANO_HIGH = 108     # C8
NOTE_HOLD = 29.0     # seconds note-on is held
NOTE_RELEASE = 1.0   # seconds after note-off (decay / silence tail)
TOTAL_DURATION = NOTE_HOLD + NOTE_RELEASE
PREROLL = 0.15       # seconds of capture before note-on (ensures attack is not clipped)
CHANNELS = 2
DEFAULT_MIDI_PORT = "loopMIDI port"
DEFAULT_THRESHOLD_DB = -50.0

# 8 velocity layers: evenly spaced so that layer 0 is the softest non-zero
# signal and layer 7 is full velocity 127
# Formula: round(127/8 * (i+1))  for i in 0..7
VELOCITY_LAYERS = [round(127 / 8 * (i + 1)) for i in range(8)]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# WAV I/O
# ---------------------------------------------------------------------------

def save_wav(data: np.ndarray, path: Path, sample_rate: int, channels: int) -> None:
    """Save float32 numpy array as 16-bit stereo PCM WAV."""
    data_int = (data * 32767.0).clip(-32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)   # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(data_int.tobytes())


# ---------------------------------------------------------------------------
# Audio recorder  (WASAPI loopback via pyaudiowpatch)
# ---------------------------------------------------------------------------

class Recorder:
    """Captures audio from a WASAPI loopback device into a numpy array."""

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

    # --- internal thread ---

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
        self._stream_ready.set()   # unblock start()

        recorded = 0
        while recorded < total_frames:
            to_read = min(chunk, total_frames - recorded)
            data = stream.read(to_read, exception_on_overflow=False)
            self._frames.append(data)
            recorded += to_read

        stream.stop_stream()
        stream.close()

    # --- public API ---

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

    def get(self) -> np.ndarray:
        """Wait for recording to finish and return float32 numpy array."""
        if self._thread:
            self._thread.join(timeout=TOTAL_DURATION + 15)
        raw = b"".join(self._frames)
        data = np.frombuffer(raw, dtype=np.int16)
        if self._channels > 1:
            data = data.reshape(-1, self._channels)
        return data.astype(np.float32) / 32767.0


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------

def list_loopback_devices(p: pyaudio.PyAudio) -> list[tuple[int, dict]]:
    devices = []
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get("isLoopbackDevice", False):
            devices.append((i, info))
    return devices


def select_loopback_device(p: pyaudio.PyAudio) -> tuple[int, int, int]:
    """
    Interactive device selection.
    Returns (device_index, sample_rate, channels).
    """
    loopbacks = list_loopback_devices(p)
    if not loopbacks:
        log.error(
            "Nebyla nalezena žádná WASAPI loopback zařízení.\n"
            "Ujistěte se, že máte nainstalován pyaudiowpatch a používáte Windows 10+."
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
    dev_ch = min(int(dev_info["maxInputChannels"]), CHANNELS)

    print(f"\nZvoleno: {dev_info['name']}")
    print(f"Parametry zachycení: {dev_rate} Hz, {dev_ch} ch")
    return dev_idx, dev_rate, dev_ch


# ---------------------------------------------------------------------------
# MIDI helper
# ---------------------------------------------------------------------------

def open_midi_port(port_name: str) -> mido.ports.BaseOutput:
    available = mido.get_output_names()
    match = next((p for p in available if port_name.lower() in p.lower()), None)
    if match is None:
        log.error(f"MIDI port '{port_name}' nebyl nalezen.")
        print("Dostupné MIDI porty:", available)
        sys.exit(1)
    log.info(f"MIDI port: {match}")
    return mido.open_output(match)


# ---------------------------------------------------------------------------
# Main sampling loop
# ---------------------------------------------------------------------------

NORMALIZE_TARGET = 10 ** (-1.0 / 20)  # -1 dBFS


def record_one(
    recorder: Recorder,
    midi_out: mido.ports.BaseOutput,
    note: int,
    velocity: int,
    midi_channel: int,
    sample_rate: int,
    threshold_db: float,
) -> np.ndarray | None:
    """Record a single note+velocity. Returns trimmed float32 array or None if silent."""
    recorder.start(TOTAL_DURATION + PREROLL)
    time.sleep(PREROLL)

    midi_out.send(mido.Message("note_on", note=note, velocity=velocity, channel=midi_channel))
    time.sleep(NOTE_HOLD)
    midi_out.send(mido.Message("note_off", note=note, velocity=0, channel=midi_channel))
    time.sleep(NOTE_RELEASE)

    data = recorder.get()

    if data.ndim == 1:
        data -= np.mean(data)
    else:
        data -= np.mean(data, axis=0)

    trimmed, _, _ = SilenceTrimmer(threshold_db=threshold_db).trim(data, sample_rate)
    return trimmed if len(trimmed) > 0 else None


def sample_all(
    recorder: Recorder,
    midi_out: mido.ports.BaseOutput,
    output_dir: Path,
    sample_rate: int,
    channels: int,
    threshold_db: float,
    note_start: int,
    note_end: int,
    midi_channel: int,
    normalize: bool = True,
) -> None:
    freq_tag = f"f{sample_rate // 1000}"
    total = (note_end - note_start + 1) * len(VELOCITY_LAYERS)
    n = 0
    saved = 0
    skipped = 0

    log.info(f"Rozsah not: {note_start}–{note_end}  ×  {len(VELOCITY_LAYERS)} velocity vrstev  =  {total} vzorků")
    log.info(f"Odhadovaný čas: {total * TOTAL_DURATION / 60:.0f} min")
    log.info(f"Velocity vrstvy: {VELOCITY_LAYERS}")
    if normalize:
        log.info("Normalizace: zapnuta (per-nota, zachování dynamiky mezi vrstvami)")

    for note in range(note_start, note_end + 1):
        # --- Record all velocity layers for this note ---
        note_recordings: list[tuple[int, int, np.ndarray | None]] = []

        for vel_layer, velocity in enumerate(VELOCITY_LAYERS):
            n += 1
            print(
                f"[{n:>4}/{total}]  nota={note:>3}  vrstva={vel_layer}  vel={velocity:>3}",
                end="",
                flush=True,
            )
            trimmed = record_one(
                recorder, midi_out, note, velocity, midi_channel, sample_rate, threshold_db
            )
            if trimmed is None:
                print("  →  ticho, přeskočeno")
                skipped += 1
                note_recordings.append((vel_layer, velocity, None))
            else:
                dur = len(trimmed) / sample_rate
                peak_db = 20 * np.log10(np.max(np.abs(trimmed)) + 1e-10)
                print(f"  →  {dur:.2f}s  peak={peak_db:.1f}dB")
                note_recordings.append((vel_layer, velocity, trimmed))

        # --- Per-note normalization: same gain across all velocity layers ---
        if normalize:
            valid = [t for _, _, t in note_recordings if t is not None]
            if valid:
                global_peak = max(np.max(np.abs(t)) for t in valid)
                gain = NORMALIZE_TARGET / global_peak if global_peak > 0 else 1.0
            else:
                gain = 1.0
        else:
            gain = 1.0

        # --- Save ---
        for vel_layer, velocity, trimmed in note_recordings:
            if trimmed is None:
                continue
            filename = f"m{note:03d}-vel{vel_layer}-{freq_tag}.wav"
            out_path = output_dir / filename
            save_wav(trimmed * gain, out_path, sample_rate, channels)
            saved += 1

    print()
    log.info(f"Hotovo. Uloženo: {saved}  Přeskočeno: {skipped}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="vstget – sampluje VST nástroj přes loopMIDI + WASAPI loopback"
    )
    parser.add_argument("--output-dir", required=True, help="Výstupní adresář pro WAV soubory")
    parser.add_argument(
        "--threshold-db",
        type=float,
        default=DEFAULT_THRESHOLD_DB,
        help=f"Práh ticha pro ořez v dB (výchozí: {DEFAULT_THRESHOLD_DB})",
    )
    parser.add_argument(
        "--note-start",
        type=int,
        default=PIANO_LOW,
        help=f"První MIDI nota (výchozí: {PIANO_LOW} = A0)",
    )
    parser.add_argument(
        "--note-end",
        type=int,
        default=PIANO_HIGH,
        help=f"Poslední MIDI nota (výchozí: {PIANO_HIGH} = C8)",
    )
    parser.add_argument(
        "--midi-port",
        default=DEFAULT_MIDI_PORT,
        help=f"Název MIDI výstupního portu (výchozí: '{DEFAULT_MIDI_PORT}')",
    )
    parser.add_argument(
        "--midi-channel",
        type=int,
        default=0,
        help="MIDI kanál 0–15 (výchozí: 0)",
    )
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="Vypne per-nota normalizaci (výchozí: normalizace zapnuta)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- PyAudio / WASAPI loopback ---
    p = pyaudio.PyAudio()
    try:
        dev_idx, dev_rate, dev_ch = select_loopback_device(p)

        # --- MIDI ---
        midi_out = open_midi_port(args.midi_port)

        print(f"\nVýstupní adresář: {output_dir.resolve()}")
        input("Ujistěte se, že VST je spuštěno a routováno na zvolené výstupní zařízení.\nStiskněte Enter pro zahájení...\n")

        recorder = Recorder(p, dev_idx, dev_rate, dev_ch)

        try:
            sample_all(
                recorder=recorder,
                midi_out=midi_out,
                output_dir=output_dir,
                sample_rate=dev_rate,
                channels=dev_ch,
                threshold_db=args.threshold_db,
                note_start=args.note_start,
                note_end=args.note_end,
                midi_channel=args.midi_channel,
                normalize=not args.no_normalize,
            )
        except KeyboardInterrupt:
            print("\n\nPřerušeno uživatelem.")
        finally:
            midi_out.close()
    finally:
        p.terminate()

    return 0


if __name__ == "__main__":
    sys.exit(main())
