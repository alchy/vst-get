# vstget — VST Instrument Sampler

Automaticky sampluje VST nástroj přes všechny noty v rozsahu piana (MIDI 21–108) a 8 velocity vrstev.

**Cross-platform:** funguje na macOS i Windows.

| | macOS | Windows |
|---|---|---|
| Audio zachycení | CoreAudio + [BlackHole](https://existential.audio/blackhole/) | WASAPI loopback (pyaudiowpatch) |
| MIDI routing | IAC Driver (vestavěný v macOS) | [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) |

## Požadavky

### macOS

- macOS 12+
- Python 3.11+
- [BlackHole 2ch](https://existential.audio/blackhole/) — virtuální audio loopback
- IAC Driver — vestavěný v macOS, je potřeba aktivovat (viz níže)
- VST nástroj v DAW nebo standalone — s audio výstupem na BlackHole 2ch

### Windows

- Windows 10/11
- Python 3.11+
- [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) (Tobias Erichsen) — spuštěný s portem `loopMIDI Port`
- VST nástroj v režimu **standalone** — routovaný na výstupní audio zařízení Windows
- Audio zařízení s WASAPI loopback podporou (integrovaná karta nebo ZOOM UAC-2 apod.)

## Instalace

### macOS

#### 1. BlackHole (virtuální audio loopback)

```bash
brew install blackhole-2ch
```

Po instalaci se BlackHole 2ch objeví jako audio vstup i výstup v systému.

#### 2. IAC Driver (virtuální MIDI)

IAC Driver je součástí macOS, ale je potřeba ho aktivovat:

1. Otevři **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup")
2. Menu **Window → Show MIDI Studio**
3. Dvakrát klikni na **IAC Driver**
4. Zaškrtni **Device is online**
5. Ověř, že existuje alespoň jeden bus (např. "Bus 1")

#### 3. Python závislosti

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Na macOS je potřeba mít nainstalovaný `portaudio`:

```bash
brew install portaudio
```

#### 4. Nastavení VST nástroje

1. Spusť VST nástroj (standalone nebo v DAW, např. UVI Workstation)
2. **Audio výstup** nastav na **BlackHole 2ch**
3. **MIDI vstup** nastav na **IAC Driver Bus 1**

**Tip:** Pokud chceš zároveň slyšet výstup, vytvoř v Audio MIDI Setup **Multi-Output Device** obsahující BlackHole 2ch + reproduktory/sluchátka.

### Windows

#### 1. Externí aplikace

| Aplikace | Poznámka |
|----------|----------|
| [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) | Musí běžet se spuštěným portem `loopMIDI Port` |
| VST nástroj (standalone) | Spustit před zahájením samplování |

#### 2. Python závislosti

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`pyaudiowpatch` i `python-rtmidi` mají bundlované nativní knihovny — žádné další systémové závislosti nejsou potřeba.

## Spuštění

### macOS

Legacy režim (8 velocity vrstev):

```bash
.venv/bin/python run-vst-get.py --output-dir samples_out --midi-port "IAC Driver" --audio-device 0
```

Vlastní počet vrstev (např. 32 vzorků na notu — MIDI velocity 4, 8, 12, …, 127):

```bash
.venv/bin/python run-vst-get.py --output-dir samples_out --midi-port "IAC Driver" --audio-device 0 --velocity-layers 32
```

### Windows

Legacy režim (8 velocity vrstev):

```bash
.venv\Scripts\python run-vst-get.py --output-dir samples_out
```

Vlastní počet vrstev (např. 32 vzorků na notu):

```bash
.venv\Scripts\python run-vst-get.py --output-dir samples_out --velocity-layers 32
```

Program při startu:
1. Vypíše dostupná audio vstupní zařízení — na macOS hledej **BlackHole 2ch** (označeno ★), na Windows WASAPI loopback
2. Ověří dostupnost MIDI portu (IAC Driver / loopMIDI)
3. Čeká na potvrzení (Enter) — čas na spuštění VST; přeskoč pomocí `--do-not-prompt`

## Výstupní formát

Soubory se ukládají jako **stereo interleaved 16-bit PCM WAV**. Pojmenování závisí na režimu:

### Legacy režim (bez `--velocity-layers`)

```
mNNN-velV-fSR.wav
 │    │    └─ vzorkovací frekvence: f48 = 48 kHz, f44 = 44 kHz
 │    └─────── velocity vrstva: 0 (nejtiší) – 7 (nejhlasitější)
 └──────────── MIDI číslo noty: 000–127  (060 = C4, 069 = A4)
```

Příklady: `m060-vel0-f44.wav`, `m060-vel7-f44.wav`, `m021-vel3-f48.wav`

### Vlastní režim (s `--velocity-layers N`)

```
mNNN-vVVV-fSR.wav
 │    │    └─ vzorkovací frekvence: f48 = 48 kHz, f44 = 44 kHz
 │    └─────── index velocity vrstvy: 000 (nejtiší) – (N−1) (nejhlasitější), zero-padded na 3 cifry
 └──────────── MIDI číslo noty: 000–127
```

Indexy jsou souvislé od 0 do N−1 — bez skoků a nezávisle na skutečné MIDI velocity, která se posílá do VST.

Příklady (pro `--velocity-layers 32`, indexy 0–31): `m060-v000-f48.wav`, `m060-v015-f48.wav`, `m060-v031-f48.wav`

## Velocity vrstvy

### Legacy režim (8 vrstev)

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

### Vlastní režim (`--velocity-layers N`)

Po zadání `--velocity-layers N` (1–127) program vygeneruje N vrstev rovnoměrně rozložených v rozsahu 1–127 podle vzorce `round(127/N · (i+1))` pro `i = 0..N-1`. Poslední vrstva má vždy MIDI velocity 127.

Příklady:

| N | Inkrement | MIDI velocity |
|---|-----------|--------------|
| 16 | ≈ 8 | 8, 16, 24, 32, …, 119, 127 |
| 32 | ≈ 4 | 4, 8, 12, 16, …, 123, 127 |
| 64 | ≈ 2 | 2, 4, 6, 8, …, 125, 127 |
| 127 | 1 | 1, 2, 3, …, 126, 127 |

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

### Obecné

| Parametr | Výchozí | Popis |
|----------|---------|-------|
| `--output-dir` | *(povinné)* | Výstupní adresář |
| `--do-not-prompt` | *(příznak)* | Přeskočit potvrzení Enterem před zahájením |
| `--verbose` | *(příznak)* | Podrobný výpis celé pipeline (výchozí: jeden kompaktní řádek na vzorek) |
| `--audio-device` | *(interaktivní)* | Index audio zařízení — přeskočí interaktivní výběr |

### MIDI

| Parametr | Výchozí | Popis |
|----------|---------|-------|
| `--midi-port` | `IAC Driver` (macOS) / `loopMIDI port` (Win) | Název MIDI výstupního portu |
| `--midi-channel` | `0` | MIDI kanál 0–15 |
| `--note-start` | `21` | První MIDI nota (A0) |
| `--note-end` | `108` | Poslední MIDI nota (C8) |
| `--velocity-layers` | *(legacy 8)* | Počet velocity vrstev (1–127). Bez parametru = legacy režim s 8 vrstvami a pojmenováním `mNNN-velV-fSR.wav`; s parametrem se použije `mNNN-vVVV-fSR.wav` (VVV = index vrstvy 000…N−1, souvislý) |
| `--prevent-damper-sound` | *(příznak)* | Note-off odložen až po skončení záznamu — damper nezazní do samplu (+5 s/nota) |

### Detekce onsetu

| Parametr | Výchozí | Popis |
|----------|---------|-------|
| `--preroll-ms` | `120` | Délka prerollu pro odhad šumové podlahy (ms) |
| `--onset-snr-db` | `6` | SNR nad šumem pro detekci onsetu v dB (nižší = citlivější; pro vel0 basy zkus 4–5) |
| `--onset-window-ms` | `10` | RMS okno pro detekci onsetu (ms) |
| `--zero-threshold` | `0.001` | Amplituda pod kterou je vzorek považován za nulový (≈ –60 dBFS); slouží i pro sub-window refinement onsetu |

### Detekce fade-outu

| Parametr | Výchozí | Popis |
|----------|---------|-------|
| `--peak-window-ms` | `10` | RMS okno pro detekci peaku (ms) |
| `--fadeout-snr-db` | `6` | Fade-out: RMS pod touto hodnotou nad šumem = ticho (dB) |
| `--fadeout-coarse-chunks` | `16` | Počet počátečních oken binary subdivision |
| `--fadeout-min-window-ms` | `100` | Min. velikost okna subdivision v ms (kryje basové frekvence) |
| `--tail-fade-ms` | `500` | Fade-out na konci záznamu pokud nota neodezněla (ms) |

### Ochrana hran (click prevence)

| Parametr | Výchozí | Popis |
|----------|---------|-------|
| `--max-fade-samples` | `96` | Délka cosine fade na hranách samplu (vzorky, ≈ 2 ms při 48 kHz) |

## Prevence damper zvuku (`--prevent-damper-sound`)

U akustických nástrojů (piano, upright bass) způsobuje note-off dopad tlumítka (damperu) na strunu — krátký mechanický zvuk, který může být slyšitelný na konci samplu, zejména v tichých velocity vrstvách.

Bez příznaku:

```
PREROLL (0.15 s, nahrává se) → note-on → NOTE_HOLD (29 s) → note-off ← damper
→ NOTE_RELEASE (1 s, nahrává se) → konec záznamu
```

S `--prevent-damper-sound`:

```
PREROLL (0.15 s, nahrává se) → note-on → TOTAL_DURATION (30 s, nahrává se, nota drží)
→ konec záznamu → 2.5 s pauza → note-off ← damper (nenáhráváno) → 2.5 s pauza → další nota
```

Obsah a délka záznamu jsou stejné. Celková doba samplování se zvyšuje o 5 s na notu.

## Časový odhad

| Režim | Čas na notu | 88 not × 8 vrstev |
|-------|-------------|-------------------|
| Výchozí | 30 s | ~6 hodin |
| `--prevent-damper-sound` | 35 s (+5 s pauza) | ~7 hodin |

## Struktura projektu

```
vst-get/
├── run-vst-get.py          # CLI entry point
├── diagnose.py             # Diagnostika audio zařízení
├── requirements.txt        # Python závislosti (cross-platform)
└── vstget/                 # Knihovna (importovatelné moduly)
    ├── recorder.py         # Cross-platform audio recorder (CoreAudio / WASAPI)
    ├── peak_detector.py    # estimate_noise_rms(), find_onset(), find_peak(), find_fadeout()
    ├── sample_processor.py # Celý processing pipeline pro jeden vzorek
    ├── sampler.py          # record_one(), sample_all(), konstanty
    ├── midi_utils.py       # open_midi_port()
    ├── wav_io.py           # save_wav()
    ├── wasapi_recorder.py  # Legacy WASAPI-only recorder (zachován pro zpětnou kompatibilitu)
    └── audio_trim.py       # SilenceTrimmer (legacy)
```

## Diagnostika

Pro ověření, které audio zařízení zachytí VST audio:

```bash
# macOS
.venv/bin/python diagnose.py

# Windows
.venv\Scripts\python diagnose.py
```

Nahraje 5 s z každého vstupního zařízení sekvenčně a uloží WAV soubory do `diag_out/` pro kontrolu v Audacity.
