"""
Batch characterizer: measure every clean burst across many recordings and
aggregate. The FSK *shift* (tone separation) is invariant to receiver
tuning, so a real protocol parameter clusters across independent files.
"""
import os, sys, glob
import numpy as np
from scipy import signal as sig
from signal_characterizer import load_audio, measure

def bursts_in(a, sr, nwin=10):
    nper = 2048
    if len(a) < nper * 2:
        return []
    f, t, S = sig.spectrogram(a, sr, nperseg=nper, noverlap=nper // 2)
    band = (f >= 300) & (f <= 6000)
    score = (S[band, :].mean(axis=0) / (S.mean(axis=0) + 1e-12)) * \
            np.sqrt(S[band, :].mean(axis=0) + 1e-12)
    idx = np.argsort(score)[::-1]
    picks, used = [], []
    for i in idx:
        if all(abs(t[i] - u) > 0.8 for u in used):
            used.append(t[i]); picks.append(t[i])
        if len(picks) >= nwin:
            break
    out = []
    for tc in picks:
        s = int(max(0, (tc - 0.5) * sr)); e = int(min(len(a), (tc + 0.5) * sr))
        out.append((s, e))
    return out

def run(paths, rate=None, tune=0.0, iq='u8', minq=0.35):
    allm = []
    for p in paths:
        try:
            a, sr = load_audio(p, rate=rate, tune=tune, iq_dtype=iq)
        except Exception as ex:
            print(f"  skip {os.path.basename(p)}: {ex}"); continue
        good = 0
        for s, e in bursts_in(a, sr):
            m = measure(a[s:e], sr)
            if m and m['quality'] >= minq:
                m['file'] = os.path.basename(p)
                allm.append(m); good += 1
        print(f"  {os.path.basename(p):44s} {good:2d} clean bursts")
    return allm

def summarize(allm):
    if not allm:
        print("\nNo clean bursts found anywhere."); return
    shifts = np.array([m['shift'] for m in allm])
    marks  = np.array([m['mark'] for m in allm])
    spaces = np.array([m['space'] for m in allm])
    bauds  = np.array([m['baud'] for m in allm if m['baud']])
    print(f"\n=== AGGREGATE over {len(allm)} clean bursts "
          f"from {len(set(m['file'] for m in allm))} files ===")
    # shift histogram (tuning-invariant -> the key statistic)
    print("\nFSK SHIFT distribution (Hz)  [tuning-invariant]:")
    h, edges = np.histogram(shifts, bins=np.arange(0, 1200, 40))
    for c, n in zip((edges[:-1]+edges[1:])/2, h):
        if n: print(f"  {c:5.0f} Hz | {'#'*n} {n}")
    # dominant shift cluster
    binw = 40
    hh, ed = np.histogram(shifts, bins=np.arange(0, 1200, binw))
    pk = (ed[:-1]+ed[1:])[ : ][np.argmax(hh)]/2 if False else \
         ((ed[np.argmax(hh)]+ed[np.argmax(hh)+1])/2)
    near = shifts[np.abs(shifts - pk) < binw]
    print(f"\nDominant shift cluster: {np.median(near):.0f} Hz "
          f"(+/- {near.std():.0f}), {len(near)}/{len(shifts)} bursts")
    print(f"Median mark={np.median(marks):.0f}  space={np.median(spaces):.0f}")
    if len(bauds):
        hb, eb = np.histogram(bauds, bins=[80,130,180,250,350,500,800,1400,2200])
        print("Baud distribution:", dict((f'{eb[i]:.0f}-{eb[i+1]:.0f}', int(hb[i]))
                                          for i in range(len(hb)) if hb[i]))
    return np.median(near)

if __name__ == '__main__':
    args = sys.argv[1:]
    def opt(n,d=None,c=str):
        return c(args[args.index(n)+1]) if n in args else d
    rate=opt('--rate',None,float); tune=opt('--tune',0.0,float); iq=opt('--iq','u8')
    folders=[a for a in args if not a.startswith('--') and
             a not in {str(rate),str(tune),iq} and os.path.exists(a)]
    paths=[]
    for fp in folders:
        if os.path.isdir(fp): paths += sorted(glob.glob(os.path.join(fp,'*.wav')))
        else: paths.append(fp)
    print(f"Characterizing {len(paths)} files...")
    allm = run(paths, rate=rate, tune=tune, iq=iq)
    summarize(allm)
