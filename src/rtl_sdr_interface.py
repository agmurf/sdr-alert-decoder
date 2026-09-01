"""
RTL-SDR Interface for real-time ALERT decoding.
Wraps rtl_fm as a subprocess and streams FM-demodulated audio.

Key robustness features:
  - No console windows (CREATE_NO_WINDOW) so rtl_fm/rtl_test don't flash cmd windows
  - Idempotent start: kills any stale rtl_fm before spawning a new one
  - Reliable stop: terminates the process and joins the reader thread
  - Safety sweep: kills orphaned rtl_fm.exe processes on start/stop
"""
import numpy as np
import subprocess
import threading
import queue
import time
import sys
from pathlib import Path

# Windows: prevent subprocesses from popping a console window
if sys.platform == 'win32':
    _NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW
else:
    _NO_WINDOW = 0


def _kill_orphan_rtl_fm():
    """Kill any orphaned rtl_fm.exe processes (Windows safety net)."""
    if sys.platform != 'win32':
        return
    try:
        subprocess.run(['taskkill', '/F', '/IM', 'rtl_fm.exe'],
                        capture_output=True, timeout=5,
                        creationflags=_NO_WINDOW)
    except Exception:
        pass


class RTLSDRInterface:
    def __init__(self, frequency=151.5e6, sample_rate=24000, gain=40,
                 squelch=0, ppm=0):
        self.frequency = int(frequency)
        self.sample_rate = int(sample_rate)
        self.gain = gain
        self.squelch = squelch
        self.ppm = ppm
        self.running = False
        self.process = None
        self.data_queue = queue.Queue(maxsize=100)
        self.read_thread = None
        self._lock = threading.Lock()

    def check_rtl_sdr(self) -> bool:
        """Check if RTL-SDR tools and device are available."""
        try:
            result = subprocess.run(['rtl_test', '-t'],
                                    capture_output=True,
                                    timeout=8,
                                    text=True,
                                    creationflags=_NO_WINDOW)
            combined = result.stdout + result.stderr
            return 'Found' in combined
        except subprocess.TimeoutExpired:
            return True  # Device found, test still running
        except FileNotFoundError:
            pass
        try:
            subprocess.run(['rtl_fm'], capture_output=True, timeout=3,
                            text=True, creationflags=_NO_WINDOW)
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def list_devices(self) -> list:
        """List available RTL-SDR devices."""
        try:
            result = subprocess.run(['rtl_test', '-t'],
                                    capture_output=True,
                                    timeout=8,
                                    text=True,
                                    creationflags=_NO_WINDOW)
            combined = result.stdout + result.stderr
            devices = []
            for line in combined.split('\n'):
                if 'Found' in line or 'tuner' in line.lower():
                    devices.append(line.strip())
            return devices
        except subprocess.TimeoutExpired:
            return ['RTL-SDR device detected']
        except FileNotFoundError:
            return []

    def start_streaming(self) -> bool:
        """Start streaming FM-demodulated audio. Idempotent and safe."""
        with self._lock:
            if self.running:
                print("[WARNING] Already streaming — ignoring duplicate start")
                return True

            # Kill any stale/orphaned rtl_fm before spawning a new one
            if self.process is not None:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                self.process = None
            _kill_orphan_rtl_fm()
            time.sleep(0.3)

            cmd = [
                'rtl_fm',
                '-f', str(self.frequency),
                '-M', 'fm',
                '-s', str(self.sample_rate),
                '-r', str(self.sample_rate),
                '-g', str(self.gain),
                '-p', str(self.ppm),
                '-E', 'dc',
            ]
            if self.squelch > 0:
                cmd.extend(['-l', str(self.squelch)])
            cmd.append('-')

            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    creationflags=_NO_WINDOW,
                )
                self.running = True
                self.read_thread = threading.Thread(
                    target=self._read_samples, daemon=True)
                self.read_thread.start()
                print(f"[RTL-SDR] Streaming @ {self.frequency/1e6:.3f} MHz, "
                      f"{self.sample_rate/1000:.0f}k S/s, gain={self.gain}, "
                      f"squelch={self.squelch}, ppm={self.ppm}")
                return True
            except FileNotFoundError:
                print("[ERROR] rtl_fm not found. Install RTL-SDR tools.")
                self.running = False
                return False
            except Exception as e:
                print(f"[ERROR] Failed to start RTL-SDR: {e}")
                self.running = False
                return False

    def _read_samples(self):
        """Background thread: read 100ms chunks of signed-16 audio."""
        chunk_size = int(self.sample_rate * 0.1)
        while self.running and self.process:
            try:
                data = self.process.stdout.read(chunk_size * 2)
                if not data:
                    break
                samples = (np.frombuffer(data, dtype=np.int16)
                           .astype(np.float32) / 32768.0)
                try:
                    self.data_queue.put(samples, block=False)
                except queue.Full:
                    pass
            except Exception:
                break
        print("[RTL-SDR] Reader thread stopped")

    def get_samples(self, timeout=1.0):
        """Get next chunk of audio samples, or None on timeout."""
        try:
            return self.data_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop_streaming(self):
        """Stop streaming. Reliably terminates rtl_fm and joins reader."""
        with self._lock:
            if not self.running and self.process is None:
                return
            print("[RTL-SDR] Stopping...")
            self.running = False

            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2)
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

            _kill_orphan_rtl_fm()

            while not self.data_queue.empty():
                try:
                    self.data_queue.get_nowait()
                except queue.Empty:
                    break
            print("[RTL-SDR] Stopped")

    def restart_with_params(self, gain=None, squelch=None, ppm=None,
                            frequency=None) -> bool:
        """Restart rtl_fm with updated parameters."""
        if gain is not None:
            self.gain = gain
        if squelch is not None:
            self.squelch = squelch
        if ppm is not None:
            self.ppm = ppm
        if frequency is not None:
            self.frequency = int(frequency)
        was_running = self.running
        if was_running:
            self.stop_streaming()
            time.sleep(0.3)
            return self.start_streaming()
        return True

    def get_signal_strength(self) -> float:
        """Approximate signal strength in dB from the last queued chunk."""
        try:
            q = list(self.data_queue.queue)
            if q:
                samples = q[-1]
                if samples is not None and len(samples) > 0:
                    rms = np.sqrt(np.mean(samples ** 2))
                    return 20 * np.log10(rms + 1e-10)
            return -100.0
        except Exception:
            return -100.0


if __name__ == "__main__":
    rtl = RTLSDRInterface()
    print("RTL-SDR found:", rtl.check_rtl_sdr())
    for d in rtl.list_devices():
        print(" ", d)
