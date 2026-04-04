#!/usr/bin/env python3
"""
vstget — VST Instrument Sampler
================================
Samples all piano-range MIDI notes across 8 velocity layers.

MIDI output : loopMIDI port  (Tobias Erichsen loopMIDI)
Audio input : WASAPI loopback (pyaudiowpatch)
Output      : stereo interleaved 16-bit PCM WAV
Filename    : m<NNN>-vel<V>-f<SR>.wav
              NNN = MIDI note 000-127
              V   = velocity layer 0-7
              SR  = sample rate kHz (48 or 44)

Usage:
    python vst-get.py --output-dir samples_out [options]
"""

import argparse
import logging
import sys
from pathlib import Path

import pyaudiowpatch as pyaudio

from midi_utils import open_midi_port
from sampler import PIANO_HIGH, PIANO_LOW, sample_all
from wasapi_recorder import Recorder, select_loopback_device

# Force UTF-8 output on Windows consoles
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

DEFAULT_MIDI_PORT = "loopMIDI port"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="vstget – sampluje VST nástroj přes loopMIDI + WASAPI loopback",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # --- I/O ---
    parser.add_argument(
        "--output-dir", required=True,
        help="Výstupní adresář pro WAV soubory",
    )

    # --- MIDI ---
    parser.add_argument(
        "--midi-port", default=DEFAULT_MIDI_PORT,
        help="Název MIDI výstupního portu",
    )
    parser.add_argument(
        "--midi-channel", type=int, default=0,
        help="MIDI kanál 0–15",
    )

    # --- Rozsah not ---
    parser.add_argument(
        "--note-start", type=int, default=PIANO_LOW,
        help="První MIDI nota (A0)",
    )
    parser.add_argument(
        "--note-end", type=int, default=PIANO_HIGH,
        help="Poslední MIDI nota (C8)",
    )

    # --- Onset detection ---
    parser.add_argument(
        "--threshold-db", type=float, default=-42.0,
        help="Onset práh v dB relativně k normalizovanému peaku mono kopie",
    )

    # --- Fade-out detection ---
    parser.add_argument(
        "--fadeout-ratio", type=float, default=0.1,
        help="Fade-out: okno s E ≤ peak_energy × ratio je cut-out bod",
    )
    parser.add_argument(
        "--fadeout-coarse-chunks", type=int, default=16,
        help="Počet počátečních oken binary subdivision (výchozí 16 = 1/16 délky od peaku)",
    )

    # --- Zero-start ochrana ---
    parser.add_argument(
        "--max-fade-in", type=int, default=30,
        help="Max. délka cosine fade-in na začátku samplu (vzorky, škáluje s amplitudou)",
    )
    parser.add_argument(
        "--zero-threshold", type=float, default=0.001,
        help="Amplituda pod kterou je start považován za nulu (výchozí 0.001 ≈ –60 dBFS)",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    p = pyaudio.PyAudio()
    try:
        dev_idx, dev_rate, dev_ch = select_loopback_device(p)
        midi_out = open_midi_port(args.midi_port)

        print(f"\nVýstupní adresář: {output_dir.resolve()}")
        input(
            "Ujistěte se, že VST je spuštěno a routováno na zvolené výstupní zařízení.\n"
            "Stiskněte Enter pro zahájení...\n"
        )

        recorder = Recorder(p, dev_idx, dev_rate, dev_ch)

        try:
            sample_all(
                recorder=recorder,
                midi_out=midi_out,
                output_dir=output_dir,
                sample_rate=dev_rate,
                channels=dev_ch,
                note_start=args.note_start,
                note_end=args.note_end,
                midi_channel=args.midi_channel,
                threshold_db=args.threshold_db,
                fadeout_ratio=args.fadeout_ratio,
                fadeout_coarse_chunks=args.fadeout_coarse_chunks,
                max_fade_in=args.max_fade_in,
                zero_threshold=args.zero_threshold,
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
