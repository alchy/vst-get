"""
Sekvenční diagnostika loopback zařízení.
Zaznamená 5s z každého zařízení a uloží jako WAV do diag_out/.
Zahraj tón ve VST a pusť tento skript.
"""
import sys
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pyaudiowpatch as pyaudio
import numpy as np
import wave
from pathlib import Path

SECONDS = 5
OUT_DIR = Path("diag_out")
OUT_DIR.mkdir(exist_ok=True)


def record(p, dev_idx, rate, channels, seconds):
    chunk = 1024
    total = int(rate * seconds)
    frames = []
    stream = p.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=rate,
        input=True,
        input_device_index=dev_idx,
        frames_per_buffer=chunk,
    )
    recorded = 0
    while recorded < total:
        to_read = min(chunk, total - recorded)
        frames.append(stream.read(to_read, exception_on_overflow=False))
        recorded += to_read
    stream.stop_stream()
    stream.close()
    return b"".join(frames)


def save_wav(raw, path, rate, channels):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(raw)


def main():
    p = pyaudio.PyAudio()
    loopbacks = [
        (i, p.get_device_info_by_index(i))
        for i in range(p.get_device_count())
        if p.get_device_info_by_index(i).get("isLoopbackDevice", False)
    ]

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
        ch   = min(int(info["maxInputChannels"]), 2)
        name = info["name"][:51]

        try:
            raw = record(p, dev_idx, rate, ch, SECONDS)
        except Exception as e:
            print(f"[{dev_idx:>2}] {name:<52} CHYBA: {e}")
            continue

        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
        peak = np.max(np.abs(data))
        peak_db = 20 * np.log10(peak + 1e-10)

        safe_name = info["name"].replace(" ", "_").replace("(", "").replace(")", "").replace("/", "-")[:40]
        out_path = OUT_DIR / f"dev{dev_idx}_{safe_name}.wav"
        save_wav(raw, out_path, rate, ch)

        flag = "  <<< SIGNAL" if peak_db > -30 else ""
        print(f"[{dev_idx:>2}] {name:<52} {peak_db:>7.1f} dB  {out_path.name}{flag}")

    p.terminate()
    print(f"\nSoubory ulozeny do: {OUT_DIR.resolve()}")
    print("Otevri je v Audacity a najdi ktery ma VST audio.")


if __name__ == "__main__":
    main()
