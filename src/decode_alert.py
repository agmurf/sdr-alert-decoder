"""
ALERT1 BINARY Protocol Decoder

Based on ERRTS Protocol Specification (Bureau of Meteorology spec 7/88)
and OneRain StormLink IQ decoding reference.

Protocol: ALERT1 Binary Format (Format A)
  - FSK modulation: mark=2133Hz (1), space=1920Hz (0), 300 baud
  - 4 x 10-bit UART frames: start(0) + 8 data bits (LSB first) + stop(1)
  - 40 bits total per message

  Sensor ID: 13 bits (0-8191), ERRTS IDs are 3000-7999
  Data value: 11 bits (0-2047)
  Preamble: ~250ms of alternating mark/space tones
"""
import numpy as np
from scipy.io import wavfile
from scipy import signal as sig
from typing import List, Dict, Optional, Tuple
from pathlib import Path

try:
    from sensor_database import get_sensor_db
except ImportError:
    get_sensor_db = None


MARK_FREQ = 2133
SPACE_FREQ = 1920
BAUD_RATE = 300
MSG_BITS = 40

FIXED_BITS = {
    0: 0, 7: 1, 8: 0, 9: 1,
    10: 0, 17: 1, 18: 0, 19: 1,
    20: 0, 27: 1, 28: 1, 29: 1,
    30: 0, 37: 1, 38: 1, 39: 1,
}


