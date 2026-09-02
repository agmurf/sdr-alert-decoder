# sdr-alert-decoder

Off-air decoders for **ALERT** flood telemetry — a desktop Python
implementation and an Android app that hosts the SDR itself.

Point a cheap RTL-SDR at 151.5 MHz and read exactly what a field station
put on the radio: sensor ID, value, and the four raw bytes it came from.
Whatever a logger *claims* it is sending, this says what actually went out.
Requires RTL-SDR Driver to be installed. https://play.google.com/store/apps/details?id=marto.rtl_tcp_andro&pcampaignid=web_share

---

## Parts

| Part | Role | Notes |
|---|---|---|
| **RTL-SDR (RTL2832U + R820T)** | receiver | 1.024 Msps, gain 40 dB, ppm 0 |
| **Mini-Circuits SBP-150+** | bandpass filter | optional; A/B it with `tools/capture_iq.sh` |
| VHF antenna | 151.5 MHz | a real burst arrives ~55 dB above the noise floor |
| Android phone (USB OTG) | host for the app | the phone does the whole DSP chain, no PC |
| **ELPRO ERT-A2** | the transmitter under test | firmware v1.7, 151.5 MHz, 5 W |

---

## The three ALERT protocols are not interchangeable

This cost real time, so it is stated plainly:

| Protocol | Frame | Error detection | Status here |
|---|---|---|---|
| **ALERT Binary** | 16 fixed marker bits | **none** | decoded, see the warning below |
| **ALERT iFLOWS** (Enhanced) | 2 marker bits + **6-bit CRC** | yes | decoded and field-proven |
| **ALERT2** | different PHY entirely | — | frame generation observed; decode not implemented |

**Physical layer (iFLOWS / Binary):** AFSK, mark 1300.8 Hz / space
2109.4 Hz over narrowband FM, 300 baud, four 10-bit UART bytes carrying a
13-bit sensor ID and an 11-bit value.

An 11-bit value field means a maximum of 2047. With a x1000 scale that is
2.047 m, and there is no way to encode "no reading" — a point that matters
more than it sounds.

### Warning: ALERT Binary has no error detection, and it showed

During a controlled test against a gauge whose true value was known to the
millimetre from an independent SDI-12 record:

    18:50:17   decoded 1946  ->  1.946 m     served 0.666 m     WRONG by 1.28 m
    18:52:53   decoded  666  ->  0.666 m     served 0.666 m     correct

The bad reading carried **16 votes — the same consensus as every correct
one**. Vote count did not separate them, because ALERT Binary carries no
CRC to fail. Enhanced iFLOWS produced 9 river readings across the same
session, all correct.

Two readings is a small sample and should not be read as a rate. But it is
the only decode error observed all day, and it appeared in the one format
with nothing to catch it.

---

## Why it does not guess the protocol

iFLOWS and Enhanced iFLOWS can both claim the same burst. A wrong guess
does not produce an obvious error — it **invents a station**, at a
plausible-looking value. So the apps decode under one declared format, and
the soak tool decodes under all three and records which one produced each
reading, leaving the arbitration visible rather than hidden.

Related hard-won constraints, all encoded in the code:

* **A 6-bit CRC is a 1-in-64 filter, not proof.** Thousands of candidate
  bit positions are tested per burst, so accepting on CRC alone produced
  **33 phantom stations in a capture known to contain 4**. CRC lowers the
  vote bar; it does not replace consensus.
* **Sensor ID 5461 is the 0x55 preamble parsing as a frame**, never a real
  station.
* **Bit 12 of the sensor ID is not part of the ID.** On the rig it appears
  to carry a stale/held flag. Folding it away to recover the station
  discards exactly that signal, so it is kept as its own field.

---

## Code

    src/                 desktop decoders (Python)
      iq_decoder.py        IQ front end, voting, format arbitration
      alert_formats.py     frame parsers: Binary, ASCII, Enhanced iFLOWS + CRC-6
      fsk_pll_decode.py    Gardner timing recovery, symbol phase search
      alert2_decoder.py    ALERT2 front end
      sensor_database.py   optional site labelling (see below)

    tools/
      protocol_soak.py     unattended multi-protocol logger, one row per reading
      correlate.py         joins decodes against ground truth; solves the mapping
      capture_iq.sh        raw IQ capture for A/B filter and gain tests
      live_meter.py        live signal meter

    android/             on-device decoder (Kotlin, RTL-SDR over USB OTG)

The Android app is a line-for-line port of the desktop chain, including
the four-sideband matched filter that gave ~8 dB over the earlier
single-sideband approach.

### Verifying a decode

`correlate.py` joins decoded readings against an independent record of
what the transmitter was actually given, and solves the scaling from
matched pairs:

    raw = 1000.0000 * metres + 0.00     residuals: max 0.00, mean 0.00 (n=7)

It **refuses to solve** when the median match age exceeds 30 minutes.
Polls run every 5 minutes, so nothing legitimate reaches that — but the
two sources can sit in different timezones, and that failure has no
symptom of its own: it would fit a clean straight line through pairs ten
hours apart. Guard the join, not just the fit.

---

## Site metadata is not included

Neither the desktop tools nor the app ship a site database. **Sensor
registers are agency data and are not ours to redistribute.** The
decoders report what came off the air — sensor ID, value, timestamp, raw
bytes — and nothing else.

To label readings, supply your own: `sensor_overrides.json` (test-rig
entries only are included, as a worked example), or **IMPORT SITE CSV** in
the Android app, using `android/site_metadata_template.csv`.

---

## Licence

MIT. See `LICENSE`.

Receive-only tooling for telemetry that is already being broadcast in the
clear. It transmits nothing. Published because the protocol details and
the false-positive traps are poorly documented elsewhere; it is not a
product and carries no warranty. Do not put it in the path of a public
warning without your own verification.
