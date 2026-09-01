"""Live 151.5 MHz burst-SNR meter for peaking the antenna.
Prints the strongest burst SNR near 151.5 once per second so you can move
the antenna and watch the signal come back. Ctrl-C to stop.

Run:  python "C:/SDR ALERT Decoder/live_meter.py"
"""
import subprocess, sys, numpy as np
from scipy import signal as sig

FS = 1024000
CHUNK = FS * 2            # 1 second of interleaved uint8 I/Q

def main():
    # free the device first
    subprocess.run(["taskkill", "//F", "//IM", "rtl_sdr.exe"],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    p = subprocess.Popen(
        ["rtl_sdr", "-f", "151500000", "-s", str(FS), "-g", "40", "-p", "0", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=CHUNK)
    print("Live 151.5 burst meter (Ctrl-C to stop). Move/aim the antenna to peak it.")
    print("  target: 30+ dB = decodable, 38 dB = the strong spot we had\n")
    peak_hold = 0.0
    try:
        while True:
            raw = p.stdout.read(CHUNK)
            if not raw or len(raw) < CHUNK:
                continue
            a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
            x = (a[0::2] + 1j * a[1::2]).astype(np.complex64)
            xd = sig.decimate(x, 16, ftype="fir")
            f, t, S = sig.spectrogram(xd, 64000, nperseg=2048, noverlap=1024,
                                      return_onesided=False)
            f = np.fft.fftshift(f); S = np.fft.fftshift(S, axes=0)
            exc = 10*np.log10(S+1e-12) - np.median(10*np.log10(S+1e-12),
                                                   axis=1, keepdims=True)
            band = (np.abs(f) < 10000) & (np.abs(f) > 250)
            snr = float(exc[band, :].max()) if band.any() else 0.0
            peak_hold = max(peak_hold*0.9, snr)   # decaying peak-hold
            bar = "#" * int(max(0, min(45, snr)))
            flag = "  <-- DECODABLE" if snr >= 30 else ""
            print(f"  {snr:5.1f} dB (peak {peak_hold:4.1f}) |{bar}{flag}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate()

if __name__ == "__main__":
    main()