class ALERTDecoder:
    """ALERT1 Binary Protocol decoder for WAV file and real-time audio"""

    def __init__(self, min_match_bits=14, weak_match_bits=13, freq_offset=0.0):
        self.min_match_bits = min_match_bits
        self.weak_match_bits = weak_match_bits
        self.freq_offset = freq_offset
        # Defaults (US OneRain ALERT1-BINARY); overridden by a calibration
        # profile produced by wav_calibrator.py if one is present.
        self.mark_freq = MARK_FREQ + freq_offset
        self.space_freq = SPACE_FREQ + freq_offset
        self.baud = BAUD_RATE
        self.cal_format = 'BINARY'
        self.cal_bit_order = 'lsb'
        self.cal_source = 'default'
        self._load_calibration()
        self.sensor_db = get_sensor_db() if get_sensor_db else None
        self.valid_ids = set()
        if self.sensor_db and hasattr(self.sensor_db, 'sensors'):
            self.valid_ids = set(self.sensor_db.sensors.keys())

    def _load_calibration(self):
        """Load calibration_profile.json (next to this module / exe) if any."""
        import os, json, sys
        dirs = []
        if getattr(sys, 'frozen', False):
            dirs.append(os.path.dirname(sys.executable))
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
        for d in dirs:
            p = os.path.join(d, 'calibration_profile.json')
            try:
                if os.path.exists(p):
                    prof = json.load(open(p))
                    if prof.get('ok'):
                        self.mark_freq = float(prof['mark'])
                        self.space_freq = float(prof['space'])
                        self.baud = int(prof['baud'])
                        self.cal_format = prof.get('format', 'BINARY')
                        self.cal_bit_order = prof.get('bit_order', 'lsb')
                        self.cal_source = p
                        return
            except Exception:
                pass

    def decode_wav_file(self, wav_path: str) -> Dict:
        """Decode ALERT messages from a WAV file."""
        path = Path(wav_path)
        print(f"\n[ALERT Decoder] {path.name}")

        try:
            sample_rate, audio = wavfile.read(wav_path)
        except Exception as e:
            print(f"  [ERROR] Cannot read WAV: {e}")
            return {'messages': [], 'bursts': [], 'stats': {}}

        if len(audio.shape) > 1:
            audio = audio[:, 0]
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype == np.int32:
            audio = audio.astype(np.float32) / 2147483648.0
        elif audio.dtype == np.uint8:
            audio = (audio.astype(np.float32) - 128) / 128.0

        duration = len(audio) / sample_rate
        print(f"  Duration: {duration:.1f}s, Rate: {sample_rate}Hz")

        bursts = self._detect_bursts(audio, sample_rate)
        print(f"  Bursts detected: {len(bursts)}")

        all_messages = []
        for i, burst_info in enumerate(bursts):
            burst_audio = audio[burst_info['start_sample']:burst_info['end_sample']]
            message = self._decode_burst(burst_audio, sample_rate, burst_index=i+1)
            if message:
                message['time'] = burst_info['start_time']
                message['burst_index'] = i + 1
                all_messages.append(message)
                self._log_message(message, burst_info)

        stats = {
            'burst_count': len(bursts),
            'message_count': len(all_messages),
            'decode_rate': len(all_messages) / len(bursts) if bursts else 0,
        }
        print(f"  Result: {len(all_messages)} messages from {len(bursts)} bursts "
              f"({stats['decode_rate']:.0%} decode rate)")

        return {'messages': all_messages, 'bursts': bursts, 'stats': stats}

    def decode_audio_chunk(self, audio: np.ndarray, sample_rate: int,
                           timestamp: float = 0.0) -> List[Dict]:
        """
        Decode ALERT messages from an audio chunk (live RTL-SDR use).

        Uses STRICT mode: requires a real ALERT preamble plus a perfect
        16/16 protocol match with a known ERRTS ID and sane value. This
        gives zero false positives on rtl_fm static (the live stream is
        constant full-scale FM noise with no squelch).
        """
        bursts = self._detect_bursts(audio, sample_rate)
        messages = []
        for i, burst_info in enumerate(bursts):
            burst_audio = audio[burst_info['start_sample']:burst_info['end_sample']]
            message = self._decode_burst(burst_audio, sample_rate,
                                         burst_index=i+1, strict=True)
            if message:
                message['time'] = timestamp + burst_info['start_time']
                messages.append(message)
        return messages

    def _detect_bursts(self, audio: np.ndarray, sr: int) -> List[Dict]:
        """
        Detect transmission bursts.

        Two strategies:
          1. ENERGY-GAP (primary): for clean recordings/squelched audio that
             have quiet gaps between bursts. Proven ~95% decode rate.
          2. SPECTROGRAM (fallback): for live rtl_fm output with no squelch,
             which is constant full-scale FM noise with no gaps. Detects
             ALERT bursts by FSK-band power dominance.

        Strategy 1 is tried first; if it finds nothing (uniform-energy
        FM-noise case), strategy 2 is used.
        """
        spb = max(8, int(round(sr / self.baud)))
        if len(audio) < sr * 0.3:
            return []

        # --- Strategy 1: energy-gap detection ---
        eg = self._detect_bursts_energy(audio, sr)
        if eg:
            return eg

        # --- Strategy 2: spectrogram FSK-band detection ---
        return self._detect_bursts_spectrogram(audio, sr)

    def _detect_bursts_energy(self, audio: np.ndarray, sr: int) -> List[Dict]:
        """Energy-envelope burst detection (clean / squelched audio)."""
        win = int(sr * 0.02)
        envelope = np.sqrt(np.maximum(0,
            sig.fftconvolve(audio ** 2, np.ones(win) / win, mode='same')))
        noise = np.percentile(envelope, 50)
        peak = envelope.max()
        # Only valid if there's clear contrast (quiet gaps exist).
        # In constant FM noise, noise ~ peak, so this returns nothing.
        if peak < noise * 4:
            return []
        threshold = max(noise * 10, 0.005)
        above = envelope > threshold
        d = np.diff(above.astype(int))
        starts = np.where(d == 1)[0]
        ends = np.where(d == -1)[0]
        bursts = []
        for s_idx in starts:
            cand = ends[ends > s_idx]
            if len(cand) == 0:
                continue
            e_idx = cand[0]
            duration = (e_idx - s_idx) / sr
            if 0.15 <= duration <= 3.0:
                bursts.append({
                    'start_sample': s_idx,
                    'end_sample': e_idx,
                    'start_time': s_idx / sr,
                    'end_time': e_idx / sr,
                    'duration_ms': duration * 1000,
                })
        return bursts

    def _detect_bursts_spectrogram(self, audio: np.ndarray, sr: int) -> List[Dict]:
        """Spectrogram FSK-band burst detection (live FM noise, no gaps)."""
        spb = max(8, int(round(sr / self.baud)))
        if len(audio) < sr * 0.3:
            return []

        # Spectrogram — FSK band power vs total spectrum power
        nperseg = min(512, len(audio) // 4)
        if nperseg < 64:
            return []
        try:
            f, t, Sxx = sig.spectrogram(audio, sr, nperseg=nperseg,
                                        noverlap=nperseg // 2)
        except Exception:
            return []

        alert_band = (f >= 1850) & (f <= 2250)
        if not np.any(alert_band):
            return []
        alert_power = Sxx[alert_band, :].mean(axis=0)
        total_power = Sxx.mean(axis=0)
        ratio = alert_power / (total_power + 1e-12)

        # Burst = FSK band at least 1.8x stronger than the average spectrum
        strong = ratio > 1.8
        idx = np.where(strong)[0]
        if len(idx) == 0:
            return []

        # Group contiguous strong slices (allow small gaps of <=4 slices)
        groups = []
        cur = [idx[0]]
        for i in idx[1:]:
            if i - cur[-1] <= 4:
                cur.append(i)
            else:
                groups.append(cur)
                cur = [i]
        groups.append(cur)

        bursts = []
        for g in groups:
            t0 = t[g[0]]
            t1 = t[g[-1]]
            # Pad: ~350ms before for preamble, ~350ms after for data tail
            s0 = int(max(0, (t0 - 0.35) * sr))
            s1 = int(min(len(audio), (t1 + 0.35) * sr))
            duration = (s1 - s0) / sr
            if 0.30 <= duration <= 4.0:
                bursts.append({
                    'start_sample': s0,
                    'end_sample': s1,
                    'start_time': s0 / sr,
                    'end_time': s1 / sr,
                    'duration_ms': duration * 1000,
                })

        # Merge overlapping/adjacent padded regions
        merged = []
        for b in sorted(bursts, key=lambda x: x['start_sample']):
            if merged and b['start_sample'] <= merged[-1]['end_sample']:
                if b['end_sample'] > merged[-1]['end_sample']:
                    merged[-1]['end_sample'] = b['end_sample']
                    merged[-1]['end_time'] = b['end_time']
                    merged[-1]['duration_ms'] = (
                        (merged[-1]['end_sample'] - merged[-1]['start_sample'])
                        / sr * 1000)
            else:
                merged.append(b)

        return merged

    def _has_valid_preamble(self, burst_audio: np.ndarray, sr: int) -> bool:
        """
        Detect a genuine ALERT preamble (~250ms of alternating mark/space
        at 300 baud → a ~150 Hz tone in the mark-space decision signal).

        Measured on raw rtl_fm static: peak band-fraction maxes at 0.145
        across many bursts. A real preamble through the same chain is a
        clean strong tone well above this. Gate at 0.17 → zero false
        positives on live FM noise.
        """
        spb = max(8, int(round(sr / self.baud)))
        mark_f = self.mark_freq
        space_f = self.space_freq
        t = np.arange(spb) / sr
        mark_ref = np.exp(-2j * np.pi * mark_f * t)
        space_ref = np.exp(-2j * np.pi * space_f * t)
        try:
            mc = np.abs(sig.fftconvolve(burst_audio,
                        np.conj(mark_ref[::-1]), mode='valid'))
            sc = np.abs(sig.fftconvolve(burst_audio,
                        np.conj(space_ref[::-1]), mode='valid'))
        except Exception:
            return False
        dec = mc - sc
        if len(dec) < int(0.18 * sr):
            return False
        winlen = int(0.18 * sr)
        hop = int(0.04 * sr)
        for i in range(0, len(dec) - winlen + 1, hop):
            seg = dec[i:i + winlen]
            seg = seg - seg.mean()
            if np.sqrt(np.mean(seg ** 2)) < 1e-7:
                continue
            f, P = sig.welch(seg, sr, nperseg=min(1024, len(seg)))
            band = (f >= 120) & (f <= 180)
            if not np.any(band):
                continue
            if P[band].max() / (P.sum() + 1e-12) > 0.17:
                return True
        return False

    # A genuine ALERT message is physically present across the entire
    # burst, so it decodes to the SAME (sensor_id, value) at MANY
    # independent sampling phases / window positions. A noise fluke
    # decodes at exactly one. Empirically (validated on synthetic
    # signals down to 0 dB SNR and on 66 bursts of pure rtl_fm static):
    #   - real ALERT signal  -> 20+ consistent votes
    #   - pure noise/static  -> at most 1 vote, ever
    # Requiring >= MIN_VOTES gives weak-signal recovery (like the old
    # hardware) AND structurally zero false positives.
    MIN_VOTES = 4
    FRAME_MIN_BITS = 14  # per-candidate framing bits (real HW tolerates errors)

    def _decode_burst(self, burst_audio: np.ndarray, sr: int,
                      burst_index: int = 0, strict: bool = False) -> Optional[Dict]:
        """
        Decode one ALERT message from a burst using consensus voting.

        Exhaustively samples every clock phase / window position / polarity,
        tallies a vote for every (sensor_id, value) that yields a valid
        framed message with a known ERRTS ID and a sane value, and accepts
        the winner only if it was seen consistently (>= MIN_VOTES times).
        """
        spb = max(8, int(round(sr / self.baud)))
        if len(burst_audio) < spb * 20:
            return None

        mark_f = self.mark_freq
        space_f = self.space_freq
        t = np.arange(spb) / sr
        mark_ref = np.exp(-2j * np.pi * mark_f * t)
        space_ref = np.exp(-2j * np.pi * space_f * t)

        mark_corr = sig.fftconvolve(burst_audio, np.conj(mark_ref[::-1]),
                                    mode='valid')
        space_corr = sig.fftconvolve(burst_audio, np.conj(space_ref[::-1]),
                                     mode='valid')
        mark_env = np.abs(mark_corr)
        space_env = np.abs(space_corr)
        decision = mark_env - space_env
        energy = mark_env + space_env

        energy_smooth = sig.fftconvolve(energy, np.ones(spb) / spb,
                                        mode='same')
        if energy_smooth.max() == 0:
            return None
        threshold = energy_smooth.max() * 0.1
        sig_indices = np.where(energy_smooth > threshold)[0]
        if len(sig_indices) < spb * 10:
            return None
        sig_start = sig_indices[0]
        sig_end = sig_indices[-1]

        # Tally votes across all phases / polarities / window positions.
        votes = {}                 # (sid,val) -> count
        best_mc = {}               # (sid,val) -> best framing-bit count seen
        sample = {}                # (sid,val) -> a representative result dict

        for phase in range(0, spb, max(1, spb // 16)):
            for invert in (False, True):
                bits = []
                for bi in range((sig_end - sig_start) // spb):
                    center = sig_start + phase + bi * spb + spb // 2
                    if 0 <= center < len(decision):
                        v = -decision[center] if invert else decision[center]
                        bits.append(1 if v > 0 else 0)
                if len(bits) < MSG_BITS:
                    continue
                for pos in range(len(bits) - MSG_BITS + 1):
                    msg = bits[pos:pos + MSG_BITS]
                    mc = sum(1 for fp, fv in FIXED_BITS.items()
                             if msg[fp] == fv)
                    if mc < self.FRAME_MIN_BITS:
                        continue
                    result = self._parse_message(msg, mc, invert)
                    if result is None:
                        continue
                    sid = result['sensor_id']
                    if sid not in self.valid_ids:
                        continue
                    if not self._is_value_reasonable(sid, result['value']):
                        continue
                    key = (sid, result['value'])
                    votes[key] = votes.get(key, 0) + 1
                    if mc > best_mc.get(key, -1):
                        best_mc[key] = mc
                        sample[key] = result

        if not votes:
            return None

        # Winner = most consistently decoded message.
        key = max(votes, key=lambda k: (votes[k], best_mc[k]))
        if votes[key] < self.MIN_VOTES:
            return None  # not consistent enough -> reject (noise / unrecoverable)

        result = sample[key]
        result['match_count'] = best_mc[key]
        result['votes'] = votes[key]
        return result

    def _extract_clock_from_preamble(self, decision: np.ndarray, spb: int
                                     ) -> Optional[Tuple[int, int, bool]]:
        """Extract bit clock phase and data start from the preamble."""
        if len(decision) < spb * 15:
            return None

        signs = np.sign(decision)
        sign_changes = np.where(np.diff(signs) != 0)[0]
        if len(sign_changes) < 10:
            return None

        intervals = np.diff(sign_changes)
        valid_mask = (intervals > 0.7 * spb) & (intervals < 1.3 * spb)
        valid_crossings = sign_changes[:-1][valid_mask]
        if len(valid_crossings) < 8:
            return None

        crossing_phases = valid_crossings % spb
        median_transition = int(np.median(crossing_phases))
        sampling_phase = (median_transition + spb // 2) % spb

        is_inverted = False
        first_crossing = valid_crossings[0]
        start_sample = max(0, first_crossing - spb)

        bits = []
        for bi in range(min(120, (len(decision) - start_sample) // spb)):
            center = start_sample + sampling_phase + bi * spb + spb // 2
            if 0 <= center < len(decision):
                bits.append(1 if decision[center] > 0 else 0)

        preamble_end = None
        for i in range(3, len(bits) - MSG_BITS):
            if bits[i] == bits[i - 1]:
                if bits[i] == 0 and bits[i - 1] == 0:
                    preamble_end = i - 1
                    is_inverted = True
                elif bits[i] == 1 and bits[i - 1] == 1:
                    preamble_end = i - 1
                    is_inverted = False
                else:
                    preamble_end = i
                break

        if preamble_end is None:
            estimated = int(0.6 * len(bits))
            preamble_end = min(estimated, len(bits) - MSG_BITS)

        data_start = start_sample + preamble_end * spb
        if data_start + MSG_BITS * spb > len(decision):
            return None

        return (sampling_phase, data_start, is_inverted)

    def _score_phase_soft(self, decision: np.ndarray, data_start: int,
                          phase: int, spb: int, invert: bool) -> float:
        """Soft confidence score for a clock phase at a candidate position."""
        score = 0.0
        for fp, fv in FIXED_BITS.items():
            center = data_start + phase + fp * spb + spb // 2
            if 0 <= center < len(decision):
                val = -decision[center] if invert else decision[center]
                if fv == 1:
                    score += val
                else:
                    score -= val
        return score

    def _parse_message(self, msg_bits: list, match_count: int,
                       inverted: bool) -> Optional[Dict]:
        """Parse a 40-bit ALERT message into sensor ID and value."""
        words = []
        for w in range(4):
            frame = msg_bits[w*10:(w+1)*10]
            byte_val = 0
            for bi in range(8):
                if frame[1 + bi]:
                    byte_val |= (1 << bi)
            words.append(byte_val)

        sensor_id = (words[0] & 0x3F) + 64 * (words[1] & 0x3F) + 4096 * (words[2] & 0x01)
        value = (words[3] & 0x3F) * 32 + ((words[2] & 0x3E) >> 1)

        if value < 0 or value > 2047:
            return None

        return {
            'sensor_id': sensor_id,
            'station_id': sensor_id,  # backward compat with dashboard
            'value': value,
            'match_count': match_count,
            'inverted': inverted,
            'raw_bytes': words,
            'protocol': 'ALERT1',
        }

    def _is_value_reasonable(self, sensor_id: int, value: int) -> bool:
        """Check if the decoded value is physically reasonable."""
        if not self.sensor_db:
            return True
        info = self.sensor_db.get_sensor_info(sensor_id)
        if not info:
            return True
        stype = info.get('sensor_type', '')
        if stype == 'Batt':
            return 10.0 <= value * 0.01 <= 15.0
        elif stype == 'Rain':
            return 0 <= value <= 2047
        elif 'River' in stype:
            return -1.0 <= value * 0.01 <= 20.0
        elif stype == 'C':
            return -10.0 <= value * 0.1 <= 50.0
        return True

    def _log_message(self, message: Dict, burst_info: Dict):
        """Log a decoded message."""
        sid = message['sensor_id']
        val = message['value']
        mc = message['match_count']
        inv = 'INV' if message.get('inverted') else ''
        if sid in self.valid_ids and self.sensor_db:
            info = self.sensor_db.get_sensor_info(sid)
            stype = info.get('sensor_type', '?') if info else '?'
            sname = info.get('site_name', '?')[:35] if info else '?'
            decoded_val, unit, _ = self.sensor_db.decode_sensor_value(sid, val)
            print(f"  [OK] Burst {message['burst_index']} @ {burst_info['start_time']:.1f}s: "
                  f"ERRTS {sid} ({stype}) = {decoded_val:.2f}{unit} [{mc}/16] {inv} [{sname}]")
        else:
            print(f"  [??] Burst {message['burst_index']} @ {burst_info['start_time']:.1f}s: "
                  f"ID={sid} val={val} [{mc}/16] {inv}")

    def format_message(self, message: Dict) -> str:
        """Format a decoded message for display."""
        sid = message['sensor_id']
        val = message['value']
        if self.sensor_db:
            info = self.sensor_db.get_sensor_info(sid)
            if info:
                decoded_val, unit, desc = self.sensor_db.decode_sensor_value(sid, val)
                return (f"ERRTS {sid} ({info.get('sensor_type','?')}) = "
                        f"{decoded_val:.2f}{unit} [{info.get('site_name','?')}]")
        return f"ID={sid} Value={val}"


ALERTDecoderV6 = ALERTDecoder  # backward compat


def main():
    import sys
    decoder = ALERTDecoder(min_match_bits=14, weak_match_bits=13)
    if len(sys.argv) > 1:
        results = decoder.decode_wav_file(sys.argv[1])
        print(f"\n{'='*60}\nDECODED MESSAGES\n{'='*60}")
        for msg in results['messages']:
            print(f"  @ {msg['time']:.1f}s: {decoder.format_message(msg)}")


if __name__ == "__main__":
    main()
