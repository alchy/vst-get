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

from vstget.midi_utils import open_midi_port
from vstget.sampler import PIANO_HIGH, PIANO_LOW, sample_all
from vstget.wasapi_recorder import Recorder, select_loopback_device

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
    parser.add_argument(
        "--do-not-prompt", action="store_true",
        help="Přeskočit potvrzení Enterem — použij pokud je VST již spuštěno",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Podrobný výpis pipeline pro každý vzorek (výchozí: kompaktní řádek)",
    )
    parser.add_argument(
        "--audio-device", type=int, default=None,
        help="Index WASAPI loopback zařízení (přeskočí interaktivní výběr); výchozí = interaktivní",
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

    # --- Šum a onset ---
    parser.add_argument(
        "--preroll-ms", type=float, default=120.0,
        help="Délka prerollu (ticha před note-on) pro odhad šumové podlahy v ms (výchozí 120 ms)",
    )
    parser.add_argument(
        "--onset-snr-db", type=float, default=6.0,
        help="Onset: SNR nad šumovou podlahou v dB (výchozí 6 dB; nižší = citlivější, pro vel0 zkus 4–5)",
    )
    parser.add_argument(
        "--onset-window-ms", type=float, default=10.0,
        help="RMS okno pro detekci onsetu v ms (výchozí 10 ms)",
    )
    parser.add_argument(
        "--peak-window-ms", type=float, default=10.0,
        help="RMS okno pro detekci peaku v ms (výchozí 10 ms)",
    )

    # --- Fade-out detection ---
    parser.add_argument(
        "--fadeout-snr-db", type=float, default=6.0,
        help="Fade-out: SNR nad šumem pod který = ticho v dB (výchozí 6 dB ≈ 2× šum)",
    )
    parser.add_argument(
        "--fadeout-coarse-chunks", type=int, default=16,
        help="Počet počátečních oken binary subdivision (výchozí 16)",
    )
    parser.add_argument(
        "--fadeout-min-window-ms", type=float, default=100.0,
        help="Minimální okno binary subdivision v ms (výchozí 100 ms, kryje basové frekvence)",
    )

    # --- Tail fade (když nota neodezní do konce záznamu) ---
    parser.add_argument(
        "--tail-fade-ms", type=float, default=500.0,
        help="Délka fade-out v ms na konci záznamu pokud nota neodezněla (výchozí 500 ms)",
    )

    # --- Zero-edge ochrana (start i konec) ---
    parser.add_argument(
        "--max-fade-samples", type=int, default=96,
        help="Délka cosine fade na začátku a konci samplu v vzorcích (výchozí 96 ≈ 2 ms při 48 kHz)",
    )
    parser.add_argument(
        "--zero-threshold", type=float, default=0.001,
        help="Amplituda pod kterou je hrana považována za nulu (výchozí 0.001 ≈ –60 dBFS)",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    p = pyaudio.PyAudio()
    try:
        dev_idx, dev_rate, dev_ch = select_loopback_device(p, auto_select=args.audio_device)
        midi_out = open_midi_port(args.midi_port)

        print(f"\nVýstupní adresář: {output_dir.resolve()}")
        if args.do_not_prompt:
            print("--do-not-prompt: zahájení bez potvrzení.")
        else:
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
                verbose=args.verbose,
                preroll_ms=args.preroll_ms,
                onset_snr_db=args.onset_snr_db,
                onset_window_ms=args.onset_window_ms,
                peak_window_ms=args.peak_window_ms,
                fadeout_snr_db=args.fadeout_snr_db,
                fadeout_coarse_chunks=args.fadeout_coarse_chunks,
                fadeout_min_window_ms=args.fadeout_min_window_ms,
                tail_fade_ms=args.tail_fade_ms,
                max_fade_samples=args.max_fade_samples,
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
