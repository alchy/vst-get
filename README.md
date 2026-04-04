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
python vst-get.py --output-dir samples_out
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

### 1. Mono pracovní kopie a normalizace

Ze stereo záznamu se vytvoří mono kopie součtem L + R kanálů. Ta se normalizuje tak, aby peak dosáhl **–6 dBFS**, s limitem gainu `--max-gain-db` (výchozí 40 dB).

Limit gainu chrání tichá velocity (vrstva 0) před přílišným zesílením šumové podlahy: pokud by ideální gain překročil 40 dB, normalizace se zastaví na tomto limitu a výsledný peak bude nižší než –6 dBFS. Mono kopie slouží výhradně pro analýzu a nikdy se neukládá.

### 2. Detekce začátku (onset)

Na normalizované mono kopii se hledá začátek signálu pomocí RMS analýzy v oknech po 1 ms. Výchozí práh je **–42 dB** relativně k peaku záznamu. Výsledný `start_frame` se aplikuje na originální stereo záznam.

### 3. Detekce peaku

Od `start_frame` se hledá okno s maximálním RMS (1 ms okna, včetně posledního neúplného). Výsledkem je `peak_frame` a `peak_rms` — referenční hodnoty pro detekci fade-outu.

### 4. Detekce fade-outu (binary subdivision průměrného výkonu)

Od `peak_frame` do konce záznamu se iterativně halvingem hledá cut-out bod:

1. Rozděl úsek na **`--fadeout-coarse-chunks`** oken (výchozí 16) — počáteční okno = `len(segment) / 16`, přizpůsobuje se délce záznamu
2. Pro každé okno spočítej průměrný výkon `P = E / n_vzorků` (= RMS²) — neúplné okno na konci se vždy zahrne a počítá přes skutečný počet vzorků
3. Z oken splňujících podmínku `P ≤ peak_rms² × fadeout_ratio` vyber **nejdřívější** (první přechod pod práh)
4. Zúži hledání na toto okno a halvuj — subdivision se zastaví nejdříve na **`--fadeout-min-window-ms`** (výchozí 100 ms), aby okna nepoklesla pod délku periody basových frekvencí (A0 = 36 ms)
5. Start posledního okna = `end_frame`

**Fallback:** Pokud žádné okno nesplní podmínku ratio (signál odezněl, ale nepřekročil práh), použije se okno s nejmenším průměrným výkonem. Zaznamenáno v logu jako `(fallback: min-power)`.

### 5. Zero-start a zero-end ochrana (prevence lupnutí)

Střih na nenulové hodnotě způsobuje lupnutí při přehrávání samplu. Po ořezu stereo originálu se zkontrolují obě hrany — začátek i konec:

1. Změř amplitudu hrany `A = max(|L|, |R|)`
2. Pokud `A < --zero-threshold` (výchozí 0.001 ≈ –60 dBFS) → hrana je na nule, nic se nedělá
3. Jinak se aplikuje **cosine fade** délky `max(1, round(max_fade_samples × A))` vzorků
   - Začátek: fade-in `(1 − cos) / 2`, roste od 0 do 1
   - Konec: fade-out `(1 + cos) / 2`, klesá od 1 do 0
   - Délka škáluje s amplitudou — při A ≈ 1 se použije plný `--max-fade-samples`; při malé amplitudě se zkrátí

Dopředné hledání zero crossing se záměrně neprovádí — posunutí startu vpřed by mohlo oříznout attack transient.

### Konzolový výstup

Pro každý vzorek se loguje průběh celého pipeline:

```
[  42/704]  nota= 60  vrstva=3  vel= 64
  Normalizace : gain=+14.3 dB  (originální peak=-20.3 dBFS → cíl -6.0 dBFS)
  Onset       : start_frame=28  t=29.2 ms  (práh=-42 dB rel. k peaku)
  Peak        : frame=312  t=325.0 ms  RMS=-6.0 dBFS
  Peak (abs)  : frame=340  t=354.2 ms
  Fade-out    : ratio=0.10  P_threshold=...  počáteční okno=1875.0 ms  min okno=100 ms
    kolo 1  [1875.00 ms / 90000 vzorků]  16 oken  nejdřívější okno pod prahem č.13  P=-28.4 dB
    kolo 2  [ 937.50 ms / 45000 vzorků]   2 oken  nejdřívější okno pod prahem č. 1  P=-29.1 dB
    kolo 3  [ 468.75 ms / 22500 vzorků]   2 oken  nejdřívější okno pod prahem č. 2  P=-30.0 dB
    kolo 4  [ 234.38 ms / 11250 vzorků]   2 oken  nejdřívější okno pod prahem č. 1  P=-31.2 dB
    kolo 5  [ 117.19 ms /  5625 vzorků]   2 oken  nejdřívější okno pod prahem č. 2  P=-32.0 dB
    → end_frame=158320  t=3298.3 ms
  Zero-start  : cosine fade-in 17 vzorků  (A=0.57, max=30)
  Zero-end    : cosine fade-out 12 vzorků  (A=0.40, max=30)
  Délka       : 3291.6 ms
  Uloženo     : m060-vel3-f48.wav  (3291.6 ms)
```

## Parametry příkazové řádky

| Parametr | Výchozí | Popis |
|----------|---------|-------|
| `--output-dir` | *(povinné)* | Výstupní adresář |
| `--note-start` | `21` | První MIDI nota (A0) |
| `--note-end` | `108` | Poslední MIDI nota (C8) |
| `--midi-port` | `loopMIDI port` | Název MIDI výstupního portu |
| `--midi-channel` | `0` | MIDI kanál 0–15 |
| `--threshold-db` | `-42` | Onset práh (dB rel. k normalizovanému peaku mono kopie) |
| `--fadeout-ratio` | `0.1` | Fade-out: `P ≤ peak_rms² × ratio` |
| `--fadeout-coarse-chunks` | `16` | Počet počátečních oken binary subdivision |
| `--fadeout-min-window-ms` | `100` | Min. velikost okna subdivision v ms (kryje basové frekvence) |
| `--max-gain-db` | `40` | Max. gain normalizace v dB (limit pro tichá velocity) |
| `--max-fade-samples` | `30` | Max. délka cosine fade na začátku i konci (vzorky, škáluje s amplitudou) |
| `--zero-threshold` | `0.001` | Amplituda pod kterou je hrana považována za nulu (≈ –60 dBFS) |

## Časový odhad

- Každá nota: 29 s note-on + 1 s release = 30 s
- Plný piano rozsah (88 not × 8 vrstev): ~6 hodin

## Struktura modulů

| Modul | Obsah |
|-------|-------|
| `peak_detector.py` | `find_onset()`, `find_peak()`, `find_fadeout()` — standalone detekce |
| `sample_processor.py` | Celý processing pipeline pro jeden vzorek |
| `audio_trim.py` | `SilenceTrimmer` — standalone reusable silence trimmer |
| `wasapi_recorder.py` | `Recorder`, `list_loopback_devices`, `select_loopback_device` |
| `midi_utils.py` | `open_midi_port` |
| `wav_io.py` | `save_wav` |
| `sampler.py` | `record_one`, `sample_all`, konstanty |
| `vst-get.py` | CLI entry point |
| `diagnose.py` | Diagnostika loopback zařízení |

## Diagnostika

Pro ověření, které loopback zařízení zachytí VST audio:

```bash
python diagnose.py
```

Nahraje 5 s z každého loopback zařízení sekvenčně a uloží WAV soubory do `diag_out/` pro kontrolu v Audacity.
