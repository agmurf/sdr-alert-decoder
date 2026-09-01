"""
Unattended multi-protocol soak logger.

Built for a day-long rig test where the transmitter's IDs and PROTOCOL change
over time. It captures continuously and decodes every window under all three
protocols, recording which one produced each reading - so whatever the rig is
set to at a given hour, the log shows it without anyone having to switch a
setting first.

  python "C:\\SDR ALERT Decoder\\protocol_soak.py"              # until Ctrl-C
  python "C:\\SDR ALERT Decoder\\protocol_soak.py" --hours 8    # stop after 8 h
  python "C:\\SDR ALERT Decoder\\protocol_soak.py" --gain 25

Writes two files next to the app, appending so a restart never loses history:
  soak_log.csv    one row per decoded reading
  soak_events.log human-readable, including burst SNR heartbeats and warnings

WHY IT DECODES UNDER ALL THREE, WHEN THE APPS DELIBERATELY DO NOT: the apps
must not GUESS a protocol, because iFLOWS and Enhanced iFLOWS can both claim
the same burst and a wrong guess silently invents stations. Here every reading
is written with the protocol that produced it and its vote count, so the log
is evidence to read rather than an answer to trust. Cross-talk shows up as the
same burst appearing under two protocols - which is itself worth seeing.

WHAT THE EXTRA COLUMNS ARE FOR
  raw_hex    the four bytes off the wire. Everything else in the row is
             derived from these; when a decode looks wrong this is the only
             field that can settle it. It is also how the other workstream
             proved the rig was re-sending a byte-identical frozen payload.
  format     which frame format actually parsed, as distinct from which
             decoder was asked. Enhanced iFLOWS and BINARY can both claim a
             burst, and this is where that shows up.
  crc_ok     Enhanced iFLOWS carries a 6-bit CRC. Only 1-in-64 as a filter,
             so it is corroboration rather than proof - but its absence on a
             frame that should have it is a real signal.
  id_bit12   bit 12 of the sensor ID. On this rig it appears to carry a
             STALE/HELD flag: eleven frozen frames had it set, live ones
             clear. Folding it away to recover the ID - which the decoders do
             - discards exactly that signal, so it gets its own column here.
  id_folded  the ID with bit 12 removed, i.e. what the station actually is.

DE-DUPLICATION, AND WHY IT IS NOT KEYED ON VALUE
The original keyed on (protocol, id, value) over 30 s, which silently dropped
a burst whenever the rig sent the same value twice - a scheduled report and an
event report carrying an unchanged reading, say. Burst COUNTING is part of
what this test measures, so that is the wrong trade. The decoders already
collapse repeats within a burst; this only guards against the same burst being
decoded in two overlapping windows, so the guard is short and time-based.
"""
import argparse
import csv
import os
import subprocess
import sys
import time
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

from scipy import signal as sig            # noqa: E402
from iq_decoder import IQAlertDecoder       # noqa: E402
from alert2_decoder import ALERT2Decoder    # noqa: E402
from sensor_database import get_sensor_db   # noqa: E402
from iq_sdr_interface import _rtl_tool      # noqa: E402

FS = 1024000
WIN_S = 3.0
CSV_PATH = os.path.join(HERE, "soak_log.csv")
LOG_PATH = os.path.join(HERE, "soak_events.log")
RTL_LOG = os.path.join(HERE, "soak_rtl.log")

COLUMNS = ["timestamp", "protocol", "sensor_id", "id_folded", "id_bit12",
           "raw_value", "engineering", "site", "votes", "format", "crc_ok",
           "raw_hex", "burst_snr_db"]


