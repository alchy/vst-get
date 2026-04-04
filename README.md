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

`pyaudiowpatch` i `python-rtmidi` mají bundlované nativní knihovny — žádné další systémové závislosti nejsou potřeba.

## Spuštění

```bash
python run-vst-get.py --output-dir samples_out
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

## Zpracování každého samplu

Pro každou notu a velocity vrstvu probíhá následující pipeline:

### 1. Mono pracovní kopie

Ze stereo záznamu se vytvoří mono kopie jako průměr kanálů: `mono = (L + R) / 2`. Mono kopie slouží výhradně pro analýzu a nikdy se neukládá. **Žádná normalizace se neprovádí** — všechny prahy jsou relativní k naměřené šumové podlaze, takže normalizace není potřeba a pouze by zhoršila situaci pro tiché velocity vrstvy (normalizace zesiluje i šum).

### 2. Odhad šumové podlahy

Z prvních `--preroll-ms` (výchozí 120 ms) záznamu — garantovaně tiché období před note-on — se změří RMS šumové podlahy. Tato hodnota slouží jako reference pro všechny prahy. Přístup je imunní vůči zesílení šumu a funguje pro všechny velocity vrstvy včetně nejtišší (vrstva 0).

### 3. Detekce začátku (onset)

Na mono kopii se hledá první okno (`--onset-window-ms`, výchozí 10 ms), jehož RMS překročí šumovou podlahu o `--onset-snr-db` (výchozí 6 dB). Práh je tedy `noise_rms × 10^(6/20) ≈ 2× noise_rms`. Výsledný `start_frame` se aplikuje na originální stereo záznam.

Pro velmi tichá velocity (vrstva 0 na basových notách, SNR ≈ 9 dB) lze snížit `--onset-snr-db` na 4–5 dB.

### 4. Detekce peaku

Od `start_frame` se hledá okno s maximálním RMS (výchozí 10 ms okna). Výsledkem je `peak_frame`.

### 5. Detekce fade-outu (binary subdivision)

Od `peak_frame` do konce záznamu se iterativně halvingem hledá cut-out bod:

1. Rozděl úsek na **`--fadeout-coarse-chunks`** oken (výchozí 16) — počáteční okno = `len(segment) / 16`
2. Pro každé okno spočítej RMS
3. Z oken splňujících `RMS ≤ noise_rms × 10^(fadeout_snr_db/20)` vyber **nejdřívější** (první přechod pod práh)
4. Halvuj — subdivision se zastaví nejdříve na **`--fadeout-min-window-ms`** (výchozí 100 ms), aby okna nepoklesla pod délku periody basových frekvencí (A0 = 36 ms)

**Fallback:** Pokud žádné okno nesplní podmínku (nota neodezní do konce záznamu), algoritmus vrátí konec záznamu a aplikuje cosine fade délky `--tail-fade-ms` (výchozí 500 ms). Zaznamenáno v logu jako `(fallback: tail fade needed)`.

### 6. Zero-start a zero-end ochrana (prevence lupnutí)

Střih na nenulové hodnotě způsobuje lupnutí. Po ořezu stereo originálu se zkontrolují obě hrany:

1. Změř amplitudu hrany `A = max(|L|, |R|)`
2. Pokud `A < --zero-threshold` (výchozí 0.001 ≈ –60 dBFS) → hrana je na nule, nic se nedělá
3. Jinak se aplikuje **cosine fade** délky `--max-fade-samples` vzorků (výchozí 200 ≈ 4 ms při 48 kHz)
   - Začátek: fade-in `(1 − cos) / 2`
   - Konec: fade-out `(1 + cos) / 2`

### Konzolový výstup

Pro každý vzorek se loguje průběh celého pipeline:

```
[  42/704]  nota= 60  vrstva=3  vel= 64
  Šum podlahy : -72.3 dBFS  (preroll 120 ms, 5760 vzorků)
  Onset       : start_frame=7215  t=150.3 ms  (práh = šum + 6 dB = -66.3 dBFS)
  Peak        : frame=9360  t=195.0 ms  RMS=-18.4 dBFS
  Fade-out    : práh = šum + 6 dB = -66.3 dBFS  počáteční okno=1875.0 ms  min okno=100 ms
    kolo 1  [1875.00 ms / 90000 vzorků]  16 oken  nejdřívější okno pod prahem č.11  -68.1 dBFS
    kolo 2  [ 937.50 ms / 45000 vzorků]   2 oken  nejdřívější okno pod prahem č. 1  -69.4 dBFS
    kolo 3  [ 468.75 ms / 22500 vzorků]   2 oken  nejdřívější okno pod prahem č. 2  -71.0 dBFS
    kolo 4  [ 234.38 ms / 11250 vzorků]   2 oken  nejdřívější okno pod prahem č. 1  -72.8 dBFS
    kolo 5  [ 117.19 ms /  5625 vzorků]   2 oken  nejdřívější okno pod prahem č. 2  -74.1 dBFS
    → end_frame=158320  t=3298.3 ms
  Zero-start  : OK (A=0.00001 < threshold=0.0010)
  Zero-end    : OK (A=0.00008 < threshold=0.0010)
  Délka       : 3148.0 ms
  Uloženo     : m060-vel3-f48.wav  (3148.0 ms)
