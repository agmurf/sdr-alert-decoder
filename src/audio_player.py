"""
Real-time audio playback for the RTL-SDR demodulated stream.

Lets the operator HEAR the received signal (FM hiss + the distinctive
ALERT data-burst "brap") so they can tell by ear whether bursts are
arriving and correlate that with what the decoder reports.

Design:
  - sounddevice OutputStream at 24 kHz mono float32
  - A ring buffer decouples the RTL-SDR 100ms chunks from the audio
    device callback block size
  - Mute toggle: when muted the stream stays open but outputs silence
    (keeps timing stable, no clicks on un-mute)
  - Volume scale so the loud no-squelch FM noise isn't deafening
"""
import numpy as np
import threading
from collections import deque

try:
    import sounddevice as sd
    _HAVE_SD = True
except Exception:
    sd = None
    _HAVE_SD = False


class AudioPlayer:
    def __init__(self, sample_rate=24000, volume=0.35, max_buffer_seconds=2.0):
        self.sample_rate = int(sample_rate)
        self.volume = float(volume)
        self.muted = True  # start muted; user un-mutes when they want to listen
        self._buf = deque(maxlen=int(self.sample_rate * max_buffer_seconds))
        self._lock = threading.Lock()
        self._stream = None
        self.available = _HAVE_SD
        self.error = None

    def start(self) -> bool:
        """Open the audio output stream. Returns True on success."""
        if not _HAVE_SD:
            self.error = "sounddevice not available"
            return False
        if self._stream is not None:
            return True
        try:
            self._stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='float32',
                blocksize=0,            # let the backend choose
                callback=self._callback,
            )
            self._stream.start()
            return True
        except Exception as e:
            self.error = str(e)
            self._stream = None
            return False

    def stop(self):
        """Close the audio output stream."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._buf.clear()

    def feed(self, samples: np.ndarray):
        """Push RTL-SDR audio samples (float32, -1..1) for playback."""
        if not _HAVE_SD or self._stream is None:
            return
        if self.muted:
            # Drop audio while muted so we don't build latency,
            # but keep the stream alive (callback outputs silence).
            return
        with self._lock:
            self._buf.extend(samples)

    def set_muted(self, muted: bool):
        self.muted = bool(muted)
        if self.muted:
            with self._lock:
                self._buf.clear()  # flush so un-mute starts at "now"

    def toggle_mute(self) -> bool:
        self.set_muted(not self.muted)
        return self.muted

    def set_volume(self, volume: float):
        self.volume = max(0.0, min(1.0, float(volume)))

    def _callback(self, outdata, frames, time_info, status):
        # status may flag underruns; ignore — we just output silence then
        if self.muted:
            outdata.fill(0.0)
            return
        with self._lock:
            n = min(frames, len(self._buf))
            if n > 0:
                chunk = np.fromiter(
                    (self._buf.popleft() for _ in range(n)),
                    dtype=np.float32, count=n)
            else:
                chunk = np.empty(0, dtype=np.float32)
        out = np.zeros(frames, dtype=np.float32)
        if len(chunk) > 0:
            out[:len(chunk)] = np.clip(chunk * self.volume, -1.0, 1.0)
        outdata[:] = out.reshape(-1, 1)
