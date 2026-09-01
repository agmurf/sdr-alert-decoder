"""
Signal Characterizer — measure the ACTUAL modulation parameters of a
recording instead of assuming them.

This is the diagnostic step that has been missing from the whole project:
every decode attempt assumed 2133/1920 Hz, 300 baud. This tool MEASURES
the real two-FSK tones, the frequency shift, the baud rate and the FM
deviation directly off your own signal (the way Universal Radio Hacker
does), so the decode parameters stop being guesswork.

Handles:
  * WAV files that are already FM-demodulated audio (rtl_fm / SDR# NBFM)
  * Raw IQ files (.bin/.iq/.raw 8-bit unsigned I/Q from rtl_sdr;
    .cs16 16-bit signed) with --rate and optional --tune offset; these
    are FM-discriminated internally first.

Method (robust, interpretable):
  1. Locate the strongest/cleanest burst (energy + FSK-band purity).
  2. Bandpass the audio, take the analytic signal, get instantaneous
     frequency in Hz.
  3. TONES: histogram the steady-state IF samples (low |dIF/dt|); the two
     dominant, well-separated modes are the mark/space tones.
  4. SHIFT = |mark - space|.  Audio centre = (mark+space)/2.
  5. BAUD: slice IF to bipolar symbols at the mid-frequency, measure the
     inter-transition intervals; the smallest robust interval is one bit
     period -> baud = sr / bit_period.  Cross-checked with the symbol
     autocorrelation main-lobe width.
  6. QUALITY: bimodality / separation score so a degraded recording is
     flagged as unmeasurable rather than returning a confident wrong
     answer.

CLI:
  python signal_characterizer.py FILE.wav
  python signal_characterizer.py capture.bin --rate 1024000 --tune -50396
"""
import os
import sys
import numpy as np
from scipy import signal as sig
from scipy.io import wavfile


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def load_audio(path, rate=None, tune=0.0, iq_dtype='u8'):
    """
    Return (audio_float, sr) — FM-demodulated audio.

    WAV -> assumed already FM-demod audio.
    IQ  -> FM discriminated, decimated to ~32 kHz audio.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == '.wav':
        sr, a = wavfile.read(path)
        if a.ndim > 1:
            a = a[:, 0]
        a = _to_float(a)
        return a, sr

    # raw IQ
    if rate is None:
        raise ValueError("raw IQ requires --rate")
    fs = int(rate)
    if iq_dtype == 'u8':
        raw = np.fromfile(path, dtype=np.uint8).astype(np.float32)
        x = (raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)
    elif iq_dtype == 's16':
        raw = np.fromfile(path, dtype=np.int16).astype(np.float32)
        x = raw[0::2] + 1j * raw[1::2]
    else:
        raise ValueError("iq_dtype must be u8 or s16")

    if tune:
        n = np.arange(len(x))
        x = x * np.exp(-1j * 2 * np.pi * float(tune) / fs * n)
    # narrowband channel filter ~ +/-8 kHz, then decimate to ~32 kHz
    b = sig.firwin(513, 8000 / (fs / 2))
    x = sig.lfilter(b, 1, x)
    dec = max(1, fs // 32000)
    x = x[::dec]
    srd = fs // dec
    fm = np.angle(x[1:] * np.conj(x[:-1]))      # FM discriminator
    fm = fm - np.median(fm)
    return fm.astype(np.float32), srd


def _to_float(a):
    if a.dtype == np.int16:
        return a.astype(np.float32) / 32768.0
    if a.dtype == np.int32:
        return a.astype(np.float32) / 2147483648.0
    if a.dtype == np.uint8:
        return (a.astype(np.float32) - 128) / 128.0
    return a.astype(np.float32)


# ----------------------------------------------------------------------
# Burst location
# ----------------------------------------------------------------------

def find_best_burst(a, sr, want=1.0):
    """
    Return (start, end) of the cleanest ~`want`-second region, ranked by
    FSK-band purity (power in 300-4000 Hz vs total) so we characterise an
    actual data burst, not background hiss.
    """
    nper = 2048
    if len(a) < nper * 2:
        return 0, len(a)
    f, t, S = sig.spectrogram(a, sr, nperseg=nper, noverlap=nper // 2)
    band = (f >= 300) & (f <= 4000)
    purity = S[band, :].mean(axis=0) / (S.mean(axis=0) + 1e-12)
    # also weight by absolute in-band power so silence doesn't win
    inband = S[band, :].mean(axis=0)
    score = purity * np.sqrt(inband + 1e-12)
    # smooth over ~want seconds
    wcols = max(1, int(want / (t[1] - t[0]))) if len(t) > 1 else 1
    sm = np.convolve(score, np.ones(wcols) / wcols, mode='same')
    j = int(np.argmax(sm))
    tc = t[j]
    s = int(max(0, (tc - want / 2) * sr))
    e = int(min(len(a), (tc + want / 2) * sr))
    return s, e


# ----------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------

def measure(seg, sr, fmin=300, fmax=5000):
    """Measure FSK tones, shift, baud, deviation, and a quality score.

    TONES are found from a high-resolution power spectrum: a continuous
    2-FSK signal puts two well-defined spectral lines at the mark/space
    frequencies regardless of modulation index, so this is robust even
    for narrow-shift FSK (shift/baud < 1) where per-symbol tone
    discrimination is ambiguous.

    BAUD is then found by running matched filters at the measured tones to
    get a soft symbol-decision track, and measuring the inter-transition
    interval (refined to integer multiples of one bit period).
    """
    if len(seg) < sr * 0.1:
        return None
    seg = seg - np.mean(seg)

    # --- TONES via Welch PSD (fine resolution, well averaged) ---
    nper = min(8192, len(seg))
    f, P = sig.welch(seg, sr, nperseg=nper, noverlap=nper // 2)
    band = (f >= fmin) & (f <= fmax)
    fb, Pb = f[band], P[band]
    if len(fb) < 8:
        return None
    Ps = np.convolve(Pb, np.ones(3) / 3, mode='same')
    order = np.argsort(Ps)[::-1]
    peaks = []
    for i in order:
        if all(abs(fb[i] - p) > 100 for p in peaks):
            peaks.append(fb[i])
        if len(peaks) == 2:
            break
    if len(peaks) < 2:
        return None
    space_f, mark_f = sorted(peaks)
    mid = (mark_f + space_f) / 2
    shift = mark_f - space_f

    # quality: the two tone lines should dominate the in-band spectrum
    p_mark = Ps[np.argmin(np.abs(fb - mark_f))]
    p_space = Ps[np.argmin(np.abs(fb - space_f))]
    tone_power = p_mark + p_space
    total = Ps.sum() + 1e-12
    dominance = tone_power / total * len(Ps)   # >1 means tones stand out
    balance = min(p_mark, p_space) / (max(p_mark, p_space) + 1e-12)
    quality = float(max(0.0, min(1.0, (dominance / 30.0) * balance)))

    # --- BAUD via matched-filter symbol track at the measured tones ---
    # window ~ resolves up to ~1500 baud; long enough to separate tones
    W = max(16, int(sr / 700))
    t = np.arange(W) / sr
    mk = np.exp(-2j * np.pi * mark_f * t)
    sp = np.exp(-2j * np.pi * space_f * t)
    cm = np.abs(sig.fftconvolve(seg, np.conj(mk[::-1]), mode='valid'))
    cs = np.abs(sig.fftconvolve(seg, np.conj(sp[::-1]), mode='valid'))
    d = cm - cs
    sym = np.sign(d)
    sym[sym == 0] = 1
    tr = np.where(np.diff(sym) != 0)[0]
    baud = None
    if len(tr) > 6:
        iv = np.diff(tr).astype(float)
        iv = iv[(iv > sr / 2000) & (iv < sr / 80)]   # 80..2000 baud
        if len(iv) > 6:
            bit = np.percentile(iv, 20)
            for _ in range(4):
                k = np.round(iv / bit)
                k[k < 1] = 1
                bit = np.median(iv / k)
            baud = sr / bit

    # independent baud cross-check: spectral line of the rectified
    # transition train (energy recurs at the symbol rate)
    baud_sl = None
    dd = np.abs(np.diff(d))
    dd = dd - dd.mean()
    if len(dd) > 64:
        F = np.abs(np.fft.rfft(dd * np.hanning(len(dd))))
        ff = np.fft.rfftfreq(len(dd), 1 / sr)
        bb = (ff >= 80) & (ff <= 2000)
        if np.any(bb):
            baud_sl = float(ff[bb][np.argmax(F[bb])])

    return {
        'mark': float(mark_f),
        'space': float(space_f),
        'shift': float(shift),
        'center': float(mid),
        'baud': float(baud) if baud else None,
        'baud_specline': baud_sl,
        'deviation_est': float(shift / 2),
        'quality': quality,
        'n_samples': int(len(seg)),
    }


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------

def characterize(path, rate=None, tune=0.0, iq_dtype='u8', want=1.0,
                 verbose=True):
    a, sr = load_audio(path, rate=rate, tune=tune, iq_dtype=iq_dtype)
    dur = len(a) / sr
    s, e = find_best_burst(a, sr, want=want)
    seg = a[s:e]
    m = measure(seg, sr)
    if verbose:
        print(f"\n=== {os.path.basename(path)} ===")
        print(f"  {dur:.1f}s @ {sr} Hz | analysed burst @ {s/sr:.1f}-{e/sr:.1f}s")
        if not m:
            print("  UNMEASURABLE - no clean two-FSK structure in this "
                  "recording (too degraded / not 2-FSK here).")
            return None
        conf = ("STRONG" if m['quality'] > 0.5 else
                "WEAK" if m['quality'] > 0.2 else "POOR")
        print(f"  Tones : mark {m['mark']:.0f} Hz  /  space {m['space']:.0f} Hz")
        print(f"  Shift : {m['shift']:.0f} Hz   (centre {m['center']:.0f} Hz)")
        bd = f"{m['baud']:.0f}" if m['baud'] else "?"
        bs = f"{m['baud_specline']:.0f}" if m['baud_specline'] else "?"
        print(f"  Baud  : {bd}  (spectral-line cross-check {bs})")
        print(f"  Quality: {conf}  (tone dominance {m['quality']:.2f}, "
              f"n={m['n_samples']})")
        print(f"  vs assumed 2133/1920/300: shift delta {m['shift']-213:+.0f} Hz, "
              f"centre delta {m['center']-2026:+.0f} Hz")
    return m


def consistency_scan(path, rate=None, tune=0.0, iq_dtype='u8', nwin=6):
    """
    Measure several bursts in one file and report whether they AGREE on
    tones/shift/baud. A real single-protocol signal is internally
    consistent; scattered values mean noise or mixed/degraded content.
    """
    a, sr = load_audio(path, rate=rate, tune=tune, iq_dtype=iq_dtype)
    nper = 2048
    f, t, S = sig.spectrogram(a, sr, nperseg=nper, noverlap=nper // 2)
    band = (f >= 300) & (f <= 4000)
    purity = S[band, :].mean(axis=0) / (S.mean(axis=0) + 1e-12)
    score = purity * np.sqrt(S[band, :].mean(axis=0) + 1e-12)
    # pick the nwin strongest, well-separated time centres
    idx = np.argsort(score)[::-1]
    picks, used = [], []
    for i in idx:
        if all(abs(t[i] - u) > 0.8 for u in used):
            used.append(t[i]); picks.append(i)
        if len(picks) >= nwin:
            break
    res = []
    for i in picks:
        s = int(max(0, (t[i] - 0.5) * sr)); e = int(min(len(a), (t[i] + 0.5) * sr))
        m = measure(a[s:e], sr)
        if m and m['quality'] > 0.25:
            res.append(m)
    print(f"\n=== {os.path.basename(path)}  (consistency over {len(res)} "
          f"good bursts) ===")
    if len(res) < 2:
        print("  Not enough clean bursts to establish consistency.")
        return None
    marks = np.array([r['mark'] for r in res])
    spaces = np.array([r['space'] for r in res])
    shifts = np.array([r['shift'] for r in res])
    bauds = np.array([r['baud'] for r in res if r['baud']])
    print(f"  mark  : {np.median(marks):.0f} Hz  (spread {marks.std():.0f})")
    print(f"  space : {np.median(spaces):.0f} Hz  (spread {spaces.std():.0f})")
    print(f"  shift : {np.median(shifts):.0f} Hz  (spread {shifts.std():.0f})")
    if len(bauds):
        print(f"  baud  : {np.median(bauds):.0f}    (spread {bauds.std():.0f})")
    consistent = shifts.std() < 40 and marks.std() < 60
    print(f"  --> {'CONSISTENT (real parameter)' if consistent else 'SCATTERED (noise/degraded/mixed)'}")
    return res


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: signal_characterizer.py FILE [--rate R] [--tune Hz] "
              "[--iq u8|s16] [--want SECONDS] [--scan]")
        return
    def opt(name, default=None, cast=str):
        if name in args:
            return cast(args[args.index(name) + 1])
        return default
    rate = opt('--rate', None, float)
    tune = opt('--tune', 0.0, float)
    iqd = opt('--iq', 'u8', str)
    want = opt('--want', 1.0, float)
    scan = '--scan' in args
    # files = positional args that are not option flags or their values
    opt_vals = set()
    for fl in ('--rate', '--tune', '--iq', '--want'):
        if fl in args:
            opt_vals.add(args[args.index(fl) + 1])
    files = [a for a in args
             if not a.startswith('--') and a not in opt_vals]
    for f in files:
        try:
            if scan:
                consistency_scan(f, rate=rate, tune=tune, iq_dtype=iqd)
            else:
                characterize(f, rate=rate, tune=tune, iq_dtype=iqd, want=want)
        except Exception as ex:
            print(f"\n=== {os.path.basename(f)} ===\n  ERROR: {ex}")


if __name__ == '__main__':
    main()
