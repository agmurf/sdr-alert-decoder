"""
Auto-tuning for RTL-SDR parameters.

Automatically calibrates RF gain, squelch, and frequency offset by
measuring received signal characteristics. Plug-and-play operation.

Lifecycle:
  1. Startup (0-5s): Measure noise floor with defaults
  2. Calibration (5-15s): Adjust gain if clipping detected
  3. Active (ongoing): Measure frequency offset from ALERT bursts
"""
import numpy as np
from collections import deque
import time
import json
from pathlib import Path
from typing import Optional, Callable, Dict

NOMINAL_MARK = 2133.0
NOMINAL_SPACE = 1920.0
NOMINAL_MID = (NOMINAL_MARK + NOMINAL_SPACE) / 2
NOMINAL_SHIFT = NOMINAL_MARK - NOMINAL_SPACE

GAIN_STEPS = [0, 10, 15, 20, 25, 30, 35, 40, 45, 49]


class AutoTuner:
    """Observes the RTL-SDR audio stream and auto-tunes parameters."""

    def __init__(self, initial_gain=40, initial_squelch=0, initial_ppm=0,
                 sample_rate=24000,
                 restart_callback: Optional[Callable] = None,
                 log_callback: Optional[Callable] = None,
                 config_path: Optional[str] = None):
        self.sample_rate = sample_rate
        self.restart_callback = restart_callback
        self.log = log_callback or print
        self.enabled = True

        self.gain = initial_gain
        self.squelch = initial_squelch
        self.ppm = initial_ppm

        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = Path(__file__).parent / 'tuner_config.json'
        self._load_config()

        self.phase = 'startup'
        self.start_time = time.time()
        self.last_restart_time = 0
        self.MIN_RESTART_INTERVAL = 10.0

        self.noise_rms_values = deque(maxlen=100)
        self.noise_floor = 0.01

        self.clip_count = 0
        self.total_count = 0
        self.CLIP_THRESHOLD = 0.95
        self.MAX_CLIP_RATE = 0.001

        self.freq_offset = 0.0
        self.measured_offset = 0.0
        self.freq_offset_measurements = deque(maxlen=30)
        self.FREQ_OFFSET_THRESHOLD = 15
        self.auto_apply_offset = False

        self.ppm_measurements = deque(maxlen=30)
        self.PPM_CHANGE_THRESHOLD = 3

        self.ring_buffer = deque(maxlen=int(sample_rate * 2))

        self.false_burst_count = 0
        self.false_burst_window_start = time.time()
        self.MAX_SQUELCH = 400
        self.SQUELCH_STEP = 25

        self.status_message = "Starting up..."
        self.calibration_complete = False

    def process_samples(self, audio: np.ndarray):
        """Process an audio chunk. Lightweight — RMS and clipping stats only."""
        if len(audio) == 0:
            return

        self.ring_buffer.extend(audio)

        abs_audio = np.abs(audio)
        self.clip_count += int(np.sum(abs_audio > self.CLIP_THRESHOLD))
        self.total_count += len(audio)

        rms = np.sqrt(np.mean(audio ** 2))
        if rms < 0.05:
            self.noise_rms_values.append(rms)
            if len(self.noise_rms_values) >= 10:
                self.noise_floor = np.median(list(self.noise_rms_values))

        elapsed = time.time() - self.start_time

        if self.phase == 'startup' and elapsed >= 5.0:
            self.phase = 'calibrating'
            self._run_calibration()
        elif self.phase == 'calibrating' and elapsed >= 15.0:
            self.phase = 'active'
            self.calibration_complete = True
            self._save_config()
            self.log("[AutoTune] Calibration complete")
            self.status_message = f"Active (gain={self.gain}, ppm={self.ppm})"
        elif self.phase == 'active':
            self._check_continuous()

    def on_decode_result(self, burst_decoded: bool, match_count: int = 0):
        """Track false-burst rate for squelch tuning."""
        if not burst_decoded or match_count < 13:
            self.false_burst_count += 1

    def measure_freq_offset(self, burst_audio: np.ndarray) -> Optional[float]:
        """Measure audio frequency offset by sweeping mark/space correlation."""
        from scipy import signal as sig
        sr = self.sample_rate
        if len(burst_audio) < int(sr * 0.25):
            return None
        spb = sr // 300
        offsets = np.arange(-60, 61, 2)
        scores = []
        for offset in offsets:
            mark_f = NOMINAL_MARK + offset
            space_f = NOMINAL_SPACE + offset
            t = np.arange(spb) / sr
            mark_ref = np.exp(-2j * np.pi * mark_f * t)
            space_ref = np.exp(-2j * np.pi * space_f * t)
            mc = sig.fftconvolve(burst_audio, np.conj(mark_ref[::-1]), mode='valid')
            sc = sig.fftconvolve(burst_audio, np.conj(space_ref[::-1]), mode='valid')
            decision = np.abs(mc) - np.abs(sc)
            scores.append(np.mean(np.abs(decision)))
        scores = np.array(scores)
        best_idx = np.argmax(scores)
        best_offset = float(offsets[best_idx])
        if scores[best_idx] < 1.2 * np.median(scores):
            return None
        return best_offset

    def get_status(self) -> Dict:
        """Return current auto-tuner state for GUI display."""
        clip_rate = self.clip_count / max(1, self.total_count)
        return {
            'phase': self.phase,
            'gain': self.gain,
            'squelch': self.squelch,
            'ppm': self.ppm,
            'noise_floor_db': 20 * np.log10(self.noise_floor + 1e-10),
            'clip_rate': clip_rate,
            'freq_offset': self.freq_offset,
            'measured_offset': self.measured_offset,
            'freq_measurements': len(self.freq_offset_measurements),
            'message': self.status_message,
            'enabled': self.enabled,
            'calibrated': self.calibration_complete,
        }

    def _run_calibration(self):
        clip_rate = self.clip_count / max(1, self.total_count)
        if clip_rate > 0.01:
            self._reduce_gain(reason=f"high clipping ({clip_rate:.1%})")
        elif clip_rate > self.MAX_CLIP_RATE:
            self._reduce_gain(reason=f"clipping ({clip_rate:.2%})")
        else:
            self.log(f"[AutoTune] Gain OK ({self.gain}dB, clip={clip_rate:.3%})")
            self.status_message = f"Calibrating (gain={self.gain} OK)"
        self.clip_count = 0
        self.total_count = 0

    def _check_continuous(self):
        if self.total_count > self.sample_rate * 5:
            clip_rate = self.clip_count / max(1, self.total_count)
            if clip_rate > self.MAX_CLIP_RATE:
                self._reduce_gain(reason=f"clipping ({clip_rate:.2%})")
            self.clip_count = 0
            self.total_count = 0

        now = time.time()
        window = now - self.false_burst_window_start
        if window > 10.0:
            rate = self.false_burst_count / window
            if rate > 0.5 and self.squelch < self.MAX_SQUELCH:
                new_sq = min(self.squelch + self.SQUELCH_STEP, self.MAX_SQUELCH)
                self._apply_squelch(new_sq,
                    reason=f"high false-burst rate ({rate:.1f}/s)")
            self.false_burst_count = 0
            self.false_burst_window_start = now

        self._try_freq_measurement()

    def _try_freq_measurement(self):
        if len(self.ring_buffer) < self.sample_rate:
            return
        audio = np.array(self.ring_buffer, dtype=np.float32)
        threshold = max(self.noise_floor * 8, 0.01)
        above = np.abs(audio) > threshold
        min_samples = int(self.sample_rate * 0.3)
        run_start = None
        run_len = 0
        best_start = None
        best_len = 0
        for i in range(len(above)):
            if above[i]:
                if run_start is None:
                    run_start = i
                run_len += 1
            else:
                if run_len > best_len:
                    best_start = run_start
                    best_len = run_len
                run_start = None
                run_len = 0
        if run_len > best_len:
            best_start = run_start
            best_len = run_len
        if best_len < min_samples or best_start is None:
            return
        burst = audio[best_start:best_start + best_len]
        offset = self.measure_freq_offset(burst)
        if offset is not None:
            self.freq_offset_measurements.append(offset)
            self._check_freq_offset_update()

    def _check_freq_offset_update(self):
        if len(self.freq_offset_measurements) < 3:
            return
        median_offset = float(np.median(list(self.freq_offset_measurements)))
        self.measured_offset = median_offset
        if self.auto_apply_offset and abs(median_offset) >= self.FREQ_OFFSET_THRESHOLD:
            if abs(median_offset - self.freq_offset) >= 5:
                self.freq_offset = median_offset
                self.log(f"[AutoTune] Applied frequency offset: {median_offset:+.0f}Hz")
        elif abs(median_offset) >= 10 and not hasattr(self, '_offset_reported'):
            self._offset_reported = True
            self.log(f"[AutoTune] Measured frequency offset: {median_offset:+.0f}Hz "
                     f"(not auto-applied)")

    def _reduce_gain(self, reason: str = ""):
        current_idx = self._gain_index()
        if current_idx <= 0:
            self.log(f"[AutoTune] Gain already at minimum ({self.gain}dB)")
            return
        new_gain = GAIN_STEPS[current_idx - 1]
        self.log(f"[AutoTune] Reducing gain: {self.gain} -> {new_gain}dB ({reason})")
        self.gain = new_gain
        self._apply_restart(reason=f"gain reduced to {new_gain}dB")

    def _apply_squelch(self, new_squelch: int, reason: str = ""):
        self.log(f"[AutoTune] Squelch: {self.squelch} -> {new_squelch} ({reason})")
        self.squelch = new_squelch
        self._apply_restart(reason=f"squelch set to {new_squelch}")

    def _apply_restart(self, reason: str = ""):
        if not self.enabled or not self.restart_callback:
            return
        now = time.time()
        if now - self.last_restart_time < self.MIN_RESTART_INTERVAL:
            return
        if len(self.ring_buffer) > self.sample_rate // 2:
            recent = np.array(list(self.ring_buffer)[-self.sample_rate // 2:])
            if np.sqrt(np.mean(recent ** 2)) > self.noise_floor * 5:
                return
        self.log(f"[AutoTune] Restarting RTL-SDR ({reason})")
        self.status_message = f"Restarting... ({reason})"
        success = self.restart_callback(self.gain, self.squelch, self.ppm)
        if success:
            self.last_restart_time = now
            self.status_message = (f"Active (gain={self.gain}, "
                                   f"squelch={self.squelch}, ppm={self.ppm})")
            self._save_config()

    def _gain_index(self) -> int:
        diffs = [abs(g - self.gain) for g in GAIN_STEPS]
        return diffs.index(min(diffs))

    def _save_config(self):
        try:
            config = {
                'gain': self.gain,
                'squelch': self.squelch,
                'ppm': self.ppm,
                'noise_floor': float(self.noise_floor),
                'freq_offset': float(self.freq_offset),
                'timestamp': time.time(),
            }
            self.config_path.write_text(json.dumps(config, indent=2))
        except Exception:
            pass

    def _load_config(self):
        try:
            if self.config_path.exists():
                config = json.loads(self.config_path.read_text())
                age = time.time() - config.get('timestamp', 0)
                if age < 7 * 86400:
                    self.gain = config.get('gain', self.gain)
                    self.squelch = config.get('squelch', self.squelch)
                    self.ppm = config.get('ppm', self.ppm)
                    self.noise_floor = config.get('noise_floor', self.noise_floor)
                    self.freq_offset = config.get('freq_offset', self.freq_offset)
        except Exception:
            pass