```

## Parametry příkazové řádky

| Parametr | Výchozí | Popis |
|----------|---------|-------|
| `--output-dir` | *(povinné)* | Výstupní adresář |
| `--note-start` | `21` | První MIDI nota (A0) |
| `--note-end` | `108` | Poslední MIDI nota (C8) |
| `--midi-port` | `loopMIDI port` | Název MIDI výstupního portu |
| `--midi-channel` | `0` | MIDI kanál 0–15 |
| `--preroll-ms` | `120` | Délka prerollu pro odhad šumové podlahy (ms) |
| `--onset-snr-db` | `6` | Onset: SNR nad šumem v dB (nižší = citlivější; pro vel0 basy zkus 4–5) |
| `--onset-window-ms` | `10` | RMS okno pro detekci onsetu (ms) |
| `--fadeout-snr-db` | `6` | Fade-out: RMS pod touto hodnotou nad šumem = ticho (dB) |
| `--fadeout-coarse-chunks` | `16` | Počet počátečních oken binary subdivision |
| `--fadeout-min-window-ms` | `100` | Min. velikost okna subdivision v ms (kryje basové frekvence) |
| `--tail-fade-ms` | `500` | Fade-out na konci záznamu pokud nota neodezněla (ms) |
| `--max-fade-samples` | `200` | Délka cosine fade na hranách samplu (vzorky, ≈ 4 ms při 48 kHz) |
| `--zero-threshold` | `0.001` | Amplituda pod kterou je hrana považována za nulu (≈ –60 dBFS) |

## Časový odhad

- Každá nota: 29 s note-on + 1 s release = 30 s
- Plný piano rozsah (88 not × 8 vrstev): ~6 hodin

## Struktura projektu

```
vst-get/
├── run-vst-get.py          # CLI entry point
├── diagnose.py             # Diagnostika loopback zařízení
└── vstget/                 # Knihovna (importovatelné moduly)
    ├── peak_detector.py    # estimate_noise_rms(), find_onset(), find_peak(), find_fadeout()
    ├── sample_processor.py # Celý processing pipeline pro jeden vzorek
    ├── sampler.py          # record_one(), sample_all(), konstanty
    ├── wasapi_recorder.py  # Recorder, list_loopback_devices, select_loopback_device
    ├── midi_utils.py       # open_midi_port()
    ├── wav_io.py           # save_wav()
    └── audio_trim.py       # SilenceTrimmer (legacy)
```

## Diagnostika

Pro ověření, které loopback zařízení zachytí VST audio:

```bash
python diagnose.py
```

Nahraje 5 s z každého loopback zařízení sekvenčně a uloží WAV soubory do `diag_out/` pro kontrolu v Audacity.
