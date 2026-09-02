"""
Direct-FSK ALERT decoder with Gardner timing recovery.

The 151.5 MHz iFLOWS signal is NARROWBAND DIRECT FSK (the RF carrier shifts
~1000 Hz between mark and space at ~300 baud), NOT audio-AFSK at 2133/1920.
So the correct receiver is:

  1. Tune the complex IQ to the burst carrier centre.
  2. Two matched filters at +/-shift/2 -> soft decision d = |mark| - |space|.
  3. GARDNER timing-recovery loop locks the bit clock and samples d at the
     true symbol centres (this is what was missing - crude phase-sweep gave
     only 14/16 framing; a tracking loop gets clean bits).
  4. Slice -> bits -> ALERT 4x10-bit UART frame -> 13-bit ID + 11-bit value.
  5. Validate against the ERRTS database; a real station emits several
     same-site sensors per burst, which is the ground-truth-free check.
"""
import numpy as np
from scipy import signal as sig

BAUD = 300
FIXED = {0:0,7:1,8:0,9:1, 10:0,17:1,18:0,19:1,
         20:0,27:1,28:1,29:1, 30:0,37:1,38:1,39:1}


# ----------------------------------------------------------------------
# Gardner timing recovery
# ----------------------------------------------------------------------

def gardner(d, sps, Kp=0.02, Ki=0.001):
    """
    Gardner timing-recovery on a real soft-decision stream `d` oversampled
    at `sps` samples/symbol. Returns the soft values sampled at the
    recovered symbol centres.

    Gardner TED (no carrier-phase needed):  e = mid * (curr - prev)
    where prev/curr are one symbol apart and mid is halfway between.
    A PI loop steers the symbol period to drive e -> 0.
    """
    n = len(d)
    if n < sps * 4:
        return np.array([])

    def interp(pos):
        k = int(np.floor(pos))
        if k < 0 or k + 1 >= n:
            return 0.0
        f = pos - k
        return d[k] * (1 - f) + d[k + 1] * f

    out = []
    period = float(sps)
    p = float(sps)          # current symbol-centre position
    prev = interp(p - period)
    integ = 0.0
    while p < n - 1:
        curr = interp(p)
        mid = interp(p - period / 2.0)
        e = mid * (curr - prev)
        # normalise error magnitude so gains are signal-independent
        scale = (abs(curr) + abs(prev) + 1e-9)
        e = e / scale
        integ += e
        period_correction = Kp * e + Ki * integ
        out.append(curr)
        prev = curr
        # advance one symbol, nudged by the timing error
        p += period - period_correction * period
        # keep period sane
        period = min(sps * 1.05, max(sps * 0.95, period))
    return np.array(out)


# ----------------------------------------------------------------------
# Complex FSK front-end
# ----------------------------------------------------------------------

def predecimate(iq, fs, mid_rate=64000):
    """
    Decimate the full-rate IQ to an intermediate rate (default 64 kHz) ONCE.
    All per-carrier work then happens on 16x less data. 64 kHz keeps
    +/-32 kHz, covering every iFLOWS carrier candidate.
    """
    f = fs // mid_rate
    s = iq
    sr = fs
    while f >= 8:
        s = sig.decimate(s, 8, ftype='fir')
        sr //= 8
        f //= 8
    if f >= 2:
        s = sig.decimate(s, f, ftype='fir')
        sr //= f
    return s.astype(np.complex64), sr


def channelize(bb, sr, center, out_rate=6000):
    """
    Tune the (pre-decimated) baseband to `center` and resample to EXACTLY
    out_rate (so samples/symbol is an integer -> no timing drift, which is
    what wrecked vote counts when the rate was non-integer). Cheap because
    it runs on the pre-decimated 64 kHz stream, not the full rate.
    """
    from math import gcd
    n = np.arange(len(bb))
    s = bb * np.exp(-1j * 2 * np.pi * center / sr * n)
    g = gcd(int(sr), int(out_rate))
    up = out_rate // g
    down = int(sr) // g
    s = sig.resample_poly(s, up, down)
    return s.astype(np.complex64), out_rate


def soft_decision(baseband, sr, shift, baud=BAUD):
    """Matched-filter the baseband at +/-shift/2 -> soft decision (cheap).
    `baud` lets the caller sweep symbol rates (some ALERT stations transmit
    at 200 baud, not just 300)."""
    spb = int(round(sr / baud))
    t = np.arange(spb) / sr
    mk = np.exp(-2j * np.pi * (+shift / 2) * t)
    sp = np.exp(-2j * np.pi * (-shift / 2) * t)
    cm = np.abs(sig.fftconvolve(baseband, np.conj(mk[::-1]), mode='valid'))
    cs = np.abs(sig.fftconvolve(baseband, np.conj(sp[::-1]), mode='valid'))
    return cm - cs, spb


