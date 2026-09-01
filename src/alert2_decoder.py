"""
Live ALERT2 decoder: raw IQ -> sensor readings.

Mirrors iq_decoder.IQAlertDecoder (the iFLOWS path) so the GUI can switch
between protocols without caring which one is running.

ALERT2 occupies far more bandwidth than iFLOWS - 4800 bps FSK rather than
300 baud AFSK - so the channel is decimated to 48 kHz (10 samples/bit)
rather than the 12 kHz used for iFLOWS.
"""
import time

import numpy as np
from scipy import signal as sig

import alert2
from alert2_app import parse_airlink_payload

try:
    from sensor_database import get_sensor_db
except ImportError:
    get_sensor_db = None

CHANNEL_RATE = 48000          # 10 samples per bit at 4800 bps
CHANNEL_BW = 16000.0          # ALERT2 is wideband compared with iFLOWS
SEARCH_HZ = 15000.0
DC_NOTCH_HZ = 200.0


class ALERT2Decoder:
    """Decode ALERT2 AirLink frames from complex IQ windows."""

    def __init__(self, sample_rate=1024000, callback=None,
                 threshold=0.55, dedup_seconds=45.0):
        self.fs = int(sample_rate)
        self.callback = callback
        self.threshold = threshold
        self.dedup_seconds = dedup_seconds
        self.recent = {}
        self.last_candidates = []
        self.stats = {"frames": 0, "readings": 0, "windows": 0}
        self.sensor_db = get_sensor_db() if get_sensor_db else None
        self._rs = alert2.ReedSolomon()

    # ------------------------------------------------------------ front end

    def _carrier_candidates(self, x, sr, maxn=3):
        n = min(len(x), 1 << 14)
        spec = np.abs(np.fft.fft(x[:n])) ** 2
        freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / sr))
        spec = np.fft.fftshift(spec)
        band = (np.abs(freqs) <= SEARCH_HZ) & (np.abs(freqs) > DC_NOTCH_HZ)
        if not band.any():
            return []
        fb, sp = freqs[band], spec[band]
        med = np.median(sp)
        out = []
        for i in np.argsort(sp)[::-1]:
            if sp[i] < 6 * med:
                break
            if all(abs(fb[i] - c) > 4000 for c in out):
                out.append(float(fb[i]))
            if len(out) >= maxn:
                break
        return out

    def _channelize(self, x, center):
        """Tune to `center` and resample to CHANNEL_RATE."""
        n = np.arange(len(x))
        s = x * np.exp(-1j * 2 * np.pi * center / self.fs * n)
        taps = sig.firwin(255, CHANNEL_BW / (self.fs / 2))
        s = sig.lfilter(taps, 1.0, s)
        from math import gcd
        g = gcd(int(self.fs), CHANNEL_RATE)
        return sig.resample_poly(s, CHANNEL_RATE // g,
                                 int(self.fs) // g).astype(np.complex64)

    # --------------------------------------------------------------- decode

    def decode_window(self, x):
        """Decode one IQ window; fire the callback for each new reading."""
        if x is None or len(x) < self.fs // 4:
            return []
        self.stats["windows"] += 1
        now = time.time()
        for k in [k for k, t in list(self.recent.items())
                  if now - t > self.dedup_seconds]:
            self.recent.pop(k, None)

        cands = self._carrier_candidates(x, self.fs)
        self.last_candidates = cands
        if not cands:
            cands = [0.0]

        out = []
        for c in cands:
            try:
                bb = self._channelize(x, c)
                frames = alert2.demodulate(bb, CHANNEL_RATE, rs=self._rs,
                                           threshold=self.threshold)
            except Exception:
                continue
            for fr in frames:
                self.stats["frames"] += 1
                out.extend(self._emit(fr, now))
        return out

    def _emit(self, frame, now):
        made = []
        for hdr, reports in parse_airlink_payload(frame["payload"]):
            site = hdr.get("source_address")
            for rep in reports:
                for rd in rep.get("readings", []):
                    sid = rd["sensor_id"]
                    val = rd["value"]
                    key = (site, sid, str(val))
                    if key in self.recent:
                        self.recent[key] = now
                        continue
                    self.recent[key] = now
                    msg = {
                        "protocol": "ALERT2",
                        "source_address": site,
                        "sensor_id": sid,
                        "value": val,
                        "report_type": rep.get("type_name"),
                        "timestamp_s": rep.get("timestamp"),
                        "test_flag": rep.get("test_flag", False),
                        "format_length": rd.get("format_length"),
                        "time_offsets": rd.get("time_offsets"),
                    }
                    self.stats["readings"] += 1
                    made.append(msg)
                    if self.callback:
                        self.callback(msg)
        return made

    def get_stats(self):
        return dict(self.stats)
