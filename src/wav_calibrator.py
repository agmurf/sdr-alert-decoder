"""
WAV-calibrated autotuner.

Given one or more WAV recordings that contain real ERRTS bursts, this
exhaustively searches the demodulation parameter space and finds the
parameter set that yields the strongest CONSENSUS of database-valid
decodes, then writes calibration_profile.json for the live decoder.

Key principle (proven this project): with the CORRECT parameters a real
burst decodes to the SAME (sensor_id,value) at many independent sampling
phases/positions (20-30 votes, even at 0 dB SNR); wrong parameters or
noise never exceed ~1 vote. So parameter search has a sharp, unambiguous
objective and needs no fragile thresholds.

Search dimensions:
  mark freq, space freq (constrained 80<=shift<=280 Hz),
  baud {150,200,300,600,1200}, polarity {norm,inv},
  bit order {lsb,msb}, frame format {BINARY,BINARY_C,ENHANCED,ASCII}

Usage (CLI):
  python wav_calibrator.py file1.wav [file2.wav ...] [--id 7132 --value 1788]
"""
import os
import json
import numpy as np
from scipy import signal as sig
from scipy.io import wavfile
from typing import List, Dict, Optional, Callable

from frame_formats import FRAME_PARSERS

# BINARY framing fixed-bit pattern (used as a fast vectorized pre-filter so
# the expensive per-window parsers run only on plausible positions).
_FIXED_IDX = np.array([0, 7, 8, 9, 10, 17, 18, 19,
                       20, 27, 28, 29, 30, 37, 38, 39])
_FIXED_VAL = np.array([0, 1, 0, 1, 0, 1, 0, 1,
                       0, 1, 1, 1, 0, 1, 1, 1])

# Reuse the proven burst detector + sensor DB from the live decoder.
try:
    from decode_alert import ALERTDecoder
except Exception:
    ALERTDecoder = None


PROFILE_NAME = 'calibration_profile.json'

# Coarse search grid
BAUDS = [300, 200, 150, 600, 1200]
SHIFT_MIN, SHIFT_MAX = 80, 280
MIN_VOTES = 8            # winner must be this consistent (noise tops out ~1)
QUALITY_GATE_VOTES = 5   # a usable corpus must clearly beat noise/degraded
                         # (pure noise <=1, degraded WAVs 2-3, clean >=8)

# Only STRONG-framing formats may auto-win calibration. BINARY and
# BINARY_C each carry 8 hard framing bits, so a vote is strong evidence.
# ENHANCED has only 2 framing bits (its integrity is a 6-bit CRC whose BoM
# polynomial is undocumented) and ASCII has no DB validation - allowing
# them to auto-win lets the search overfit the least-constrained
# hypothesis and report false positives. They remain available in
# frame_formats for manual/future use but never auto-win here.
STRONG_FORMATS = ['BINARY', 'BINARY_C']