# AU iFLOWS AFSK audio tones (measured 2026-07-11 from a decoding live
# burst: the FM spectrum shows sideband PAIRS at exactly these offsets and
# no carrier line). The signal is CCITT-style AFSK over NBFM, NOT direct
# carrier FSK - the legacy soft_decision path only ever worked by
# accidentally detecting one sideband as OOK (needs ~10 dB more SNR).
AFSK_F1 = 1300.8   # mark tone (also the idle tone)
AFSK_F2 = 2109.4   # space tone


def afsk_soft_decision(baseband, sr, baud=BAUD, f1=AFSK_F1, f2=AFSK_F2):
    """AFSK-over-NBFM soft decision on complex baseband centred on the FM
    CARRIER. An audio tone f puts RF sideband lines at BOTH +/-f, so
    matched-filter all four lines and combine each tone's pair
    non-coherently:
        d = (|MF(+f1)|+|MF(-f1)|) - (|MF(+f2)|+|MF(-f2)|)
    Uses the full signal energy (~6 dB better than the legacy path) and
    avoids the FM-discriminator threshold. Needs sr >= ~6 kHz so both
    tones fit; use out_rate=12000 when channelizing."""
    spb = int(round(sr / baud))
    t = np.arange(spb) / sr
    def _mf(fo):
        k = np.exp(-2j * np.pi * fo * t)
        return np.abs(sig.fftconvolve(baseband, np.conj(k[::-1]),
                                      mode='valid'))
    d = (_mf(+f1) + _mf(-f1)) - (_mf(+f2) + _mf(-f2))
    return d, spb


def fsk_soft(iq, fs, center, shift, out_rate=6000):
    """Convenience: predecimate + channelize + soft_decision in one call."""
    mid, msr = predecimate(iq, fs)
    bb, sr = channelize(mid, msr, center, out_rate)
    d, spb = soft_decision(bb, sr, shift)
    return d, sr, spb


# ----------------------------------------------------------------------
# ALERT framing
# ----------------------------------------------------------------------

def parse_frame(bits40):
    w = []
    for k in range(4):
        fr = bits40[k*10:(k+1)*10]
        b = 0
        for j in range(8):
            if fr[1 + j]:
                b |= (1 << j)
        w.append(b)
    sid = (w[0] & 63) + 64 * (w[1] & 63) + 4096 * (w[2] & 1)
    val = (w[3] & 63) * 32 + ((w[2] & 62) >> 1)
    return sid, val


# The 8 UART start/stop positions - structure every ALERT-framed message has
# regardless of which format-marker convention the transmitter uses.
UART_BITS = {0: 0, 9: 1, 10: 0, 19: 1, 20: 0, 29: 1, 30: 0, 39: 1}
# The 8 format-marker positions (the rest of FIXED). Some loggers - e.g. the
# 4078 test rig - use a different marker convention while keeping the standard
# ID/value packing, so these can be waived for whitelisted IDs.
MARKER_BITS = {k: v for k, v in FIXED.items() if k not in UART_BITS}


def frames_from_symbols(soft, valid_ids=None, min_fixed=15,
                        uart_only_ids=None):
    """Slice recovered soft symbols (both polarities) into ALERT frames.

    `uart_only_ids`: IDs allowed to bypass the format-marker check as long as
    all 8 UART start/stop bits are correct. Used for transmitters that pack ID
    and value the standard way but set the two format-marker bits per byte
    differently. Gated to an explicit ID set so relaxing the check cannot
    open the floodgates to noise (which is what min_fixed=16 is protecting
    against on the open band)."""
    hits = []
    for inv in (False, True):
        bits = [(1 if (-v if inv else v) > 0 else 0) for v in soft]
        for p in range(len(bits) - 40 + 1):
            fr = bits[p:p + 40]
            fixed = sum(1 for k, fv in FIXED.items() if fr[k] == fv)
            if fixed < min_fixed:
                if not uart_only_ids:
                    continue
                # UART structure must still be perfect
                if any(fr[k] != v for k, v in UART_BITS.items()):
                    continue
                sid_try, val_try = parse_frame(fr)
                if sid_try not in uart_only_ids and \
                        (sid_try & 0xFFF) not in uart_only_ids:
                    continue
            sid, val = parse_frame(fr)
            if valid_ids is not None and sid not in valid_ids:
                continue
            if not (0 <= val <= 2047):
                continue
            hits.append((sid, val, fixed, inv))
    return hits


