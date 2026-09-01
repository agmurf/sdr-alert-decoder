# ALERT Decoder — Android

On-device ALERT1 flood-telemetry decoder. The phone hosts the RTL-SDR over USB
OTG and does the whole DSP chain itself — no PC, no network, no server.

## What it decodes

**ALERT iFLOWS** as used on 151.5 MHz in NSW: AFSK mark 1300.8 Hz / space
2109.4 Hz over narrowband FM, 300 baud, four 10-bit UART bytes carrying a
13-bit sensor ID and an 11-bit value.

Note there are **three distinct ALERT protocols**, and they are not
interchangeable:

| Protocol | Status | Supported here |
|---|---|---|
| **ALERT Binary** | legacy, still in use | not verified — see below |
| **ALERT iFLOWS** | what this network uses | **yes, decoded and field-proven** |
| **ALERT2** | the next target | no — different PHY, needs new work |

This decoder was developed and verified against iFLOWS traffic only (live network
Creek, the live network, and the 4078 test rig). It has never been tested
against ALERT Binary traffic, so treat Binary support as unproven rather than
assumed.

Ported line-for-line from the desktop Python decoder in `../src`, including the
four-sideband matched filter that gave ~8 dB over the older single-sideband
approach, Gardner timing recovery, brute-force symbol phases, and vote
consensus for false-positive rejection.

Not decoded (yet): ALERT ASCII, ENHANCED BINARY (6-bit CRC), EXTENDED, and
ALERT2 — ALERT2 is a different PHY entirely and needs new work.

## Site metadata is NOT bundled

The app ships **no site database**. Sensor registers are agency data and are
not ours to redistribute, so the decoder reports exactly what came off the
air - sensor id, value and timestamp - and nothing else.

To label readings, an operator imports their own CSV with **IMPORT SITE CSV**.
It is stored privately to the app (internal storage, not shared) and survives
restarts. `site_metadata_template.csv` in this directory documents the format:

```
id,name,type,unit,multiplier
7228,Example Creek D/S Levee,River,m,0.01
7230,Example Creek U/S Levee,Rain,mm,0.254
```

Only `id` is required. With no CSV imported the app shows the **raw** value -
it never invents units it was not given. Common header spellings are accepted
(`id`, `sensor_id`, `errts id`, `address`), column order is free, and extra
columns are ignored, so most agency exports import unchanged.

## Protocols - one at a time

| Selector | Frame format | Notes |
|---|---|---|
| **iFLOWS** (default) | ALERT Binary, markers 01/01/11/11 | what the live network sends |
| **Enhanced iFLOWS** | only byte 0 marked, 6-bit CRC | what the 4078 test rig sends |
| **ALERT2** | 4800 bps, RS + convolutional FEC | separate radio entirely |

They are never run together. Enhanced iFLOWS has only 8 bits of constraint
(2 marker bits plus the CRC) against Binary's 16 fixed bits, so a strong
Binary burst produces systematically CRC-valid Enhanced iFLOWS mis-parses -
measured, they survived vote thresholds of 4, 12 and 35.

## Hardware setup

1. **RTL-SDR** connected to the phone through a **USB OTG adapter**.
2. Install **"RTL2832U driver"** by Martin Marinov from the Play Store (free —
   it is the driver SDR Touch uses).

That driver owns the USB device and serves an `rtl_tcp` stream on localhost,
which is why this app needs no NDK, no libusb and no USB permission handling.
Tapping **START LISTENING** hands off to the driver, then the app connects to
`127.0.0.1:14423` and reads raw IQ.

## Building

```
cd "C:\SDR ALERT Decoder\android"
gradlew.bat :app:assembleDebug
```

`local.properties` must point at the SDK using **forward slashes** — a Windows
path with single backslashes is parsed as escape sequences and fails with
`java.io.IOException: Invalid file path`:

```
sdk.dir=C:/Users/Adam Murphy/AppData/Local/Android/Sdk
```

Install: `adb install -r app/build/outputs/apk/debug/app-debug.apk`

## Verifying the DSP

`gradlew.bat :app:testDebugUnitTest` runs the port against a **real off-air
burst** (`app/src/test/resources/testrig_burst_240k.iq8` — 3 s of the 4078 test
rig captured 2026-08-29 at 72 dB, resampled to 240 ksps as u8 IQ). The desktop
Python decoder resolves that vector to `4079=265` and `4080=414`, and the test
asserts the Kotlin port agrees. It also asserts the decode runs faster than
real time and that random noise yields nothing.

That makes it a port-correctness test rather than a smoke test — worth keeping
green if the DSP is ever touched.

## Sample rate

The app requests **240 kHz**, chosen so the decimation is exact: 240000/20 =
12000 Hz, giving an integer 40 samples/symbol at 300 baud. Non-integer
samples-per-symbol caused timing drift and wrecked vote counts on the desktop
side, so it is worth preserving.

## Gain

Gain does **not** improve SNR — measured on the desktop side, 40/44.5/49.6 dB
produced 17.5/16.7/17.8 dB burst SNR on the same signal, because the tuner
amplifies signal and noise together. What gain controls is headroom in the
8-bit ADC. The app therefore shows a **clipping warning** rather than trying to
chase signal strength: if a nearby transmitter drives the front end into
compression, bit decisions get corrupted while the tones still look clean (this
actually happened during testing and silently corrupted a decode). Default is
25 dB; lower it if the clipping warning appears.

## Sensor list

`app/src/main/assets/sensors.csv` is exported from `Sensors.xlsx` plus
`sensor_overrides.json`, and carries per-sensor unit and multiplier — needed
because the network default for river stage is centimetres while the 4078 test
rig is configured for millimetres. Regenerate it from `../src` if the
spreadsheet changes.
