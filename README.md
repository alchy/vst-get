# vstget — VST Instrument Sampler

Automaticky sampluje standalone VST nástroj přes všechny noty v rozsahu piana (MIDI 21–108) a 8 velocity vrstev. MIDI komunikace probíhá přes loopMIDI, audio se zachytává přes WASAPI loopback.

## Požadavky

- Windows 10/11
- Python 3.13
- [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) (Tobias Erichsen) — spuštěný s portem `loopMIDI Port`
- VST nástroj v režimu **standalone** — routovaný na výstupní audio zařízení Windows
- Audio zařízení s WASAPI loopback podporou (integrovaná karta nebo ZOOM UAC-2 apod.)

## Instalace

### 1. Externí aplikace (mimo pip)

| Aplikace | Poznámka |
|----------|----------|
| [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) | Musí běžet se spuštěným portem `loopMIDI Port` |
| VST nástroj (standalone) | Spustit před zahájením samplování |

### 2. Python závislosti

```bash
pip install -r requirements.txt
```

`pyaudiowpatch` i `python-rtmidi` mají bundlované nativní knihovny (PortAudio DLL, RtMidi) — žádné další systémové závislosti nejsou potřeba.

## Spuštění

```bash
python main.py --output-dir samples_out
```

Program při startu:
1. Vypíše dostupná WASAPI loopback zařízení — vyber to, na které VST vysílá
2. Ověří dostupnost portu `loopMIDI Port`
3. Čeká na potvrzení (Enter) — čas na spuštění VST

## Výstupní formát

Soubory se ukládají jako **stereo interleaved 16-bit PCM WAV**:

```
mNNN-velV-fSR.wav
 │    │    └─ vzorkovací frekvence: f48 = 48 kHz, f44 = 44 kHz
 │    └─────── velocity vrstva: 0 (nejtiší) – 7 (nejhlasitější)
 └──────────── MIDI číslo noty: 000–127  (060 = C4, 069 = A4)
```

Příklady: `m060-vel0-f44.wav`, `m060-vel7-f44.wav`, `m021-vel3-f48.wav`

## Velocity vrstvy

8 vrstev rovnoměrně rozložených v rozsahu 1–127:

| Vrstva | MIDI velocity |
|--------|--------------|
| 0      | 16           |
| 1      | 32           |
| 2      | 48           |
| 3      | 64           |
| 4      | 79           |
| 5      | 95           |
| 6      | 111          |
| 7      | 127          |

## Parametry příkazové řádky

| Parametr | Výchozí | Popis |
|----------|---------|-------|
| `--output-dir` | *(povinné)* | Výstupní adresář |
| `--threshold-db` | `-50` | Práh ticha pro ořez v dB (zkus `-40` pro agresivnější ořez) |
| `--note-start` | `21` | První MIDI nota (A0) |
| `--note-end` | `108` | Poslední MIDI nota (C8) |
| `--midi-port` | `loopMIDI Port` | Název MIDI výstupního portu |
| `--midi-channel` | `0` | MIDI kanál 0–15 |
| `--no-normalize` | — | Vypne per-nota normalizaci |

## Normalizace

Výchozí chování: pro každou notu se nahrají všechny velocity vrstvy, pak se všechny normalizují **stejným faktorem** tak, aby nejhlasitější vrstva dosáhla -1 dBFS. Relativní dynamika mezi vrstvami je zachována.

Vypnutí normalizace: `--no-normalize`

## Trim ticha

Každý záznam se automaticky ořeže:
- **začátek** — odstraní ticho před attackem noty
- **konec** — odstraní decay/dozvuk pod prahem (viz `--threshold-db`)

Pokud je výsledek prázdný (nota na daném rozsahu nehraje), soubor se neuloží.

### Jak detekce funguje (audio_trim.py)

Nahrávka každé noty začíná 150ms před odesláním MIDI note-on (preroll), aby útok nebyl oříznutý. Po nahrání se preroll ticho odstraní takto:

1. Celý záznam se rozdělí na okna délky **1 ms**
2. V každém okně se vypočítá **RMS** (efektivní hodnota hlasitosti)
3. RMS se převede na **dB relativně k peaku** celého záznamu
4. **Začátek** = první okno, kde RMS překročí `--threshold-db` (výchozí -50 dB)
5. **Konec** = poslední okno nad prahem

Výsledek: sampl začíná přesně na transientu kladívka (přesnost 1 ms), bez prerollového šumu.

### Použití audio_trim.py v jiném projektu

Soubor `audio_trim.py` lze zkopírovat do libovolného projektu a použít samostatně — nevyžaduje žádné další soubory z tohoto projektu.

```python
import wave
import numpy as np
from audio_trim import SilenceTrimmer

# Načtení WAV souboru
with wave.open("muj_záznam.wav") as wf:
    sample_rate = wf.getframerate()
    channels = wf.getnchannels()
    raw = wf.readframes(wf.getnframes())

# Převod na numpy pole (float32, hodnoty -1.0 až +1.0)
data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
if channels > 1:
    data = data.reshape(-1, channels)  # stereo: tvar (pocet_vzorku, 2)

# Ořez ticha
trimmer = SilenceTrimmer(threshold_db=-50.0)
trimmed, start_sample, end_sample = trimmer.trim(data, sample_rate)

if len(trimmed) == 0:
    print("Záznam je celý tichý, nic k uložení.")
else:
    print(f"Ořez: začátek na {start_sample/sample_rate*1000:.1f} ms, "
          f"konec na {end_sample/sample_rate*1000:.0f} ms")

    # Uložení oříznutého souboru (16-bit PCM)
    data_int = (trimmed * 32767.0).clip(-32768, 32767).astype(np.int16)
    with wave.open("oríznutý.wav", "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data_int.tobytes())
```

**Parametr `threshold_db`** — čím vyšší (méně záporné) číslo, tím agresivnější ořez:
- `-50` (výchozí) — bezpečné, odstraní pouze šum a velmi tichý preroll
- `-40` — agresivnější, vhodné pokud zůstává příliš dlouhý dozvuk

## Časový odhad

- Každá nota: 29 s note-on + 1 s release = 30 s
- Plný piano rozsah (88 not × 8 vrstev): ~6 hodin

## Diagnostika

Pro ověření, které loopback zařízení zachytí VST audio:

```bash
python diagnose.py
```

Nahraje 5 s z každého loopback zařízení sekvenčně a uloží WAV soubory do `diag_out/` pro kontrolu v Audacity.