def frames_multi(soft, min_fixed=16, formats=("BINARY",)):
    """Slice recovered symbols into frames under the requested ALERT frame
    formats (BINARY / ASCII / ENHANCED IFLOWS).

    BINARY delegates to frames_from_symbols so its behaviour is IDENTICAL by
    construction to the field-proven iFLOWS path - an independent
    reimplementation here quietly admitted extra marginal frames (it invented
    stations 3062 and 7250 on a live capture containing exactly four).

    ENHANCED IFLOWS is what the 4078 test rig sends: only byte 0 carries
    marker bits and byte 3 carries a 6-bit CRC. Reading it as BINARY
    mis-places A12 and every data bit.

    Returns dicts: format, sensor_id, value, crc_ok, fixed, inv.
    """
    from alert_formats import (uart_ok, parse_enhanced_iflows, parse_ascii,
                               FMT_BINARY, FMT_ASCII, FMT_ENHANCED_IFLOWS)
    hits = []
    if FMT_BINARY in formats:
        for sid, val, fixed, inv in frames_from_symbols(soft, None, min_fixed):
            hits.append({"format": FMT_BINARY, "sensor_id": sid,
                         "value": val, "crc_ok": None, "fixed": fixed,
                         "inv": inv})
    others = [p for p, name in ((parse_enhanced_iflows, FMT_ENHANCED_IFLOWS),
                                (parse_ascii, FMT_ASCII)) if name in formats]
    if others:
        for inv in (False, True):
            bits = [(1 if (-v if inv else v) > 0 else 0) for v in soft]
            for p0 in range(len(bits) - 40 + 1):
                fr = bits[p0:p0 + 40]
                if not uart_ok(fr):
                    continue
                for parser in others:
                    try:
                        r = parser(fr)
                    except Exception:
                        r = None
                    if not r:
                        continue
                    if not (0 <= r["value"] <= 2047 and
                            0 <= r["sensor_id"] <= 8191):
                        continue
                    r = dict(r)
                    r["fixed"] = 16 if r.get("crc_ok") else 8
                    r["inv"] = inv
                    hits.append(r)
    return hits


def decode_burst(iq, fs, valid_ids, center_range, shift_range,
                 min_fixed=15):
    """Try centre/shift combos with Gardner recovery; return best frames."""
    best = []
    best_score = -1
    for center in center_range:
        for shift in shift_range:
            d, sr, spb = fsk_soft(iq, fs, center, shift)
            if len(d) < spb * 20:
                continue
            d = d / (np.std(d) + 1e-9)
            soft = gardner(d, spb)
            if len(soft) < 40:
                continue
            hits = frames_from_symbols(soft, valid_ids, min_fixed)
            if not hits:
                continue
            # score: prefer 16/16 and multiple same-site frames
            score = sum(f[2] for f in hits) + 5 * len(hits)
            if score > best_score:
                best_score = score
                best = [(s, v, fx, inv, center, shift) for s, v, fx, inv in hits]
    return best


# ----------------------------------------------------------------------
# Synthetic test harness
# ----------------------------------------------------------------------

def _synth_burst(fs, sid, val, shift=1000, baud=300, snr_db=20, seed=1):
    """Generate a direct-FSK ALERT burst at complex baseband."""
    rng = np.random.RandomState(seed)
    # build the 40-bit frame for sid/val
    w = [0, 0, 0, 0]
    w[0] = sid & 0x3F
    w[1] = (sid >> 6) & 0x3F
    hi = val // 32
    lo = val % 32
    w[3] = hi & 0x3F
    w[2] = ((lo << 1) & 0x3E) | ((sid >> 12) & 1)
    w[0] |= 0x40; w[1] |= 0x40; w[2] |= 0xC0; w[3] |= 0xC0
    frame = []
    for b in w:
        frame.append(0)
        for k in range(8):
            frame.append((b >> k) & 1)
        frame.append(1)
    preamble = [i % 2 for i in range(40)]
    bits = preamble + frame + [1, 1, 1, 1] + frame
    spb = int(round(fs / baud))
    # CPFSK: integrate frequency
    inst = np.concatenate([np.full(spb, (+shift/2 if b else -shift/2))
                           for b in bits])
    phase = 2 * np.pi * np.cumsum(inst) / fs
    iq = np.exp(1j * phase)
    # pad with noise either side
    p = rng.normal(0, 0.3, fs // 4) + 1j * rng.normal(0, 0.3, fs // 4)
    iq = np.concatenate([p, iq, p])
    sigpow = 1.0
    npow = sigpow / (10 ** (snr_db / 10))
    iq = iq + (rng.normal(0, np.sqrt(npow/2), len(iq)) +
               1j * rng.normal(0, np.sqrt(npow/2), len(iq)))
    return iq.astype(np.complex64)


def _self_test():
    fs = 1024000
    print("=== Gardner FSK self-test (synthetic direct-FSK ALERT) ===")
    for snr in (25, 15, 8):
        iq = _synth_burst(fs, sid=7230, val=1188, shift=1000, baud=300,
                          snr_db=snr)
        valid = {7230}
        best = decode_burst(iq, fs, valid,
                            center_range=range(-300, 301, 150),
                            shift_range=(900, 1000, 1100),
                            min_fixed=15)
        ok = any(s == 7230 and v == 1188 for s, v, *_ in best)
        top = best[0] if best else None
        print(f"  SNR {snr:2d}dB: {'DECODED 7230/1188' if ok else 'FAIL'}  "
              f"best={top}")


if __name__ == '__main__':
    _self_test()
