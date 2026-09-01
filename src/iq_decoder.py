"""
Live IQ -> ALERT decoder for the direct-FSK iFLOWS signal.

Consumes complex IQ chunks from rtl_sdr (NOT rtl_fm audio), detects
narrowband FSK bursts, estimates each burst's carrier offset, and decodes
it with the Gardner-PLL FSK receiver (fsk_pll_decode). Deduplicates and
calls back with decoded stations.

This replaces the rtl_fm -> audio -> audio-matched-filter path, which
cannot work because the signal is direct FSK, not audio-AFSK at 2133/1920.
"""
import numpy as np
import time
from collections import deque
from scipy import signal as sig

from fsk_pll_decode import (predecimate, channelize, soft_decision, gardner,
                            frames_from_symbols, frames_multi,
                            afsk_soft_decision, AFSK_F1)

try:
    from sensor_database import get_sensor_db
except ImportError:
    get_sensor_db = None


class IQAlertDecoder:
    def __init__(self, sample_rate=1024000, callback=None,
                 shift_candidates=(700, 800, 900, 1000, 1100, 1200, 1300),
                 baud_candidates=(300, 200),
                 dedup_seconds=45.0, min_fixed=16,
                 formats=("BINARY",)):
        self.fs = int(sample_rate)
        self.callback = callback
        self.shift_candidates = shift_candidates
        self.baud_candidates = baud_candidates   # ALERT uses 300 AND 200 baud
        self.min_fixed = min_fixed
        self.dedup_seconds = dedup_seconds
        # Which 40-bit frame formats to accept. BINARY is the network
        # standard; ENHANCED_IFLOWS is what the 4078 test rig sends. They are
        # kept separate because ENHANCED_IFLOWS is far more loosely
        # constrained (2 marker bits + a 6-bit CRC, versus BINARY's 16 fixed
        # bits), so running it against strong BINARY traffic manufactures
        # phantom stations. See the per-burst arbitration in _decode_band.
        self.formats = tuple(formats)

        # ~3 s rolling IQ buffer; decode on each ~1.5 s of new data
        self.buf = deque(maxlen=int(self.fs * 3))
        self.samples_since_decode = 0
        self.decode_every = int(self.fs * 1.5)

        self.recent = {}          # (sid,val) -> last time
        self.last_candidates = []  # carrier offsets seen in the last window
        self.valid_ids = set()
        self.sensor_db = get_sensor_db() if get_sensor_db else None
        if self.sensor_db and hasattr(self.sensor_db, 'sensors'):
            self.valid_ids = set(self.sensor_db.sensors.keys())

        self.stats = {'bursts': 0, 'decodes': 0, 'samples': 0}

        # IDs permitted to decode with non-standard format-marker bits (UART
        # framing still required). The 4078/4079/4080 test rig packs ID/value
        # the standard way but uses a different marker convention, scoring
        # only 10-11/16 on FIXED. Sourced from sensor_overrides.json so it
        # stays data-driven rather than hard-coded.
        self.uart_only_ids = set()
        if self.sensor_db:
            for sid, info in getattr(self.sensor_db, 'sensors', {}).items():
                if str(info.get('site_number', '')).upper() == 'TESTRIG':
                    self.uart_only_ids.add(int(sid))

    def process_iq(self, iq: np.ndarray):
        """Feed a chunk of complex64 IQ samples."""
        self.buf.extend(iq)
        self.samples_since_decode += len(iq)
        self.stats['samples'] += len(iq)
        if self.samples_since_decode >= self.decode_every:
            self._decode_buffer()
            self.samples_since_decode = 0

    def _find_bursts(self, x):
        """Locate narrowband FSK bursts; return [(start,end,carrier_off)]."""
        nper = 8192
        if len(x) < nper * 2:
            return []
        f, t, S = sig.spectrogram(x, self.fs, nperseg=nper,
                                  noverlap=nper // 2, return_onesided=False)
        f = np.fft.fftshift(f)
        S = np.fft.fftshift(S, axes=0)
        Sdb = 10 * np.log10(S + 1e-12)
        exc = Sdb - np.median(Sdb, axis=1, keepdims=True)
        me = exc.max(axis=0)
        af = f[exc.argmax(axis=0)]
        # Absolute excess threshold (dB above the per-bin noise floor).
        # Real bursts run +20..+60 dB; noise stays a few dB. A percentile
        # threshold fails on short windows where the burst skews the stats.
        thr = 10.0
        hot = me > thr
        ev = []
        for j in np.where(hot)[0]:
            if ev and t[j] - ev[-1][1] < 0.4:
                ev[-1] = (ev[-1][0], t[j], ev[-1][2] if ev[-1][3] >= me[j]
                          else af[j], max(ev[-1][3], me[j]))
            else:
                ev.append((t[j], t[j], af[j], me[j]))
        out = []
        for t0, t1, fo, db in ev:
            if (t1 - t0) < 0.15 or abs(fo) > 200000:
                continue
            s = int(max(0, (t0 - 0.25) * self.fs))
            e = int(min(len(x), (t1 + 0.35) * self.fs))
            out.append((s, e, fo))
        return out

    # iFLOWS ALERT sits within a few kHz of the tuned centre. Find each
    # burst's carrier by spectral peak, then Gardner-decode near it. A real
    # (sid,val) is recovered at MANY carrier/shift combos (20+ votes) while
    # noise 16/16 hits are isolated (1 vote) -> vote threshold separates them.
    SEARCH_HZ = 15000         # search the whole ALERT cluster, not just +/-6k
    DC_NOTCH_HZ = 200         # ignore the RTL-SDR DC-offset spike at 0 Hz
    MIN_VOTES = 6             # known (in-DB) station: real bursts get 7-28
                              # votes, bit-error/edge frames <6
    MIN_VOTES_UNKNOWN = 10    # ID not in Sensors.xlsx: could be a real
                              # unlisted station (the DB is incomplete) OR a
                              # misdecode, so demand strong consensus.
    # ALERT sensor ID is a full 13-bit field: 0-8191 (per the OneRain
    # StormLink spec, IDs below 3000 are legitimate - the local Sensors.xlsx
    # just happens to only list 3000-7999). Do NOT range-gate to the DB range.
    ERRTS_MIN = 0
    ERRTS_MAX = 8191
    MIN_VOTES_CRC = 35        # CRC-bearing format (ENHANCED IFLOWS). Measured
                              # separation is stark: the 4078 rig's frames get
                              # 71-72 CRC-passing hits per burst while chance
                              # CRC matches on other traffic get 1-6. A 6-bit
                              # CRC is only a 1-in-64 filter and thousands of
                              # bit positions are tested, so the CRC lowers the
                              # bar but cannot replace consensus.
    MIN_VOTES_15 = 14         # 15/16 (1-bit-error) frame: only accept with
                              # very high consensus to keep noise out.

    def _carrier_candidates(self, x, sr):
        """Spectral peaks within +/-SEARCH_HZ -> carrier-offset candidates.
        The DC spike (RTL-SDR I/Q offset at 0 Hz) is notched out so it is
        never mistaken for a signal. `x` is the pre-decimated baseband."""
        seg = x
        sp = np.abs(np.fft.fft(seg)) ** 2
        f = np.fft.fftfreq(len(seg), 1.0 / sr)
        f = np.fft.fftshift(f)
        sp = np.fft.fftshift(sp)
        band = (np.abs(f) <= self.SEARCH_HZ) & (np.abs(f) > self.DC_NOTCH_HZ)
        fb, spb = f[band], sp[band]
        if spb.size == 0:
            return []
        med = np.median(spb)
        sps = np.convolve(spb, np.ones(9) / 9, mode='same')
        cands = []
        for i in np.argsort(sps)[::-1]:
            if sps[i] < 6 * med:
                break
            if all(abs(fb[i] - c) > 600 for c in cands):
                cands.append(float(fb[i]))
            if len(cands) >= 5:
                break
        return cands

    def _decode_buffer(self):
        if len(self.buf) < self.fs:
            return
        self.decode_window(np.array(self.buf, dtype=np.complex64))

    def decode_window(self, x):
        """Decode one IQ window (complex array): find carriers, decode,
        dedup, and fire the callback for each new reading. Thread-safe to
        call from a decoder thread while a reader thread fills a buffer."""
        if x is None or len(x) < self.fs // 2:
            return
        # Pre-decimate the WHOLE window to 64 kHz ONCE (16x less data); all
        # carrier detection / trimming / channelization runs on this.
        mid, msr = predecimate(x, self.fs)
        now = time.time()
        for k in [k for k, t in list(self.recent.items())
                  if now - t > self.dedup_seconds]:
            self.recent.pop(k, None)
        cands = self._carrier_candidates(mid, msr)
        self.last_candidates = cands
        if not cands:
            return
        self.stats['bursts'] += 1
        found = self._decode_band(mid, msr, cands)
        for sid, val, fixed, in_db, fmt, raw in found:
            key = (sid, val)
            if key in self.recent:
                self.recent[key] = now
                continue
            self.recent[key] = now
            self.stats['decodes'] += 1
            if self.callback:
                self.callback({'sensor_id': sid, 'station_id': sid,
                               'value': val, 'match_count': fixed,
                               'in_db': in_db,
                               # Added for protocol/ID testing. Consumers
                               # that do not know about these ignore them.
                               'format': fmt, 'bytes': raw})

    def _trim_to_burst(self, x, sr, center):
        """Return the baseband slice around the burst at `center` (drop
        noise). Operates on the pre-decimated 64 kHz stream."""
        n = np.arange(len(x))
        s = x * np.exp(-1j * 2 * np.pi * center / sr * n)
        b = sig.firwin(127, 2500 / (sr / 2))
        s = sig.lfilter(b, 1, s)
        env = np.abs(s)
        w = max(1, int(sr * 0.01))
        env = sig.fftconvolve(env, np.ones(w) / w, mode='same')
        thr = np.median(env) * 3
        hot = np.where(env > thr)[0]
        if len(hot) < sr * 0.1:
            # Weak burst: the envelope never clears 3x median even though a
            # decodable signal is present (AFSK at ~25-30 dB spectral excess
            # is only a small envelope bump in a +/-2.5 kHz channel). Decode
            # the whole window rather than dropping the candidate.
            return x
        a = max(0, hot[0] - int(sr * 0.1))
        z = min(len(x), hot[-1] + int(sr * 0.1))
        return x[a:z]

    def _decode_band(self, x, sr, candidates):
        """AFSK-decode near each carrier candidate; vote; keep >=MIN_VOTES.
        `x` is the pre-decimated 64 kHz baseband.

        The signal is AFSK (tones 1300.8/2109.4 Hz) over NBFM. The FFT peak
        that produced each candidate is usually the strongest TONE LINE, not
        the FM carrier, so the true carrier is at peak +/- f1 (idle tone) -
        we try all three hypotheses. For each carrier we matched-filter all
        four sideband lines (afsk_soft_decision) and recover timing with
        Gardner plus the best brute-force symbol phases."""
        votes = {}
        rawframes = {}   # key -> raw 4 bytes
        bestfix = {}
        crcok = set()
        for c in candidates:
            burst = self._trim_to_burst(x, sr, c)
            if burst is None:
                continue
            for car in (c - AFSK_F1, c + AFSK_F1, c):
              for ctr in (int(car) - 75, int(car), int(car) + 75):
                bb, csr = channelize(burst, sr, ctr, out_rate=12000)
                for baud in self.baud_candidates:
                    d, spb = afsk_soft_decision(bb, csr, baud)
                    if len(d) < spb * 20:
                        continue
                    d = d / (np.std(d) + 1e-9)
                    streams = [gardner(d, spb)]
                    sc = sorted(((float(np.mean(np.abs(d[ph::spb]))), ph)
                                 for ph in range(spb)), reverse=True)
                    streams += [d[ph::spb] for _, ph in sc[:3]]
                    for soft in streams:
                      if len(soft) < 40:
                          continue
                      seen = set()
                      # Parse under every ALERT frame format. ENHANCED IFLOWS
                      # (the 4078 rig) carries a 6-bit CRC and only marks
                      # byte 0; reading it as BINARY mis-places A12 and every
                      # data bit, which is what produced the bogus "+4096
                      # flag" and battery=414 instead of 121 (12.1 V).
                      for r in frames_multi(soft, self.min_fixed,
                                                self.formats):
                          sid = r["sensor_id"]
                          val = r["value"]
                          if not (self.ERRTS_MIN <= sid <= self.ERRTS_MAX):
                              continue
                          # sid 5461 is the ALERT 0x55 preamble parsing as a
                          # frame - never a real station.
                          if sid == 5461 and r["format"] == "BINARY":
                              continue
                          if r["crc_ok"] is False:
                              continue          # CRC-bearing format that failed
                          key = (sid, val, r["format"])
                          # Keep the raw frame for this key. The test rig's
                          # bytes are the ground truth for protocol and ID
                          # work; everything else here is derived from them.
                          if r.get("bytes") is not None:
                              rawframes.setdefault(key, tuple(r["bytes"]))
                          if key not in seen:     # 1 vote per stream/params
                              seen.add(key)
                              votes[key] = votes.get(key, 0) + 1
                          bestfix[key] = max(bestfix.get(key, 0), r["fixed"])
                          if r["crc_ok"]:
                              crcok.add(key)
        out = []
        for key, n in votes.items():
            s, v, fmt = key
            in_db = s in self.valid_ids
            bf = bestfix[key]
            # A 6-bit CRC is only a 1-in-64 filter, and we test thousands of
            # candidate bit positions per burst, so CRC alone is NOT enough -
            # accepting on it produced 33 phantom stations in a capture known
            # to contain 4. Treat it as strong corroboration that LOWERS the
            # vote bar rather than replacing consensus.
            if key in crcok:
                if n >= self.MIN_VOTES_CRC:
                    out.append((s, v, bf, in_db, fmt))
                continue
            if bf >= 16:
                # clean frame: modest consensus is enough
                thr = self.MIN_VOTES if in_db else self.MIN_VOTES_UNKNOWN
            else:
                # 15/16 frame (1 bit error): a real-but-degraded station
                # (e.g. ID 5461 at 200 baud got 19 votes), but noise can also
                # hit 15/16, so demand very strong consensus.
                thr = self.MIN_VOTES_15
            if n >= thr:
                out.append((s, v, bf, in_db, fmt))

        # A single transmission uses ONE frame format, so if more than one
        # yielded ACCEPTED results keep only the best-supported. Arbitrating on
        # raw vote counts instead would let unaccepted BINARY noise outvote and
        # erase perfectly good CRC-validated ENHANCED IFLOWS frames.
        if out:
            per_fmt = {}
            for rec in out:
                per_fmt.setdefault(rec[4], []).append(rec)
            if len(per_fmt) > 1:
                def rank(f):
                    return (f == "ENHANCED_IFLOWS", len(per_fmt[f]))
                keep = max(per_fmt, key=rank)
                out = per_fmt[keep]
        # Keep fmt and attach the raw frame. decode_window needs both;
        # the previous flattening to 4-tuples discarded exactly the
        # information a protocol test depends on.
        return [(a, b, c, d, f, rawframes.get((a, b, f)))
                for a, b, c, d, f in out]

    def _reasonable(self, sid, val):
        if not self.sensor_db:
            return True
        info = self.sensor_db.get_sensor_info(sid)
        if not info:
            return True
        st = info.get('sensor_type', '')
        if st == 'Batt':
            return 0 <= val <= 2047       # battery code, keep broad
        return 0 <= val <= 2047

    def get_stats(self):
        return dict(self.stats, dedup_cache=len(self.recent))
