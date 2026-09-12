# Handover — SDR ALERT Decoder

**Updated:** 2026-09-12 · **Repo:** https://github.com/agmurf/sdr-alert-decoder · **HEAD at writing:** `626f51f`
**For:** whoever picks this up next — human or Claude. Read §9 before changing any DSP.

---

## 1. What this is

An off-air decoder for **ALERT flood telemetry**. Point a cheap RTL-SDR at the ALERT
channel (151.5 MHz in NSW) and read exactly what a field station put on the radio:
sensor ID, value, timestamp, and the four raw bytes it came from.

The point is independence. Whatever a logger *claims* it is sending, this says what
actually went out. That is not theoretical — it is how the test rig's frozen-payload
defect was found (§7).

Two applications, one DSP chain ported between them:

| | where | what it is |
|---|---|---|
| **Desktop** | `src/` | Python + Tkinter, PyInstaller build, Windows installer |
| **Android** | `android/` | Kotlin + Compose, on-device decoding, phone hosts the dongle |

---

## 2. Status at a glance

| Thing | State |
|---|---|
| ALERT Binary (live network) | **Proven off-air**, both apps |
| Enhanced iFLOWS (test rig) | **Proven off-air**, both apps, CRC-validated |
| ALERT2 | **Built and unit-tested, never seen a real transmitter** |
| Desktop app | Working, deployed on AI_1 and maddog |
| Windows installer | Built, tested, published as **v1.1.0** |
| Android app | Working, decodes real rig traffic; 10/10 unit tests pass |
| APK on the phone | **Not installed** since the icon change — nothing attached to `adb` |
| Day-long 3-protocol soak | **Not run.** No `soak_log.csv` on either machine |

---

## 3. Where everything runs

- **maddog** — development, builds, git. Working copy `C:\SDR ALERT Decoder`, repo `C:\repos\sdr-alert-decoder`.
- **AI_1** — **the RTL-SDR lives here** (R820T, SN 77771111153705700). Testing happens here.
- **Share** — `N:\networks\adamsnet\sdr-alert-decoder`, mounted on both, is how code reaches AI_1 (SMB to AI_1 is closed; this goes over the noBGP overlay).
- **Phone** — Android app; needs the free **"RTL2832U driver"** app (`marto.rtl_tcp_andro`), which owns the USB device and serves `rtl_tcp` on `127.0.0.1:14423`. That is why this app needs no NDK, no libusb and no USB permission handling.

> **There are three copies of this code and they drift.** Work done on AI_1 reaches the
> share and the repo but **not** maddog's working copy. This has already shipped a build
> around a stale decoder — see §9.6.

---

## 4. The radio, in the facts that matter

- **AFSK over narrowband FM** — mark **1300.8 Hz**, space **2109.4 Hz**, **300 baud**.
  It is *not* direct carrier FSK; believing that cost a week (§9.1).
- A frame is **40 bits**: four 10-bit UART bytes (start + 8 data LSB-first + stop),
  carrying a **13-bit sensor ID** and an **11-bit value** (0–2047, a hard ceiling).
- **Four-sideband matched filter.** Under FM the tones appear as sideband *pairs* at
  ±1300.8 and ±2109.4 with a suppressed carrier. The detector uses all four:
  `d = (|MF(+f1)| + |MF(-f1)|) - (|MF(+f2)| + |MF(-f2)|)`. Worth ~8 dB over the accidental
  single-sideband detection that preceded it; usable decode threshold went ~31–33 dB → ~23–25 dB.
- **Gain does not improve SNR.** Measured on one signal: 40 / 44.5 / 49.6 dB gain gave
  17.5 / 16.7 / 17.8 dB SNR — the tuner amplifies signal and noise together. Gain controls
  **ADC headroom**; its real job is avoiding front-end overload, which corrupts bit
  decisions while the tones still look clean. The Android app shows a clipping warning
  rather than chasing signal strength.
- Sample rates: desktop `IQ_RATE = 1_024_000`; ALERT2 channel `48_000` (10 samples/bit);
  Android `FS_IN = 240_000`, decimate by 20 → 12 kHz → **exactly 40 samples/symbol**.
  Non-integer samples-per-symbol caused timing drift and wrecked vote counts. Keep it integer.
