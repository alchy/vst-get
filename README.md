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

Ze stereo záznamu se vytvoří mono kopie součtem L + R kanálů. Ta se normalizuje tak, aby peak dosáhl **–6 dBFS**. Tato kopie slouží výhradně pro analýzu (nikdy se neukládá).

### 2. Detekce začátku (onset)

Na normalizované mono kopii se hledá začátek signálu pomocí RMS analýzy v oknech po 1 ms. Výchozí práh je **–42 dB** relativně k peaku. Výsledný `start_frame` se aplikuje na originální stereo záznam.

### 3. Detekce peaku

Od `start_frame` se hledá okno s maximálním RMS (1 ms okna). Výsledkem je `peak_frame` a `peak_rms` — referenční hodnoty pro detekci fade-outu.

### 4. Detekce fade-outu (binary subdivision průměrného výkonu)

Od `peak_frame` do konce záznamu se iterativně halvingem hledá cut-out bod:

1. Rozděl úsek na **`--fadeout-coarse-chunks`** oken (výchozí 16) — velikost okna se přizpůsobuje délce záznamu od peaku: `initial_hop = len(segment) / 16`
2. Pro každé okno spočítej průměrný výkon `P = E / n_vzorků` (= RMS²) — neúplné okno na konci záznamu se vždy zahrne a počítá se přes skutečný počet vzorků
3. Z oken splňujících podmínku `P ≤ peak_rms² × fadeout_ratio` vyber **nejdřívější** (první přechod pod práh, ne nejtiší bod)
4. Zúži hledání na toto okno, halvuj → **50 % → 25 % → … → 1 vzorek**
5. Start posledního okna = `end_frame`

**Fallback:** Pokud žádné okno nesplní podmínku, použije se okno s nejmenším průměrným výkonem (signál odezněl, ale nepřekročil práh). Zaznamenáno v logu jako `(fallback: min-power)`.

### 5. Zero-start ochrana (prevence lupnutí)

Střih na nenulové hodnotě způsobuje lupnutí při přehrávání. Po ořezu stereo originálu se proto:

1. Zkontroluje amplituda prvního vzorku `A = max(|L|, |R|)`
2. Pokud `A < --zero-threshold` (výchozí 0.001 ≈ –60 dBFS) → vzorek začíná na nule, nic se nedělá
3. Jinak se aplikuje **cosine fade-in** délky `max(1, round(max_fade_in × A))` vzorků
   - Při plné amplitudě (A≈1) se použije plných `--max-fade-in` vzorků; při malé amplitudě se délka zkrátí úměrně

Poznámka: dopředné hledání zero crossing se záměrně neprovádí — posunutí startu vpřed by mohlo oříznout začátek transientu, což je auditivně výraznější vada než krátký fade-in.

### Konzolový výstup

Pro každý vzorek se loguje průběh celého pipeline:

```
[  42/704]  nota= 60  vrstva=3  vel= 64
  Normalizace : gain=+14.3 dB  (originální peak=-20.3 dBFS → cíl -6.0 dBFS)
  Onset       : start_frame=28  t=29.2 ms  (práh=-42 dB)
  Peak        : frame=312  t=325.0 ms  RMS=-6.0 dBFS
  Peak (abs)  : frame=340  t=354.2 ms
  Fade-out    : ratio=0.10  E_threshold=...
    kolo 1  [100 ms]  31 oken  min-energie okno č.18  E=-41.2 dB
    kolo 2  [ 50 ms]   2 oken  min-energie okno č. 2  E=-43.8 dB
    ...
    → end_frame=3187  t=3320.8 ms
  Zero-start  : cosine fade-in 17 vzorků  (A=0.57, max_fade_in=30)
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
| `--fadeout-coarse-chunks` | `16` | Počet počátečních oken binary subdivision (přizpůsobuje se délce záznamu) |
| `--max-fade-in` | `30` | Max. délka cosine fade-in na začátku (vzorky, škáluje s amplitudou) |
| `--zero-threshold` | `0.001` | Amplituda pod kterou je start považován za nulu (≈ –60 dBFS) |

## Časový odhad

- Každá nota: 29 s note-on + 1 s release = 30 s
- Plný piano rozsah (88 not × 8 vrstev): ~6 hodin

## Struktura modulů

| Modul | Obsah |
|-------|-------|
| `audio_trim.py` | `SilenceTrimmer` — standalone reusable silence trimmer |
| `peak_detector.py` | `find_onset()`, `find_peak()`, `find_fadeout()` — standalone detekce |
| `sample_processor.py` | Celý processing pipeline pro jeden vzorek |
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