class WavCalibrator:
    def __init__(self, sensors_xlsx: Optional[str] = None,
                 log: Optional[Callable] = None):
        self.log = log or (lambda m: print(m))
        # valid ERRTS IDs from the live decoder's DB
        self.valid_ids = set()
        self.sensor_db = None
        if ALERTDecoder is not None:
            dec = ALERTDecoder()
            self.valid_ids = set(dec.valid_ids)
            self.sensor_db = dec.sensor_db
            self._detect = dec._detect_bursts
        else:
            self._detect = None

    # ---- audio / burst loading -------------------------------------------

    def _load_wav(self, path: str):
        sr, a = wavfile.read(path)
        if a.ndim > 1:
            a = a[:, 0]
        if a.dtype == np.int16:
            a = a.astype(np.float32) / 32768.0
        elif a.dtype == np.int32:
            a = a.astype(np.float32) / 2147483648.0
        elif a.dtype == np.uint8:
            a = (a.astype(np.float32) - 128) / 128.0
        else:
            a = a.astype(np.float32)
        return sr, a

    def _bursts(self, audio, sr):
        if self._detect is not None:
            try:
                bl = self._detect(audio, sr)
                return [(b['start_sample'], b['end_sample']) for b in bl]
            except Exception:
                pass
        # Fallback: spectrogram FSK-band purity
        if len(audio) < sr * 0.3:
            return []
        nper = min(512, len(audio) // 4)
        if nper < 64:
            return []
        f, t, S = sig.spectrogram(audio, sr, nperseg=nper, noverlap=nper // 2)
        ab = (f >= 1850) & (f <= 2250)
        r = S[ab, :].mean(axis=0) / (S.mean(axis=0) + 1e-12)
        idx = np.where(r > 1.8)[0]
        if len(idx) == 0:
            return []
        groups, cur = [], [idx[0]]
        for i in idx[1:]:
            if i - cur[-1] <= 4:
                cur.append(i)
            else:
                groups.append(cur); cur = [i]
        groups.append(cur)
        out = []
        for g in groups:
            s = int(max(0, (t[g[0]] - 0.35) * sr))
            e = int(min(len(audio), (t[g[-1]] + 0.35) * sr))
            if 0.30 <= (e - s) / sr <= 4.0:
                out.append((s, e))
        return out

    # ---- core vote scoring for one parameter set -------------------------

    def _decision(self, seg, sr, mark, space, baud):
        spb = max(8, int(round(sr / baud)))
        if len(seg) < spb * 20:
            return None, spb
        t = np.arange(spb) / sr
        mr = np.exp(-2j * np.pi * mark * t)
        sp = np.exp(-2j * np.pi * space * t)
        mc = np.abs(sig.fftconvolve(seg, np.conj(mr[::-1]), mode='valid'))
        sc = np.abs(sig.fftconvolve(seg, np.conj(sp[::-1]), mode='valid'))
        return (mc - sc), spb

    def _vote_burst(self, seg, sr, mark, space, baud, formats,
                    phase_div=12):
        """
        Tally votes for every (format,sid,val) across phases/polarity/order.

        Vectorised: for each phase the whole bit vector is sampled at once,
        all 40-bit windows are framing-pre-filtered with numpy, and the
        per-window parsers run ONLY on the few plausible positions.
        """
        d, spb = self._decision(seg, sr, mark, space, baud)
        if d is None:
            return {}
        votes = {}
        step = max(1, spb // phase_div)
        nbits_max = (len(d) - spb // 2) // spb
        if nbits_max < 40:
            return {}
        try:
            swv = np.lib.stride_tricks.sliding_window_view
        except Exception:
            swv = None

        for phase in range(0, spb, step):
            idx = phase + np.arange(nbits_max) * spb + spb // 2
            idx = idx[idx < len(d)]
            if len(idx) < 40:
                continue
            base = (d[idx] > 0).astype(np.int8)
            for inv in (False, True):
                bits = (1 - base) if inv else base
                if swv is not None:
                    wins = swv(bits, 40)            # (nwin, 40)
                else:
                    wins = np.stack([bits[i:i + 40]
                                     for i in range(len(bits) - 39)])
                if wins.shape[0] == 0:
                    continue
                # vectorised BINARY framing pre-filter
                fb = (wins[:, _FIXED_IDX] == _FIXED_VAL).sum(axis=1)
                cand = np.where(fb >= 12)[0]
                if len(cand) == 0 and 'ENHANCED' not in formats \
                        and 'ASCII' not in formats:
                    continue
                # for ENHANCED/ASCII also consider word-1 identifier hits
                extra = np.array([], dtype=int)
                if 'ENHANCED' in formats or 'ASCII' in formats:
                    extra = np.where((wins[:, 7] >= 0))[0][::7]  # sparse scan
                positions = np.unique(np.concatenate(
                    [cand, extra]).astype(int)) if len(extra) else cand
                for order in ('lsb', 'msb'):
                    for p in positions:
                        frame = wins[p].tolist()
                        if order == 'msb':
                            for w in range(4):
                                b = w * 10
                                frame[b + 1:b + 9] = frame[b + 1:b + 9][::-1]
                        for fname in formats:
                            parser, db_val, perfect = FRAME_PARSERS[fname]
                            r = parser(frame)
                            if r is None:
                                continue
                            if r['framing_score'] < perfect - 2:
                                continue
                            if db_val:
                                sid = r['sensor_id']
                                if sid is None or sid not in self.valid_ids:
                                    continue
                                key = (fname, sid, r['value'], inv, order)
                            else:
                                key = (fname, r['aux'].get('text', ''),
                                       None, inv, order)
                            votes[key] = votes.get(key, 0) + 1
        return votes

    def _score_params(self, bursts_audio, sr, mark, space, baud, formats,
                      ground_truth=None, phase_div=12):
        """Aggregate score for a parameter set over all bursts."""
        total = 0
        per_burst_top = []
        id_recurrence = {}
        for seg in bursts_audio:
            v = self._vote_burst(seg, sr, mark, space, baud, formats,
                                 phase_div)
            if not v:
                per_burst_top.append(0)
                continue
            kbest = max(v, key=v.get)
            nb = v[kbest]
            per_burst_top.append(nb)
            fname, sid, val = kbest[0], kbest[1], kbest[2]
            if ground_truth is not None:
                gid, gval = ground_truth
                if sid == gid and (gval is None or val == gval):
                    total += nb * 5      # strong reward for ground-truth hit
            if isinstance(sid, int):
                id_recurrence[sid] = id_recurrence.get(sid, 0) + 1
            total += nb
        # bonus: same ERRTS ID recurring across bursts (real networks repeat)
        recur_bonus = sum((c - 1) * 4 for c in id_recurrence.values() if c > 1)
        return {
            'score': total + recur_bonus,
            'best_per_burst': max(per_burst_top) if per_burst_top else 0,
            'recurrence': id_recurrence,
        }

    # ---- full calibration pipeline ---------------------------------------

    def calibrate(self, wav_paths: List[str],
                  ground_truth: Optional[tuple] = None) -> Dict:
        # 1. Load corpus + detect bursts
        corpus = []
        for p in wav_paths:
            try:
                sr, a = self._load_wav(p)
            except Exception as e:
                self.log(f"[calib] skip {os.path.basename(p)}: {e}")
                continue
            bl = self._bursts(a, sr)
            self.log(f"[calib] {os.path.basename(p)}: {len(bl)} bursts @ {sr}Hz")
            for s, e in bl:
                corpus.append((sr, a[s:e]))
        if not corpus:
            return {'ok': False, 'reason': 'no bursts found in corpus'}

        # group bursts by sample rate (almost always one)
        srs = {}
        for sr, seg in corpus:
            srs.setdefault(sr, []).append(seg)
        sr = max(srs, key=lambda k: len(srs[k]))
        segs_all = srs[sr]
        # Bound runtime: rank bursts by FSK-band purity and keep the
        # strongest MAX_BURSTS. A correct calibration needs only a handful
        # of clean bursts; processing 100s of weak ones just wastes minutes.
        MAX_BURSTS = 12
        if len(segs_all) > MAX_BURSTS:
            def purity(s):
                if len(s) < 256:
                    return 0.0
                f, P = sig.welch(s, sr, nperseg=min(1024, len(s)))
                bd = (f >= 1700) & (f <= 2400)
                return (P[bd].mean() / (P.mean() + 1e-12)) if bd.any() else 0.0
            segs = sorted(segs_all, key=purity, reverse=True)[:MAX_BURSTS]
        else:
            segs = segs_all
        self.log(f"[calib] {len(segs)}/{len(segs_all)} strongest bursts "
                 f"@ {sr}Hz used")

        formats_stage1 = STRONG_FORMATS

        # 1b. FAST quality pre-screen. A degraded / non-ALERT corpus can
        #     never beat ~1-2 votes anywhere, so probe a tiny grid first and
        #     bail out immediately instead of grinding the full 5-baud grid
        #     for minutes. (Clean signal sails past this.)
        pre = segs[:4]
        pre_best = 0
        for center in (1975, 2025, 2075, 2125):
            for shift in (120, 180, 240):
                r = self._score_params(pre, sr, center + shift // 2,
                                        center - shift // 2, 300,
                                        ['BINARY'], ground_truth,
                                        phase_div=6)
                pre_best = max(pre_best, r['best_per_burst'])
                if pre_best >= QUALITY_GATE_VOTES + 2:
                    break
            if pre_best >= QUALITY_GATE_VOTES + 2:
                break
        self.log(f"[calib] pre-screen best consensus = {pre_best} votes")
        if pre_best < QUALITY_GATE_VOTES:
            return {'ok': False,
                    'reason': f'corpus too degraded - best consensus only '
                              f'{pre_best} votes at the most likely settings. '
                              'Record a cleaner sample at the correct '
                              'frequency (signals strong/audible).'}

        # 2. Stage-1 coarse grid — baud-prioritised with early exit.
        #    300 baud is by far the most common ALERT rate, so try it first
        #    and only fall back to other rates if it finds nothing solid.
        coarse = []
        probe = segs[:6]
        for baud in BAUDS:
            found_this_baud = []
            for center in range(1950, 2151, 25):
                for shift in range(SHIFT_MIN, SHIFT_MAX + 1, 25):
                    mark = center + shift // 2
                    space = center - shift // 2
                    r = self._score_params(probe, sr, mark, space, baud,
                                            formats_stage1, ground_truth,
                                            phase_div=6)
                    if r['score'] > 0:
                        found_this_baud.append(
                            (r['score'], mark, space, baud, r))
            coarse.extend(found_this_baud)
            # strong hit at this baud -> no need to scan the rest
            if found_this_baud:
                bp = max(x[4]['best_per_burst'] for x in found_this_baud)
                if bp >= 8:
                    self.log(f"[calib] strong hit at baud={baud} "
                             f"(best_per_burst={bp}); skipping other rates")
                    break
        if not coarse:
            return {'ok': False,
                    'reason': 'no parameter set produced any valid decode '
                              '(corpus likely too degraded or non-ALERT)'}
        coarse.sort(key=lambda x: -x[0])
        self.log(f"[calib] stage1 top: score={coarse[0][0]} "
                 f"mark={coarse[0][1]} space={coarse[0][2]} baud={coarse[0][3]}")

        # 3. Stage-2 refine around the top few coarse clusters
        cand = []
        for sc, mk, spc, bd, _ in coarse[:5]:
            for dm in range(-12, 13, 3):
                for ds in range(-12, 13, 3):
                    mark, space = mk + dm, spc + ds
                    if not (SHIFT_MIN <= mark - space <= SHIFT_MAX):
                        continue
                    r = self._score_params(segs, sr, mark, space, bd,
                                            formats_stage1, ground_truth,
                                            phase_div=14)
                    cand.append((r['score'], mark, space, bd, r))
        cand.sort(key=lambda x: -x[0])
        if not cand or cand[0][4]['best_per_burst'] < QUALITY_GATE_VOTES:
            return {'ok': False,
                    'reason': 'corpus too degraded - best consensus only '
                              f"{cand[0][4]['best_per_burst'] if cand else 0} "
                              'votes (need a cleaner recording at the correct '
                              'frequency)'}

        score, mark, space, baud, r = cand[0]

        # 4. Determine winning format / polarity / order from a full vote
        tally = {}
        for seg in segs:
            v = self._vote_burst(seg, sr, mark, space, baud,
                                 formats_stage1, phase_div=16)
            for k, n in v.items():
                tally[k] = tally.get(k, 0) + n
        if not tally:
            return {'ok': False, 'reason': 'refine produced no votes'}
        kbest = max(tally, key=tally.get)
        fmt, sid, val, inv, order = kbest
        votes = tally[kbest]

        # 4b. Fine-tune mark/space to MAXIMISE consensus for the winning
        #     config — gives the locked frequencies the best margin on weak
        #     live signals (correlation is tolerant, so the coarse winner is
        #     rarely the true centre).
        best_fine = (votes, mark, space)
        ft_segs = segs[:4]                       # bounded fine-tune set
        for dm in range(-15, 16, 5):
            for ds in range(-15, 16, 5):
                m2, s2 = mark + dm, space + ds
                if not (SHIFT_MIN <= m2 - s2 <= SHIFT_MAX):
                    continue
                tot = 0
                for seg in ft_segs:
                    v = self._vote_burst(seg, sr, m2, s2, baud,
                                         [fmt], phase_div=12)
                    same = [n for k, n in v.items()
                            if k[0] == fmt and k[3] == inv and k[4] == order]
                    tot += max(same) if same else 0
                if tot > best_fine[0]:
                    best_fine = (tot, m2, s2)
        votes, mark, space = best_fine

        if votes < MIN_VOTES and ground_truth is None:
            return {'ok': False,
                    'reason': f'winner only {votes} votes (< {MIN_VOTES}); '
                              'not confident - record a cleaner sample'}

        site = None
        if self.sensor_db and isinstance(sid, int):
            info = self.sensor_db.get_sensor_info(sid)
            if info:
                site = f"{info.get('sensor_type','?')} @ {info.get('site_name','?')}"

        profile = {
            'ok': True,
            'mark': int(mark),
            'space': int(space),
            'shift': int(mark - space),
            'baud': int(baud),
            'polarity': 'inverted' if inv else 'normal',
            'bit_order': order,
            'format': fmt,
            'votes': int(votes),
            'best_per_burst': int(r['best_per_burst']),
            'n_bursts': len(segs),
            'recurrence': {str(k): v for k, v in r['recurrence'].items()},
            'example': {'sensor_id': sid, 'value': val, 'site': site},
            'validated_against': ('ground_truth' if ground_truth
                                  else 'errts_database'),
        }
        return profile

    def save_profile(self, profile: Dict, directory: str) -> str:
        path = os.path.join(directory, PROFILE_NAME)
        with open(path, 'w') as f:
            json.dump(profile, f, indent=2)
        return path


def load_profile(directory: str) -> Optional[Dict]:
    path = os.path.join(directory, PROFILE_NAME)
    try:
        if os.path.exists(path):
            with open(path) as f:
                p = json.load(f)
            if p.get('ok'):
                return p
    except Exception:
        pass
    return None


def main():
    import sys
    args = sys.argv[1:]
    gt = None
    if '--id' in args:
        i = args.index('--id')
        gid = int(args[i + 1])
        gval = None
        if '--value' in args:
            gval = int(args[args.index('--value') + 1])
        gt = (gid, gval)
        args = [a for a in args if a not in (
            '--id', '--value', str(gid)) and a != (str(gval) if gval else '')]
    wavs = [a for a in args if a.lower().endswith('.wav')]
    if not wavs:
        print("usage: wav_calibrator.py FILE.wav [...] [--id N --value M]")
        return
    c = WavCalibrator()
    prof = c.calibrate(wavs, ground_truth=gt)
    print(json.dumps(prof, indent=2))
    if prof.get('ok'):
        p = c.save_profile(prof, os.path.dirname(os.path.abspath(__file__)))
        print(f"saved -> {p}")


if __name__ == '__main__':
    main()