- Frequency, gain and PPM are user-settable in both apps and applied live.

---

## 5. The three protocols — and the naming trap

Adam's framing, which the apps now follow: **ALERT Binary**, **Enhanced iFLOWS** and
**ALERT2** are three distinct protocols.

| Selector | Recognise it by | Proven against |
|---|---|---|
| **ALERT Binary** (default) | all four bytes marked `01/01/11/11`, no checksum | the live 151.5 MHz network |
| **Enhanced iFLOWS** | only byte 0 marked, 6-bit CRC in byte 3 | the 4078 ERT-A2 test rig |
| **ALERT2** | 4800 bps, RS + convolutional FEC, a different radio entirely | nothing yet |

**The trap:** NSW operators call their network *"iFLOWS"*, but its frames are the
**ALERT Binary** format. So "what the iFLOWS network sends" is *not* the Enhanced iFLOWS
frame format. Both selectors are therefore named for the frame format and carry a
one-line hint in the UI. Do not rename them back to network names.

Enhanced iFLOWS layout (ALERT2 Application Layer Spec v1.3, Appendix 2 §5.3):

```
b0: 1  1   A5  A4  A3  A2  A1 A0     CRC-6, polynomial x^6 + x^4 + x^3 + 1
b1: D0 A12 A11 A10 A9  A8  A7 A6     over bytes 0-2 plus D9,D10
b2: D8 D7  D6  D5  D4  D3  D2 D1     LSB-first, reflected output, init 0, xorout 0
b3: C0 C1  C2  C3  C4  C5  D10 D9
```

### ★ One protocol at a time — never both

Running ALERT Binary and Enhanced iFLOWS together **does not work and cannot be fixed by
thresholding**. Enhanced iFLOWS has 8 bits of constraint (2 marker bits + 6-bit CRC)
against Binary's 16 fixed bits, so a strong Binary burst produces *systematically*
CRC-valid Enhanced iFLOWS mis-parses that repeat across parameter combinations. Measured:
they survived vote thresholds of 4, 12 **and 35**.

```
rig,  Enhanced iFLOWS only : 4078=528 mm, 4079=420 mm, 4080=12.1 V   correct
live, ALERT Binary only    : the four real stations                  correct
live, both formats         : those four PLUS phantom 3062 and 7250   wrong
```

**Do not re-add a "Both" option for the two 300-baud formats.**

---

## 6. Code map

```
src/
  fsk_pll_decode.py    DSP core: predecimate, channelize, afsk_soft_decision (4-sideband),
                       Gardner timing, frames_from_symbols, frames_multi
  alert_formats.py     the three 40-bit frame parsers + crc6_enhanced
  iq_decoder.py        IQ -> bursts -> carrier candidates -> decode -> vote -> callback
  iq_sdr_interface.py  spawns rtl_sdr, rolling IQ buffer, live re-tune, finds rtl-sdr tools
  alert2.py            ALERT2 PHY + link: MLS scramble, RS(255,239), Viterbi, sync, Gardner
  alert2_app.py        ALERT2 MANT header + Application PDU parsing
  alert2_decoder.py    ALERT2 IQ -> readings (mirrors iq_decoder's interface)
  field_application.py the GUI: tuning, protocol selector, readings table, CSV logging
  sensor_database.py   optional site register (Sensors.xlsx) + per-sensor unit/multiplier
android/app/src/main/java/tech/floodwarning/alertdecoder/
  AlertDsp.kt             the Kotlin port of the above (line-for-line)
  Alert1Formats.kt        the three frame formats + crc6Enhanced
  Alert2.kt, Alert2App.kt ALERT2
  RtlSource.kt            rtl_tcp client against the driver app
  SiteMetadata.kt         operator CSV import (no bundled register — see §10)
  DecoderViewModel.kt     protocol + tuning state, decode loop
installer/    Inno Setup script, NOTICES, build_installer.py
tools/        protocol_soak.py, testrig_monitor.py, live_meter.py, correlate.py
```

