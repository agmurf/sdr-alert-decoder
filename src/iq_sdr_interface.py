"""
RTL-SDR raw-IQ streaming interface (rtl_sdr, not rtl_fm).

The iFLOWS ALERT signal is direct narrow FSK, which must be demodulated
from complex IQ - not from rtl_fm audio. This streams 8-bit IQ from
rtl_sdr into a rolling complex buffer that a decoder thread samples.
"""
import numpy as np
import os
import subprocess
import threading
import time
import sys
import queue

if sys.platform == 'win32':
    _NO_WINDOW = 0x08000000
else:
    _NO_WINDOW = 0


def _kill_orphans():
    if sys.platform != 'win32':
        return
    for img in ('rtl_sdr.exe', 'rtl_fm.exe'):
        try:
            subprocess.run(['taskkill', '/F', '/IM', img],
                           capture_output=True, timeout=5,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass


def _rtl_tool(name):
    """Absolute path to an rtl-sdr tool.

    Looks on PATH first, then in an `rtl-sdr` folder shipped beside the app
    (or beside this source tree). Bundling the tools means a new machine only
    needs the folder copied - no PATH surgery, and no silent "rtl_sdr not
    found" when the app is moved between PCs.
    """
    import shutil
    exe = name if name.lower().endswith('.exe') else name + '.exe'
    found = shutil.which(name) or shutil.which(exe)
    if found:
        return found
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [here, os.path.dirname(here)]
    if getattr(sys, 'frozen', False):
        roots.insert(0, os.path.dirname(sys.executable))
    for root in roots:
        cand = os.path.join(root, 'rtl-sdr', exe)
        if os.path.exists(cand):
            return cand
    return name          # let the OS report it


class IQSDRInterface:
    def __init__(self, frequency=151.5e6, sample_rate=1024000, gain=40,
                 ppm=0, buffer_seconds=4.0):
        self.frequency = int(frequency)
        self.sample_rate = int(sample_rate)
        self.gain = gain
        self.ppm = ppm
        self.running = False
        self.process = None
        self.read_thread = None
        self._lock = threading.Lock()
        self._buf_lock = threading.Lock()
        self.maxlen = int(self.sample_rate * buffer_seconds)
        self._buf = np.zeros(0, dtype=np.complex64)
        self.last_rms = 0.0
        # Queue of IQ chunks for the decoder so it can process EVERY window
        # consecutively (no gaps), even if decode is briefly slower than
        # real-time. ~30 s of capacity; oldest dropped if the decoder falls
        # badly behind. The rolling _buf above is only for the audio path.
        self.chunk_q = queue.Queue(maxsize=300)   # ~30 s at 0.1 s/chunk
        self.dropped_chunks = 0

    def check_rtl_sdr(self) -> bool:
        try:
            r = subprocess.run([_rtl_tool('rtl_test'), '-t'], capture_output=True,
                               timeout=8, text=True, creationflags=_NO_WINDOW)
            return 'Found' in (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            return True
        except FileNotFoundError:
            return False

    def start_streaming(self) -> bool:
        with self._lock:
            if self.running:
                return True
            if self.process is not None:
                try:
                    self.process.terminate(); self.process.wait(timeout=2)
                except Exception:
                    pass
                self.process = None
            _kill_orphans()
            time.sleep(0.3)
            cmd = [_rtl_tool('rtl_sdr'), '-f', str(self.frequency),
                   '-s', str(self.sample_rate), '-g', str(self.gain),
                   '-p', str(self.ppm), '-']
            try:
                self.process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    bufsize=0, creationflags=_NO_WINDOW)
            except FileNotFoundError:
                print("[ERROR] rtl_sdr not found.")
                return False
            self.running = True
            self.read_thread = threading.Thread(target=self._reader,
                                                daemon=True)
            self.read_thread.start()
            print(f"[IQ-SDR] streaming {self.frequency/1e6:.4f} MHz @ "
                  f"{self.sample_rate/1e6:.3f} Msps gain={self.gain} "
                  f"ppm={self.ppm}")
            return True

    def _reader(self):
        # read ~0.1 s blocks of uint8 IQ, append complex to rolling buffer.
        # rtl_sdr emits a brief direct-sampling init wobble before it locks;
        # tolerate a few short/empty reads during startup instead of bailing.
        block = int(self.sample_rate * 0.1) * 2
        empty_streak = 0
        while self.running and self.process:
            try:
                data = self.process.stdout.read(block)
                if not data:
                    empty_streak += 1
                    if empty_streak > 30:        # ~ truly dead
                        break
                    time.sleep(0.05)
                    continue
                empty_streak = 0
                raw = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
                if len(raw) < 2:
                    continue
                if len(raw) % 2:
                    raw = raw[:-1]
                iq = ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)
                      ).astype(np.complex64)
                self.last_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
                with self._buf_lock:
                    self._buf = np.concatenate([self._buf, iq])
                    if len(self._buf) > self.maxlen:
                        self._buf = self._buf[-self.maxlen:]
                # feed the decoder queue (drop oldest if the decoder is behind)
                try:
                    self.chunk_q.put_nowait(iq)
                except queue.Full:
                    try:
                        self.chunk_q.get_nowait()
                        self.dropped_chunks += 1
                        self.chunk_q.put_nowait(iq)
                    except queue.Empty:
                        pass
            except Exception as e:
                print(f"[IQ-SDR] reader error: {e}")
                break
        print("[IQ-SDR] reader stopped")

    def get_window(self, seconds):
        """Return the most recent `seconds` of IQ (complex64), or None.
        (Used by the audio path; the decoder uses the chunk queue instead.)"""
        n = int(self.sample_rate * seconds)
        with self._buf_lock:
            if len(self._buf) < n:
                return None
            return self._buf[-n:].copy()

    def get_chunk(self, timeout=1.0):
        """Pop the next IQ chunk from the decoder queue (consecutive, no
        gaps), or None on timeout."""
        try:
            return self.chunk_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def signal_dbfs(self):
        return 20 * np.log10(self.last_rms / 128.0 + 1e-9)

    def restart_with_params(self, gain=None, ppm=None, frequency=None):
        if gain is not None:
            self.gain = gain
        if ppm is not None:
            self.ppm = ppm
        if frequency is not None:
            self.frequency = int(frequency)
        if self.running:
            self.stop_streaming()
            time.sleep(0.3)
            return self.start_streaming()
        return True

    def stop_streaming(self):
        with self._lock:
            if not self.running and self.process is None:
                return
            self.running = False
            if self.process:
                try:
                    self.process.terminate(); self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                except Exception:
                    pass
                self.process = None
            if self.read_thread:
                self.read_thread.join(timeout=2)
                self.read_thread = None
            _kill_orphans()
            with self._buf_lock:
                self._buf = np.zeros(0, dtype=np.complex64)
            print("[IQ-SDR] stopped")


if __name__ == '__main__':
    iq = IQSDRInterface()
    print('rtl-sdr present:', iq.check_rtl_sdr())
