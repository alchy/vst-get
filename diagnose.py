"""
diagnose.py — Sekvenční diagnostika loopback zařízení.
Zaznamená 5 s z každého zařízení a uloží jako WAV do diag_out/.
Zahraj tón ve VST a pusť tento skript.
"""

import sys

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
from pathlib import Path

import pyaudiowpatch as pyaudio

from wasapi_recorder import Recorder, list_loopback_devices
from wav_io import save_wav

SECONDS = 5
OUT_DIR = Path("diag_out")
OUT_DIR.mkdir(exist_ok=True)


def main():
    p = pyaudio.PyAudio()
    loopbacks = list_loopback_devices(p)

    if not loopbacks:
        print("Zadna loopback zarizeni.")
        p.terminate()
        return

    print(f"Nalezeno {len(loopbacks)} loopback zarizeni.")
    print(">>> ZAHRAJ TON VE VST - bude nahravano sekvenčně <<<\n")
    print(f"{'#':<4} {'Zarizeni':<52} {'Peak dB':>8}  {'Soubor'}")
    print("-" * 85)

    for dev_idx, info in loopbacks:
        rate = int(info["defaultSampleRate"])
        ch = min(int(info["maxInputChannels"]), 2)
        name = info["name"][:51]

        try:
            rec = Recorder(p, dev_idx, rate, ch)
            rec.start(duration=SECONDS)
            data = rec.get(join_timeout=SECONDS + 5)
        except Exception as e:
            print(f"[{dev_idx:>2}] {name:<52} CHYBA: {e}")
            continue

        peak = np.max(np.abs(data))
        peak_db = 20 * np.log10(peak + 1e-10)

        safe_name = (
            info["name"]
            .replace(" ", "_")
            .replace("(", "")
            .replace(")", "")
            .replace("/", "-")[:40]
        )
        out_path = OUT_DIR / f"dev{dev_idx}_{safe_name}.wav"
        save_wav(data, out_path, rate, ch)

        flag = "  <<< SIGNAL" if peak_db > -30 else ""
        print(f"[{dev_idx:>2}] {name:<52} {peak_db:>7.1f} dB  {out_path.name}{flag}")

    p.terminate()
    print(f"\nSoubory ulozeny do: {OUT_DIR.resolve()}")
    print("Otevri je v Audacity a najdi ktery ma VST audio.")


if __name__ == "__main__":
    main()
