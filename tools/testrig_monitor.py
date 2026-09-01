"""Live ALERT monitor tuned for the test rig (IDs 4078/4079/4080).

Streams raw IQ from rtl_sdr, decodes with the AFSK-over-NBFM decoder, and
prints every decode with engineering units. Test-rig sensors are highlighted;
everything else (other stations on the network) is still shown so you can see
the receiver is healthy between the rig's ~5 minute transmissions.

Run:
    python "C:/SDR ALERT Decoder/testrig_monitor.py"
    python "C:/SDR ALERT Decoder/testrig_monitor.py" --gain 30 --freq 151500000

Options:
    --gain G     tuner gain dB (default 40; use ~20-30 if the rig is close
                 and overloads the front end)
    --freq HZ    centre frequency (default 151500000)
    --ppm P      frequency correction
    --save FILE  also write the raw IQ to FILE for offline re-analysis
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(HERE, "src"), HERE):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from scipy import signal as sig                       # noqa: E402
from iq_decoder import IQAlertDecoder                 # noqa: E402
from sensor_database import get_sensor_db             # noqa: E402

FS = 1024000
WIN_S = 3.0
HOP_S = 2.5
TESTRIG = {4078, 4079, 4080}
RIVER_ID, BATT_ID, DI3_ID = 4079, 4080, 4078


def burst_snr(x):
    """Peak spectral excess near the carrier - the signal-health number."""
    xd = sig.decimate(x, 16, ftype="fir")
    f, t, S = sig.spectrogram(xd, 64000, nperseg=2048, noverlap=1024,
                              return_onesided=False)
    f = np.fft.fftshift(f)
    S = np.fft.fftshift(S, axes=0)
    Sdb = 10 * np.log10(S + 1e-12)
    exc = Sdb - np.median(Sdb, axis=1, keepdims=True)
    m = (np.abs(f) < 10000) & (np.abs(f) > 250)
    return float(exc[m, :].max()) if m.any() else 0.0


def measure(freq, ppm, gain, secs=1.6):
    """Capture a short burst at one gain; return (rms_dBFS, clip_pct)."""
    subprocess.run(["taskkill", "/F", "/IM", "rtl_sdr.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    p = subprocess.Popen(
        ["rtl_sdr", "-f", str(freq), "-s", str(FS), "-g", str(gain),
         "-p", str(ppm), "-n", str(int(FS * secs)), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw = p.stdout.read()
    p.wait()
    if len(raw) < FS:
        return None, None
    a = np.frombuffer(raw, dtype=np.uint8)
    clip = float(np.mean((a <= 1) | (a >= 254)) * 100)
    v = a.astype(np.float32) - 127.5
    rms = 20 * np.log10(np.sqrt(np.mean(v * v)) / 128.0 + 1e-9)
    return rms, clip


def auto_gain(freq, ppm):
    """Pick a tuner gain for best ADC headroom.

    NOTE: gain does NOT improve SNR - measured directly (40/44.5/49.6 dB gave
    17.5/16.7/17.8 dB burst SNR on the same signal), because the tuner
    amplifies signal and noise together. What gain *does* control is where the
    signal sits in the 8-bit ADC's range, so this targets a healthy RMS with
    no clipping: too high and a strong local transmitter compresses the front
    end, too low and weak signals sink into quantisation noise."""
    TARGET_HI = -14.0     # dBFS ceiling - leave headroom for the burst
    print("  auto-gain: probing...", flush=True)
    best = None
    # NB: '-g 0' puts rtl_sdr in tuner-AGC mode rather than 0 dB manual gain,
    # so it is excluded - it reads hot and defeats the point of choosing a
    # fixed, repeatable gain.
    for g in (10, 20, 30, 40, 49.6):
        rms, clip = measure(freq, ppm, g)
        if rms is None:
            continue
        ok = clip < 0.05 and rms <= TARGET_HI
        print(f"     gain {g:5} -> RMS {rms:6.1f} dBFS  clip {clip:5.2f}%"
              f"  {'ok' if ok else 'reject'}", flush=True)
        if ok and (best is None or rms > best[1]):
            best = (g, rms)
    if best is None:
        print("     nothing clean - falling back to gain 20", flush=True)
        return "20"
    print(f"  auto-gain: chose {best[0]} dB (RMS {best[1]:.1f} dBFS)",
          flush=True)
    return str(best[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gain", default="40",
                    help="tuner gain dB, or 'auto' to probe and choose")
    ap.add_argument("--freq", default="151500000")
    ap.add_argument("--ppm", default="0")
    ap.add_argument("--save", default=None)
    args = ap.parse_args()

    db = get_sensor_db()
    subprocess.run(["taskkill", "/F", "/IM", "rtl_sdr.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["taskkill", "/F", "/IM", "SDR ALERT Decoder.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

    if str(args.gain).lower() == "auto":
        args.gain = auto_gain(args.freq, args.ppm)

    print("=" * 74)
    print(f"  TEST RIG MONITOR   {args.freq} Hz   gain {args.gain}   "
          f"ppm {args.ppm}")
    print(f"  watching IDs 4079 (river, mm), 4080 (battery), 4078 (DI3)")
    print(f"  rig transmits ~every 5 min - leave this running")
    print("=" * 74, flush=True)

    proc = subprocess.Popen(
        ["rtl_sdr", "-f", args.freq, "-s", str(FS), "-g", args.gain,
         "-p", args.ppm, "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=int(FS * 2))

    save_fh = open(args.save, "wb") if args.save else None
    seen = {}
    hits = []
    dec = IQAlertDecoder(sample_rate=FS, callback=lambda m: hits.append(m))
    win = int(FS * WIN_S)
    hop = int(FS * HOP_S)
    buf = np.zeros(0, dtype=np.complex64)
    t_start = time.time()
    last_beat = 0.0

    try:
        while True:
            raw = proc.stdout.read(int(FS * 2))       # ~1 s of IQ
            if not raw:
                break
            if save_fh:
                save_fh.write(raw)
            u8 = np.frombuffer(raw, dtype=np.uint8)
            # rtl_sdr can return an odd byte count on a short USB read, which
            # would leave the I and Q halves different lengths.
            if u8.size & 1:
                u8 = u8[:-1]
            # Runtime overload guard. A short pre-flight probe cannot see an
            # intermittent burst, so watch for clipping while running: a close
            # transmitter keying up is exactly when the front end compresses,
            # and compression corrupts bit decisions while leaving the tones
            # looking clean.
            clip_pct = float(np.mean((u8 <= 1) | (u8 >= 254)) * 100)
            if clip_pct > 0.10:
                print(f"      [!] CLIPPING {clip_pct:.2f}% - front end "
                      f"overloading at gain {args.gain}; lower it "
                      f"(--gain {max(0, float(args.gain) - 10):.0f})",
                      flush=True)
            a = u8.astype(np.float32) - 127.5
            buf = np.concatenate([buf, (a[0::2] + 1j * a[1::2]).astype(np.complex64)])

            while len(buf) >= win:
                x = buf[:win]
                buf = buf[hop:]
                snr = burst_snr(x)
                hits.clear()
                dec.decode_window(x)
                now = datetime.now().strftime("%H:%M:%S")

                for m in hits:
                    sid, val = m["sensor_id"], m["value"]
                    info = db.get_sensor_info(sid)
                    name = info["site_name"] if info else "NOT in Sensors.xlsx"
                    eng = db.format_decoded_value(sid, val)
                    tag = ">>> TEST RIG" if sid in TESTRIG else "    "
                    ghost = ""
                    # 4078 is one bit from 4079: same value = probable ghost
                    if sid == DI3_ID and seen.get(RIVER_ID) == val:
                        ghost = "  [!] same value as 4079 - probable bit-flip ghost"
                    if sid == RIVER_ID and seen.get(DI3_ID) == val:
                        ghost = "  [!] same value as 4078 - check which is real"
                    seen[sid] = val
                    print(f"{tag} {now}  ID {sid:5d}  raw={val:<5d} = "
                          f"{eng:>10s}   {name}{ghost}", flush=True)
                    if sid == RIVER_ID and val >= 2040:
                        print("         [!] near the 11-bit ceiling (2047 = "
                              "2.047 m) - level may be clipping", flush=True)

                el = time.time() - t_start
                if el - last_beat >= 20:
                    last_beat = el
                    ncar = len(dec.last_candidates)
                    print(f"      {now}  ... listening  ({el/60:.1f} min, "
                          f"burst SNR {snr:4.1f} dB, {ncar} carriers)",
                          flush=True)
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        proc.terminate()
        if save_fh:
            save_fh.close()
            print(f"raw IQ saved to {args.save}")
        print("\n--- last value seen per sensor ---")
        for sid in sorted(seen):
            info = db.get_sensor_info(sid)
            nm = info["site_name"] if info else "NOT in Sensors.xlsx"
            print(f"  ID {sid:5d} = {db.format_decoded_value(sid, seen[sid]):>10s}"
                  f"   {nm}")


if __name__ == "__main__":
    main()