**Vote thresholds are NOT transferable between the two implementations.** Desktop:
`MIN_VOTES=6`, `MIN_VOTES_UNKNOWN=10`, `MIN_VOTES_CRC=35`, `MIN_VOTES_15=14`. Android:
`minVotes=4`, `minVotesCrc=4`. The phone sweeps fewer parameter combinations, so its real
frames score 6–7 where the desktop's score 20+. Copying the desktop's 35 to the phone
silently decodes nothing.

---

## 7. The test rig (ground truth)

An ERT-A2 fed over SDI-12. Configured IDs: **4078** station address and DI3, **4079**
river level, **4080** battery. It sends **Enhanced iFLOWS**, roughly every 5 minutes.

Known-good frames — these are the regression vectors:

```
4078 = 528  (mm)      EE 3F 08 01
4079 = 1807           EF BF 87 53    (the frozen value; live it reads ~420)
4080 = 121  (12.1 V)  F0 BF 3C 4C
```

`4080 = 121 → 12.1 V` and `4078 = 528 mm` match the rig's own configuration exactly, and
the 6-bit CRC validates all five known frames. That is what proved the format.

**Frozen payload (solved).** The rig once re-sent a byte-identical payload for 55 minutes.
The radio side detected it off-air; the SDI-12 side found the cause — `VALID_MIN_M = 0.35`
was rejecting a real 0.300–0.348 m tide, and the rig holds its last good value. Note the
consequence for anyone consuming this data: **ALERT1 has no way to signal "no reading"** —
a held value is indistinguishable from a fresh one in the value field.

---

## 8. Build, deploy, verify

**Desktop** (from `src/`):

```bash
python -m PyInstaller --noconfirm "SDR ALERT Decoder.spec"
python "C:/SDR ALERT Decoder/deploy.py"
```

`deploy.py` refuses a stale build (timestamps vs watched sources), checks the PYZ module
table, replaces `_internal` wholesale — never merge it, mixing python311/312 runtimes has
broken this — and validates `base_library.zip`.

**Installer** (needs Inno Setup 6, installed per-user at `%LOCALAPPDATA%\Programs\Inno Setup 6`):

```bash
python installer/build_installer.py --dist "C:/SDR ALERT Decoder/src/dist/SDR ALERT Decoder"
```

**Android:**

```bash
cd "C:\SDR ALERT Decoder\android" && gradlew.bat :app:assembleDebug :app:testDebugUnitTest
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

`local.properties` must use forward slashes, or the SDK path parses as escape sequences.

**Regression — run this after touching any DSP.** ~200 three-second windows of
`captures/testrig.bin` under `ENHANCED_IFLOWS` must yield 4078=528, 4079=1807, 4080=121
with the raw bytes above. The Android side asserts the same thing against a real off-air
burst in `app/src/test/resources/testrig_burst_240k.iq8`, plus faster-than-real-time
decoding and that random noise yields nothing. All 10 tests pass as of 2026-09-12.

---

## 9. Traps that have already cost time

**9.1 "Direct FSK."** The signal was believed to be direct carrier FSK for a long time.
It is AFSK over NBFM. Measurement settled it; the rebuild gained ~8 dB.

**9.2 "Station 5461" is not a station.** It is the `0x55` preamble decoding as a frame
(`55 55 D5 D5` → sid 5461, val 682, at a perfect 16/16). Excluded now. Any "new station"
that decodes perfectly and repeats everywhere deserves this suspicion.

**9.3 The bit-12 stale flag was WRONG.** An earlier conclusion — that ID bit 12 flags
held data — was an artefact of parsing Enhanced iFLOWS frames under the Binary layout
(A12 lives in byte1 bit6; we were reading byte2 bit0, manufacturing a phantom +4096).
**`handover.md` in the working copy still asserts it** and was sent to the SDI-12
workstream; it carries a correction banner now.

**9.4 Don't reimplement what already works.** A clean-room rewrite of the BINARY path
inside `frames_multi` invented stations 3062 and 7250 on a capture containing exactly
four. `frames_multi` now *delegates* BINARY to the proven `frames_from_symbols`.

**9.5 Stale deploys are silent.** An exe copied while a later PyInstaller run was still
going looks fresh by timestamp and decodes nothing. Use `deploy.py`. Also: you cannot
check for *function* names inside the exe — PyInstaller marshals and compresses code into
the PYZ, so only module names are readable as plain bytes.

**9.6 The local source itself can be stale.** `deploy.py` proves the build is newer than
maddog's sources; it cannot know those sources are behind the repo and the share. This
happened on 2026-09-01, and again on 2026-09-12 (the gauge-site scrub had never reached
the working copy). Check before building:

```bash
cd /c/repos/sdr-alert-decoder
for f in $(git ls-files | grep -E '\.(py|kt|md)$'); do
  cmp -s <(tr -d '\r' < "$f") <(tr -d '\r' < "C:/SDR ALERT Decoder/$f") || echo "DIFF $f"; done