def note(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def burst_snr(x):
    xd = sig.decimate(x, 16, ftype="fir")
    f, t, S = sig.spectrogram(xd, 64000, nperseg=2048, noverlap=1024,
                              return_onesided=False)
    f = np.fft.fftshift(f)
    S = np.fft.fftshift(S, axes=0)
    db = 10 * np.log10(S + 1e-12)
    exc = db - np.median(db, axis=1, keepdims=True)
    m = (np.abs(f) < 10000) & (np.abs(f) > 250)
    return float(exc[m, :].max()) if m.any() else 0.0


def open_csv():
    """Append to soak_log.csv, rotating aside any file with an older header.

    The column set changed when raw_hex / bit-12 were added. Appending new
    rows under an old header would silently misalign every field, which is
    worse than losing the continuity, so an incompatible file is renamed
    rather than written into.
    """
    if os.path.exists(CSV_PATH):
        try:
            with open(CSV_PATH, newline="", encoding="utf-8") as fh:
                first = next(csv.reader(fh), [])
        except Exception:
            first = []
        if first and first != COLUMNS:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            old = CSV_PATH.replace(".csv", f".pre{stamp}.csv")
            os.rename(CSV_PATH, old)
            note(f"existing soak_log.csv had the older column set; "
                 f"rotated to {os.path.basename(old)}")
    new = not os.path.exists(CSV_PATH)
    fh = open(CSV_PATH, "a", newline="", encoding="utf-8")
    w = csv.writer(fh)
    if new:
        w.writerow(COLUMNS)
        fh.flush()
    return fh, w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=int, default=151_500_000)
    ap.add_argument("--gain", default="40")
    ap.add_argument("--ppm", default="0")
    ap.add_argument("--hours", type=float, default=0.0, help="0 = run forever")
    ap.add_argument("--dedup", type=float, default=8.0,
                    help="seconds; guards against one burst being decoded in "
                         "two overlapping windows. Keep it well below the "
                         "rig's transmit interval or real bursts are lost.")
    args = ap.parse_args()

    db = get_sensor_db()
    for img in ("rtl_sdr.exe", "SDR ALERT Decoder.exe"):
        subprocess.run(["taskkill", "/F", "/IM", img],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

    csv_fh, writer = open_csv()

    note(f"soak start  freq={args.freq} gain={args.gain} ppm={args.ppm} "
         f"dedup={args.dedup}s")
    note(f"  logging to {CSV_PATH}")

    # One decoder per protocol; each window is offered to all three. Their
    # own dedup windows are pulled in to match ours, so successive bursts
    # carrying the SAME value are still reported individually.
    decoders = [
        ("iFLOWS", IQAlertDecoder(sample_rate=FS, formats=("BINARY",),
                                  dedup_seconds=args.dedup)),
        ("Enhanced iFLOWS", IQAlertDecoder(sample_rate=FS,
                                           formats=("ENHANCED_IFLOWS",),
                                           dedup_seconds=args.dedup)),
        ("ALERT2", ALERT2Decoder(sample_rate=FS)),
    ]

    # rtl_sdr reports tuner setup, PLL lock and -- crucially -- dropped
    # samples on stderr. Discarding it makes "we heard nothing" and "we heard
    # nothing AND the dongle was shedding buffers" look identical, which are
    # different faults with different fixes. This project has already paid
    # once for dropping evidence down a silent path.
    rtl_err = open(RTL_LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    rtl_err.write(f"\n===== rtl_sdr start {datetime.now():%Y-%m-%d %H:%M:%S} "
                  f"freq={args.freq} gain={args.gain} ppm={args.ppm} =====\n")
    proc = subprocess.Popen(
        [_rtl_tool("rtl_sdr"), "-f", str(args.freq), "-s", str(FS),
         "-g", str(args.gain), "-p", str(args.ppm), "-"],
        stdout=subprocess.PIPE, stderr=rtl_err, bufsize=FS * 2)

    deadline = time.time() + args.hours * 3600 if args.hours else None
    buf = np.zeros(0, dtype=np.complex64)
    need = int(FS * WIN_S)
    last_beat = 0.0
    last_row = time.time()
    seen = {}
    rows = 0
    try:
        while True:
            if deadline and time.time() > deadline:
                note("requested duration reached")
                break
            raw = proc.stdout.read(FS * 2)
            if not raw:
                note("IQ stream ended - is the dongle still attached?")
                break
            u8 = np.frombuffer(raw, dtype=np.uint8)
            if u8.size & 1:
                u8 = u8[:-1]
            clip = float(np.mean((u8 <= 1) | (u8 >= 254)) * 100)
            if clip > 0.1:
                note(f"[!] CLIPPING {clip:.2f}% - front end overloading, "
                     f"lower --gain")
            a = u8.astype(np.float32) - 127.5
            buf = np.concatenate([buf,
                                  (a[0::2] + 1j * a[1::2]).astype(np.complex64)])

            while len(buf) >= need:
                win = buf[:need]
                buf = buf[need:]
                snr = burst_snr(win)
                stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
                for label, dec in decoders:
                    try:
                        got = []
                        dec.callback = lambda m, _g=got: _g.append(m)
                        dec.decode_window(win)
                    except Exception as e:                      # keep soaking
                        note(f"  {label} decode error: {e}")
                        continue
                    for m in got:
                        sid = m.get("sensor_id")
                        val = m.get("value")

                        # Bit 12 is folded away by the decoders to recover the
                        # station ID. On this rig it appears to mean HELD, so
                        # keep both halves rather than the folded ID alone.
                        bit12 = ""
                        folded = sid
                        if isinstance(sid, int):
                            bit12 = (sid >> 12) & 1
                            folded = sid & 0xFFF

                        raw_b = m.get("bytes")
                        raw_hex = (" ".join(f"{b:02X}" for b in raw_b)
                                   if raw_b else "")

                        site = ""
                        eng = str(val)
                        info = (db.get_sensor_info(folded)
                                if isinstance(folded, int) else None)
                        if info:
                            site = info["site_name"]
                            eng = db.format_decoded_value(folded, val)

                        # Time-based only: two windows can straddle one burst,
                        # but a genuine repeat of the same value in a LATER
                        # burst must still be recorded.
                        key = (label, sid, str(val))
                        if seen.get(key, 0) + args.dedup > time.time():
                            continue
                        seen[key] = time.time()

                        writer.writerow([
                            stamp, label, sid, folded, bit12, val, eng, site,
                            m.get("match_count", ""),
                            m.get("format", ""),
                            m.get("crc_ok", ""),
                            raw_hex, f"{snr:.1f}"])
                        csv_fh.flush()
                        rows += 1
                        last_row = time.time()
                        flag = "  STALE(bit12)" if bit12 == 1 else ""
                        note(f"  {label:16s} {folded} = {val}  {eng}  "
                             f"[{site or 'not in DB'}]  SNR {snr:.0f} dB"
                             f"  {raw_hex}{flag}")
                el = time.time()
                if el - last_beat > 300:
                    last_beat = el
                    quiet = el - last_row
                    # The rig's schedule is every 5 minutes exactly, so a
                    # heartbeat with no rows behind it is a MISSED burst, not
                    # merely an uneventful window. Say so, rather than
                    # printing something reassuring.
                    if rows == 0 or quiet > 360:
                        note(f"  [!] nothing decoded for {quiet / 60:.0f} min "
                             f"-- the rig transmits every 5 min, so that is "
                             f"~{int(quiet // 300)} missed transmission(s). "
                             f"Burst SNR {snr:.0f} dB, {rows} rows total")
                    else:
                        note(f"  ... listening, burst SNR {snr:.0f} dB, "
                             f"{rows} rows so far")
    except KeyboardInterrupt:
        note("stopped by user")
    finally:
        proc.terminate()
        csv_fh.close()
        try:
            rtl_err.close()
        except Exception:
            pass
        note(f"soak end - {rows} rows written")


if __name__ == "__main__":
    main()
