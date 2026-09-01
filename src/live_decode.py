"""
Live ALERT iFLOWS decoder — captures IQ from rtl_sdr and decodes the
direct-FSK telemetry in batches, printing decoded stations with values.

This is the working end-to-end decoder: rtl_sdr (IQ) -> IQAlertDecoder
(direct-FSK + Gardner timing recovery) -> ERRTS database lookup.

Usage:
  python live_decode.py [--freq 151500000] [--gain 40] [--window 20]
"""
import sys
import os
import time
import subprocess
import numpy as np

from iq_decoder import IQAlertDecoder

SAMPLE_RATE = 1024000
NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0


def capture_iq(freq, gain, seconds, path):
    """Capture `seconds` of raw IQ to `path` via rtl_sdr."""
    nsamp = int(SAMPLE_RATE * seconds)
    cmd = ['rtl_sdr', '-f', str(int(freq)), '-s', str(SAMPLE_RATE),
           '-g', str(gain), '-n', str(nsamp), path]
    subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)


def load_iq(path):
    raw = np.fromfile(path, dtype=np.uint8).astype(np.float32)
    return ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)).astype(np.complex64)


def main():
    args = sys.argv[1:]
    def opt(name, d, c=str):
        return c(args[args.index(name) + 1]) if name in args else d
    freq = opt('--freq', 151500000, int)
    gain = opt('--gain', 40, int)
    window = opt('--window', 20, int)

    tmp = os.path.join(os.environ.get('TEMP', '/tmp'), '_live_iq.bin')
    decoded = []
    dec = IQAlertDecoder(sample_rate=SAMPLE_RATE,
                         callback=lambda m: decoded.append(m))
    db = dec.sensor_db

    print(f"Live ALERT iFLOWS decoder @ {freq/1e6:.4f} MHz, "
          f"{window}s capture windows. Ctrl-C to stop.\n")
    cycle = 0
    try:
        while True:
            cycle += 1
            print(f"[{time.strftime('%H:%M:%S')}] capturing {window}s...",
                  flush=True)
            capture_iq(freq, gain, window, tmp)
            iq = load_iq(tmp)
            decoded.clear()
            # feed in 0.25 s chunks
            chunk = SAMPLE_RATE // 4
            for i in range(0, len(iq), chunk):
                dec.process_iq(iq[i:i + chunk])
            dec._decode_buffer()
            # report unique stations this cycle
            seen = {}
            for m in decoded:
                seen[(m['sensor_id'], m['value'])] = m
            if seen:
                print(f"  decoded {len(seen)} reading(s):")
                for (sid, val), m in sorted(seen.items()):
                    info = db.get_sensor_info(sid) if db else None
                    if info:
                        eng = db.format_decoded_value(sid, val)
                        print(f"    ERRTS {sid}  {info['sensor_type']:6s} "
                              f"{eng:>12s}  [{info['site_name']}]")
                    else:
                        print(f"    ERRTS {sid} = {val}")
            else:
                print("  (no stations decoded this window)")
            print()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


if __name__ == '__main__':
    main()