```

Strip CR when comparing — the share is CRLF, the repo is not, so a plain diff marks every
line changed and hides the real difference.

**9.7 Only one process can hold the dongle.** A leftover `rtl_sdr.exe` gives the GUI
`usb_open error -3` and zero decodes. Check `tasklist | grep rtl_sdr` before blaming the
decoder.

**9.8 Never chain a `cp` whose destination is an existing source file** with unrelated
work. One did exactly that and overwrote the GUI source; it was recovered from AI_1.

---

## 10. Data and licensing constraints

- **No site register ships, anywhere.** Sensor registers are agency data and not ours to
  redistribute. The Android app has **no** bundled metadata and imports an operator CSV
  (`android/site_metadata_template.csv` documents the format; only `id` is required). The
  installer ships no `Sensors.xlsx`; without one the desktop shows raw values labelled
  *"no site register loaded"*. This was an explicit instruction — do not re-bundle it.
- **Gauge-site names are scrubbed** from the repo (commit `b52a85d`): they are identifiable
  public-safety infrastructure and the repo is public. Don't reintroduce them in comments,
  docs or UI strings.
- **Licensing:** the project is MIT; the bundled `rtl-sdr` tools are **GPLv2**. The
  installer carries `NOTICES.txt` and `rtl-sdr/COPYING` pointing at their source.
- **Not an official data source.** Passive receiver, transmits nothing; a decode can be
  wrong; held values read as fresh. Check the agency's own feed before acting on a reading.

---

## 11. Open work

1. **Run the day-long 3-protocol soak.** `tools/protocol_soak.py` decodes every window
   under all three protocols and logs `raw_hex` alongside the parse. The plan was a rain
   relay through the ERT-A2 with IDs and protocols changed through the day. Nothing has
   been logged yet.
2. **Verify ALERT2 against a real transmitter.** The whole chain is implemented from spec
   and unit-tested end to end, but the RS field polynomial (`0x11D`) and first consecutive
   root (`fcr = 0`) are assumptions. Sweep them against real ALERT2 traffic once the
   ERT-A2 is set to transmit it.
3. **Install the current APK on the phone** — it has the new icon and the tuning controls;
   nothing has been attached to `adb` since those landed.
4. **Watch the Android 4078 recovery rate.** The phone sometimes recovers only 2 of 3
   frames in a burst (the test tolerates this deliberately). If it matters, the phone's
   parameter sweep is the place to look.
5. **Keep `excludes` in the PyInstaller spec non-empty.** It was `[]`, which swept up
   torch (302 MB), transformers, sklearn, pyarrow and llvmlite into a 799 MB build. The
   spec is tracked now precisely so that regression is visible.

---

## 12. Where the knowledge lives

- **Memory palace** — wing `SDR ALERT Decoder` (rooms: `decisions`, `protocols`, `gotchas`,
  `field-tests`, `build-deploy`, `decoder-architecture`, `hardware-plan`, `diary`).
  Query it before asserting anything about past work; several entries are explicit
  *corrections* of earlier entries, so read the dates.
- **`handover-sdi12.md`** (working copy and the share, deliberately not in the repo) —
  the 2026-08-29 radio → SDI-12 handover. Still useful for the rig's behaviour, but it
  opens with a retraction banner: see §9.3 before trusting its bit-12 sections.
  Note the lower-case name — it must **not** be called `handover.md`, because Windows
  treats that as the same file as `HANDOVER.md` and one overwrites the other.
- **`README.md`**, **`android/README.md`**, **`installer/NOTICES.txt`** — user-facing docs.
