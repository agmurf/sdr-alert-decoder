"""
ALERT Field Application
Real-time ALERT message decoder with GUI for field deployment.
"""
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import threading
import time
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
import queue

from iq_sdr_interface import IQSDRInterface
from iq_decoder import IQAlertDecoder
from alert2_decoder import ALERT2Decoder
from sensor_database import get_sensor_db
from decode_alert import ALERTDecoder
from audio_player import AudioPlayer
import numpy as np
from scipy import signal as _sig


def _resource_dir():
    """Directory containing bundled resources (Sensors.xlsx)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class ALERTFieldApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ALERT Decoder - Field Application v1.0")
        self.root.geometry("1000x700")

        self.rtl_sdr = None
        self.decoder = None
        self.audio_player = AudioPlayer(sample_rate=24000, volume=0.35)
        self.sensor_db = get_sensor_db()

        self.monitoring = False
        self.messages = []
        self.csv_file = None
        self.message_queue = queue.Queue()

        self.setup_gui()
        self.update_display()

    # Shared palette with the Android app so both read as one product.
    BAR_BLUE = "#10469E"
    BTN_BLUE = "#12489F"
    PAGE_GREY = "#F4F4F4"
    TEXT_GREY = "#5F6368"

    def _apply_theme(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        self.root.configure(bg=self.PAGE_GREY)
        style.configure('.', background=self.PAGE_GREY)
        style.configure('TFrame', background=self.PAGE_GREY)
        style.configure('TLabel', background=self.PAGE_GREY,
                        foreground="#202124")
        style.configure('TLabelframe', background=self.PAGE_GREY)
        style.configure('TLabelframe.Label', background=self.PAGE_GREY,
                        foreground=self.TEXT_GREY)
        style.configure('TButton', padding=6)
        # readings table: large, legible, ID and value are the point
        style.configure('Readings.Treeview', rowheight=30,
                        font=('Segoe UI', 12), fieldbackground="white",
                        background="white")
        style.configure('Readings.Treeview.Heading',
                        font=('Segoe UI', 10, 'bold'),
                        background="#E8EEF9", foreground=self.TEXT_GREY)

    def _header_bar(self):
        bar = tk.Frame(self.root, bg=self.BAR_BLUE)
        bar.grid(row=0, column=0, sticky=(tk.W, tk.E))
        tk.Label(bar, text="ALERT Decoder", bg=self.BAR_BLUE, fg="white",
                 font=('Segoe UI', 20, 'bold')).pack(
                     side=tk.LEFT, padx=20, pady=12)
        self.header_status = tk.Label(bar, text="idle", bg=self.BAR_BLUE,
                                      fg="#C8D8F5", font=('Segoe UI', 11))
        self.header_status.pack(side=tk.RIGHT, padx=20)

    def setup_gui(self):
        self._apply_theme()
        self._header_bar()

        control_frame = ttk.Frame(self.root, padding="10")
        control_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N))

        ttk.Label(control_frame, text="Device:").grid(row=0, column=0, sticky=tk.W)
        self.device_status = ttk.Label(control_frame, text="Not connected",
                                       foreground="red")
        self.device_status.grid(row=0, column=1, sticky=tk.W, padx=5)

        ttk.Button(control_frame, text="Check Device",
                   command=self.check_device).grid(row=0, column=2, padx=5)

        self.start_btn = ttk.Button(control_frame, text="Start Monitoring",
                                    command=self.start_monitoring)
        self.start_btn.grid(row=0, column=3, padx=5)

        self.stop_btn = ttk.Button(control_frame, text="Stop Monitoring",
                                   command=self.stop_monitoring,
                                   state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=4, padx=5)

        # Audio listen toggle — hear the received signal to tell by ear
        # whether bursts are arriving / being decoded
        self.audio_btn = ttk.Button(control_frame, text="🔇 Audio: Muted",
                                    command=self.toggle_audio)
        self.audio_btn.grid(row=0, column=5, padx=5)

        # --- Tuning row: frequency (MHz) + PPM correction ---
        ttk.Label(control_frame, text="Freq (MHz):").grid(
            row=1, column=0, sticky=tk.W)
        self.freq_var = tk.StringVar(value="151.500")
        self.freq_entry = ttk.Entry(control_frame, textvariable=self.freq_var,
                                    width=10)
        self.freq_entry.grid(row=1, column=1, sticky=tk.W, padx=5)

        ttk.Label(control_frame, text="PPM:").grid(
            row=1, column=2, sticky=tk.E)
        self.ppm_var = tk.StringVar(value="0")
        self.ppm_entry = ttk.Entry(control_frame, textvariable=self.ppm_var,
                                   width=6)
        self.ppm_entry.grid(row=1, column=3, sticky=tk.W, padx=5)

        ttk.Label(control_frame, text="Gain (dB):").grid(
            row=1, column=4, sticky=tk.E)
        self.gain_var = tk.StringVar(value="40")
        self.gain_entry = ttk.Entry(control_frame, textvariable=self.gain_var,
                                    width=6)
        self.gain_entry.grid(row=1, column=5, sticky=tk.W, padx=5)

        ttk.Label(control_frame, text="Protocol:").grid(
            row=1, column=7, sticky=tk.E, padx=(15, 2))
        # Named for the FRAME FORMAT, not the network. NSW operators call
        # their network "iFLOWS", but its frames are the ALERT Binary format
        # (all four bytes marked, no checksum) - so a label of "iFLOWS" here
        # named neither the format nor anything an operator could check.
        self.protocol_var = tk.StringVar(value="ALERT Binary")
        self.protocol_box = ttk.Combobox(
            control_frame, textvariable=self.protocol_var,
            state="readonly", width=16,
            values=("ALERT Binary", "Enhanced iFLOWS", "ALERT2"))
        self.protocol_box.bind("<<ComboboxSelected>>", self._show_protocol_hint)
        self.protocol_box.grid(row=1, column=8, sticky=tk.W, padx=2)

        # Says which real transmitter each format has been decoded from, so
        # the choice can be made without reading the spec.
        self.protocol_hint = tk.StringVar()
        ttk.Label(control_frame, textvariable=self.protocol_hint,
                  foreground=self.TEXT_GREY).grid(
                      row=2, column=5, columnspan=4, sticky=tk.W, padx=2)
        self._show_protocol_hint()

        self.retune_btn = ttk.Button(control_frame, text="Apply Tuning",
                                     command=self.apply_tuning,
                                     state=tk.DISABLED)
        self.retune_btn.grid(row=1, column=6, padx=5)

        ttk.Label(control_frame, text="Signal:").grid(row=2, column=0, sticky=tk.W)
        self.signal_var = tk.StringVar(value="-100 dB")
        ttk.Label(control_frame, textvariable=self.signal_var).grid(
            row=2, column=1, sticky=tk.W, padx=5)
        self.signal_bar = ttk.Progressbar(control_frame, length=200,
                                          mode='determinate')
        self.signal_bar.grid(row=2, column=2, columnspan=3, sticky=tk.W, padx=5)

        stats_frame = ttk.LabelFrame(self.root, text="Statistics", padding="10")
        stats_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=10, pady=5)
        self.stats_text = tk.StringVar(
            value="Messages: 0 | Runtime: 0s | CSV: Not logging")
        ttk.Label(stats_frame, textvariable=self.stats_text).pack()

        csv_frame = ttk.Frame(stats_frame)
        csv_frame.pack(fill=tk.X, pady=5)
        ttk.Button(csv_frame, text="Start CSV Logging",
                   command=self.start_csv_logging).pack(side=tk.LEFT, padx=5)
        ttk.Button(csv_frame, text="Stop CSV Logging",
                   command=self.stop_csv_logging).pack(side=tk.LEFT)
        ttk.Button(csv_frame, text="Decode WAV File",
                   command=self.decode_wav_file).pack(side=tk.LEFT, padx=20)
        ttk.Button(csv_frame, text="Decode IQ capture (.bin)",
                   command=self.decode_iq_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(csv_frame, text="Calibrate from WAV…",
                   command=self.calibrate_from_wav).pack(side=tk.LEFT, padx=5)

        msg_frame = ttk.LabelFrame(self.root, text="Decoded Messages",
                                   padding="10")
        msg_frame.grid(row=3, column=0, sticky=(tk.W, tk.E, tk.N, tk.S),
                       padx=10, pady=5)
        self.root.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)

        # ID and value first and large - that is what an operator reads.
        columns = ('Sensor', 'Value', 'Time', 'Location')
        self.message_tree = ttk.Treeview(msg_frame, columns=columns,
                                         show='headings', height=15,
                                         style='Readings.Treeview')
        widths = {'Sensor': 110, 'Value': 160, 'Time': 110, 'Location': 420}
        for col in columns:
            self.message_tree.heading(col, text=col)
            self.message_tree.column(col, width=widths[col],
                                     anchor=(tk.W if col == 'Location'
                                             else tk.CENTER))
        self.message_tree.tag_configure('held', foreground='#B26A00')
        scrollbar = ttk.Scrollbar(msg_frame, orient=tk.VERTICAL,
                                  command=self.message_tree.yview)
        self.message_tree.configure(yscrollcommand=scrollbar.set)
        self.message_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        log_frame = ttk.LabelFrame(self.root, text="Log", padding="10")
        log_frame.grid(row=4, column=0, sticky=(tk.W, tk.E), padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, width=80)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.root.after(500, self.check_device)

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)

    def check_device(self):
        self.log("Checking for RTL-SDR device...")
        probe = IQSDRInterface()
        if probe.check_rtl_sdr():
            self.device_status.config(text="RTL-SDR Found", foreground="green")
            self.log("RTL-SDR device detected (direct-FSK IQ mode)")
        else:
            self.device_status.config(
                text="No device / Tools not installed", foreground="orange")
            self.log("RTL-SDR not found. You can still decode WAV files.")
            self.log("For live monitoring, install RTL-SDR tools.")

    PROTOCOL_HINTS = {
        "ALERT Binary": ("all four bytes marked, no checksum - what the live "
                         "151.5 network sends (Kelso Creek, Liverpool)"),
        "Enhanced iFLOWS": ("one byte marked plus a 6-bit CRC - what the "
                            "ERT-A2 test rig sends"),
        "ALERT2": "4800 bps with FEC - a different radio entirely",
    }

    def _show_protocol_hint(self, _event=None):
        self.protocol_hint.set(
            self.PROTOCOL_HINTS.get(self.protocol_var.get(), ""))

    def _get_gain(self):
        """Tuner gain in dB from the GUI. Blank or 'auto' means tuner AGC.

        Exposed because gain is the control for FRONT-END OVERLOAD: a close
        transmitter drives the tuner into compression, which corrupts bit
        decisions while the tones still look clean. It does not improve SNR.
        """
        raw = (self.gain_var.get() or "").strip().lower()
        if raw in ("", "auto", "agc"):
            return -1
        try:
            g = float(raw)
        except ValueError:
            self.gain_var.set("40")
            return 40
        g = max(0.0, min(49.6, g))
        return g

    def _get_tuning(self):
        """Parse frequency (MHz->Hz) and PPM from the GUI; safe defaults."""
        try:
            freq_hz = int(round(float(self.freq_var.get()) * 1e6))
            if not (50e6 < freq_hz < 2e9):
                raise ValueError
        except (ValueError, tk.TclError):
            freq_hz = int(151.5e6)
            self.freq_var.set("151.500")
        try:
            ppm = int(float(self.ppm_var.get()))
        except (ValueError, tk.TclError):
            ppm = 0
            self.ppm_var.set("0")
        return freq_hz, ppm

    def apply_tuning(self):
        """Re-tune the running RTL-SDR to the GUI frequency/PPM live."""
        if not self.monitoring or not self.rtl_sdr:
            return
        freq_hz, ppm = self._get_tuning()
        gain = self._get_gain()
        ok = self.rtl_sdr.restart_with_params(ppm=ppm, frequency=freq_hz,
                                              gain=gain)
        if ok:
            gtxt = "auto" if gain == -1 else f"{gain:g} dB"
            self.log(f"Re-tuned to {freq_hz/1e6:.4f} MHz, PPM={ppm}, "
                     f"gain {gtxt}")
        else:
            self.log("Re-tune failed (RTL-SDR error)")

    # IQ decode parameters
    IQ_RATE = 1024000
    DECODE_WINDOW_S = 3.0

    def start_monitoring(self):
        if self.monitoring:
            return
        freq_hz, ppm = self._get_tuning()
        self.rtl_sdr = IQSDRInterface(frequency=freq_hz,
                                      sample_rate=self.IQ_RATE,
                                      gain=self._get_gain(), ppm=ppm)
        if not self.rtl_sdr.check_rtl_sdr():
            messagebox.showerror("Error", "RTL-SDR not detected.")
            return
        if not self.rtl_sdr.start_streaming():
            messagebox.showerror(
                "Error", "Failed to start rtl_sdr. Check device and drivers.")
            return

        # Protocol decoders. iFLOWS is 300-baud AFSK-over-NBFM; ALERT2 is a
        # completely different PHY (4800 bps, FEC), so they are separate
        # decoders fed the same IQ windows.
        # One protocol at a time. ALERT Binary and Enhanced iFLOWS share the
        # same 300-baud AFSK air interface, and Enhanced iFLOWS is far more
        # loosely constrained (2 marker bits + a 6-bit CRC versus Binary's 16
        # fixed bits), so running both together makes a strong Binary burst
        # yield CRC-valid Enhanced iFLOWS ghosts - measured, it invented two
        # stations on a capture containing exactly four.
        proto = self.protocol_var.get()
        self.decoder = None
        self.decoder2 = None
        if proto == "ALERT Binary":
            self.decoder = IQAlertDecoder(sample_rate=self.IQ_RATE,
                                          callback=self.on_message_decoded,
                                          formats=("BINARY",))
        elif proto == "Enhanced iFLOWS":
            self.decoder = IQAlertDecoder(sample_rate=self.IQ_RATE,
                                          callback=self.on_message_decoded,
                                          formats=("ENHANCED_IFLOWS",))
        elif proto == "ALERT2":
            self.decoder2 = ALERT2Decoder(sample_rate=self.IQ_RATE,
                                          callback=self.on_message_decoded)
        self.log(f"Decoding protocol: {proto}")

        if self.audio_player.start():
            self.log("Audio output ready (muted) - click 'Audio: Muted' to listen")
        else:
            self.log(f"Audio output unavailable: {self.audio_player.error}")

        self.monitoring = True
        self.start_time = time.time()
        self.monitor_thread = threading.Thread(target=self.monitor_loop,
                                               daemon=True)
        self.monitor_thread.start()
        self.audio_thread = threading.Thread(target=self.audio_loop,
                                             daemon=True)
        self.audio_thread.start()

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.retune_btn.config(state=tk.NORMAL)
        self.log(f"=== Monitoring started @ {freq_hz/1e6:.4f} MHz, "
                 f"PPM={ppm} (direct-FSK IQ mode) ===")

    def stop_monitoring(self):
        if not self.monitoring:
            return
        self.monitoring = False
        if self.rtl_sdr:
            self.rtl_sdr.stop_streaming()
        self.audio_player.stop()
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.retune_btn.config(state=tk.DISABLED)
        self.log("=== Monitoring stopped ===")

    def monitor_loop(self):
        """Decoder thread: consume IQ chunks from the queue and decode
        CONSECUTIVE windows so every part of the signal is examined exactly
        once (no gaps). ALERT bursts are brief and periodic - the old
        'latest 3 s' model skipped them between slow decodes."""
        win_n = int(self.IQ_RATE * self.DECODE_WINDOW_S)
        hop_n = int(self.IQ_RATE * 2.5)        # 0.5 s overlap between windows
        buf = np.zeros(0, dtype=np.complex64)
        max_buf = int(self.IQ_RATE * 30)       # cap backlog at 30 s
        last_heartbeat = 0
        while self.monitoring and self.rtl_sdr:
            chunk = self.rtl_sdr.get_chunk(timeout=1.0)
            if chunk is None:
                continue
            buf = np.concatenate([buf, chunk])
            if len(buf) > max_buf:             # decoder fell behind; skip ahead
                buf = buf[-max_buf:]
            while len(buf) >= win_n:
                window = buf[:win_n]
                buf = buf[hop_n:]
                try:
                    before = self.decoder.stats['decodes'] if self.decoder else 0
                    cands = []
                    if self.decoder:
                        self.decoder.decode_window(window)
                        cands = self.decoder.last_candidates
                    got = ((self.decoder.stats['decodes'] - before)
                           if self.decoder else 0)
                    if self.decoder2:
                        n2 = self.decoder2.stats['readings']
                        self.decoder2.decode_window(window)
                        got += self.decoder2.stats['readings'] - n2
                        if not cands:
                            cands = self.decoder2.last_candidates
                    now = time.time()
                    if got == 0 and now - last_heartbeat > 10:
                        last_heartbeat = now
                        if cands:
                            offs = ', '.join(f"{c/1000:+.1f}k" for c in cands[:3])
                            self.log(f"signal present @ 151.5 ({len(cands)} "
                                     f"carrier(s): {offs}Hz) - waiting for "
                                     f"ALERT burst")
                        else:
                            self.log("listening @ 151.5 - no signal in band")
                except Exception as e:
                    self.log(f"decode error: {e}")

    def audio_loop(self):
        """Listen path: a proper NBFM receiver on the +/-6 kHz channel around
        the tuned centre (like SDR++ at 151.5), so the operator hears the
        actual signal - not the whole 1 MHz band."""
        from fsk_pll_decode import predecimate
        while self.monitoring and self.rtl_sdr:
            if self.audio_player.muted:
                time.sleep(0.2)
                continue
            win = self.rtl_sdr.get_window(0.4)
            if win is None:
                time.sleep(0.2)
                continue
            try:
                mid, msr = predecimate(win, self.IQ_RATE)     # -> 64 kHz
                b = _sig.firwin(127, 6000 / (msr / 2))        # NBFM channel
                ch = _sig.lfilter(b, 1, mid)
                fm = np.angle(ch[1:] * np.conj(ch[:-1]))
                a = _sig.resample_poly(fm, 3, 8).astype(np.float32)  # 64k->24k
                m = np.max(np.abs(a))
                if m > 0:
                    a = a / m * 0.5
                self.audio_player.feed(a)
            except Exception:
                pass
            time.sleep(0.35)

    def toggle_audio(self):
        """Mute/unmute live audio playback of the received signal."""
        if not self.audio_player.available:
            self.log("Audio playback unavailable (sounddevice not loaded)")
            return
        muted = self.audio_player.toggle_mute()
        if muted:
            self.audio_btn.config(text="🔇 Audio: Muted")
            self.log("Audio muted")
        else:
            self.audio_btn.config(text="🔊 Audio: On")
            self.log("Audio unmuted - listening to received signal")

    def on_message_decoded(self, message):
        self.message_queue.put(message)

    def start_csv_logging(self):
        filename = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"alert_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        if filename:
            try:
                self.csv_file = open(filename, 'w', newline='')
                self.csv_writer = csv.writer(self.csv_file)
                self.csv_writer.writerow(
                    ['Timestamp', 'Station_ID', 'Sensor_ID', 'Raw_Value',
                     'Sensor_Type', 'Decoded_Value', 'Site_Name'])
                self.log(f"CSV logging started: {filename}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to create CSV: {e}")

    def stop_csv_logging(self):
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
            self.log("CSV logging stopped")

    def decode_wav_file(self):
        filename = filedialog.askopenfilename(
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")])
        if filename:
            self.log(f"Decoding WAV file: {Path(filename).name}")
            threading.Thread(target=self._decode_wav_thread,
                             args=(filename,), daemon=True).start()

    def _decode_wav_thread(self, filename):
        try:
            decoder = ALERTDecoder(min_match_bits=14, weak_match_bits=13)
            results = decoder.decode_wav_file(filename)
            messages = results.get('messages', [])
            for msg in messages:
                self.message_queue.put({
                    'station_id': msg['sensor_id'],
                    'sensor_id': msg['sensor_id'],
                    'value': msg['value'],
                    'match_count': msg.get('match_count', 0),
                })
            stats = results.get('stats', {})
            self.log(f"Decoded {len(messages)} messages from "
                     f"{stats.get('burst_count', 0)} bursts")
        except Exception as e:
            self.log(f"Error decoding WAV: {e}")

    def decode_iq_file(self):
        """Decode a saved rtl_sdr IQ capture (.bin, 8-bit uint8 IQ @ 1.024
        Msps) so you can verify decoding on real data without waiting for
        live bursts."""
        initial = _resource_dir()
        filename = filedialog.askopenfilename(
            title="Select rtl_sdr IQ capture (.bin)",
            initialdir=initial,
            filetypes=[("IQ capture", "*.bin"), ("All files", "*.*")])
        if filename:
            self.log(f"Decoding IQ capture: {Path(filename).name} …")
            threading.Thread(target=self._decode_iq_thread,
                             args=(filename,), daemon=True).start()

    def _decode_iq_thread(self, filename):
        try:
            from iq_decoder import IQAlertDecoder
            raw = np.fromfile(filename, dtype=np.uint8).astype(np.float32)
            x = ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)).astype(
                np.complex64)
            fs = self.IQ_RATE
            self.log(f"  {len(x)/fs:.0f}s of IQ; scanning…")
            seen = set()
            dec = IQAlertDecoder(
                sample_rate=fs,
                callback=lambda m: (
                    self.message_queue.put(m)
                    if (m['sensor_id'], m['value']) not in seen
                    and not seen.add((m['sensor_id'], m['value'])) else None))
            win_n = int(fs * 3.0)
            hop_n = int(fs * 2.5)
            pos = 0
            while pos + win_n <= len(x):
                dec.decode_window(x[pos:pos + win_n])
                pos += hop_n
            self.log(f"  IQ decode complete: {len(seen)} unique readings")
        except Exception as e:
            self.log(f"IQ decode error: {e}")

    def calibrate_from_wav(self):
        """
        Discover the true modulation parameters from clean WAV recordings
        and write calibration_profile.json. Optionally use a known
        station ID/value (from existing hardware) as ground truth.
        """
        files = filedialog.askopenfilenames(
            title="Select clean WAV recording(s) containing real signals",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")])
        if not files:
            return

        # Optional ground-truth dialog
        gt = None
        if messagebox.askyesno(
                "Ground truth?",
                "Do you have a known station ID + value from existing "
                "equipment for a transmission in this recording?\n\n"
                "(Optional, but makes calibration far more reliable.)"):
            from tkinter import simpledialog
            gid = simpledialog.askinteger("Ground truth",
                                          "Known ERRTS sensor ID:")
            if gid is not None:
                gval = simpledialog.askinteger(
                    "Ground truth",
                    "Known value (Cancel to match ID only):")
                gt = (gid, gval)

        self.log(f"Calibrating from {len(files)} file(s) — this runs in the "
                 f"background and may take a few minutes…")
        threading.Thread(target=self._calibrate_thread,
                          args=(list(files), gt), daemon=True).start()

    def _calibrate_thread(self, files, gt):
        try:
            from wav_calibrator import WavCalibrator
            cal = WavCalibrator(log=self.log)
            prof = cal.calibrate(files, ground_truth=gt)
            if prof.get('ok'):
                path = cal.save_profile(prof, _resource_dir())
                self.log(f"[calib] SUCCESS  mark={prof['mark']} "
                         f"space={prof['space']} shift={prof['shift']} "
                         f"baud={prof['baud']} {prof['polarity']} "
                         f"{prof['bit_order']} {prof['format']} "
                         f"votes={prof['votes']}")
                ex = prof.get('example', {})
                msg = (f"Calibration succeeded and was saved.\n\n"
                       f"mark={prof['mark']} Hz  space={prof['space']} Hz\n"
                       f"shift={prof['shift']} Hz  baud={prof['baud']}\n"
                       f"polarity={prof['polarity']}  "
                       f"bit order={prof['bit_order']}\n"
                       f"format={prof['format']}  votes={prof['votes']}\n"
                       f"example: ERRTS {ex.get('sensor_id')} "
                       f"= {ex.get('value')}  {ex.get('site')}\n\n"
                       f"Stop and re-Start Monitoring to apply it.")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Calibration complete", msg))
            else:
                reason = prof.get('reason', 'unknown')
                self.log(f"[calib] FAILED: {reason}")
                self.root.after(0, lambda: messagebox.showwarning(
                    "Calibration failed",
                    f"Could not calibrate:\n\n{reason}"))
        except Exception as e:
            self.log(f"[calib] error: {e}")

    def update_display(self):
        while not self.message_queue.empty():
            try:
                self.add_message_to_display(self.message_queue.get_nowait())
            except queue.Empty:
                break

        if self.monitoring and hasattr(self, 'start_time'):
            runtime = int(time.time() - self.start_time)
            csv_status = "Logging" if self.csv_file else "Not logging"
            info = ""
            if self.decoder is not None:
                s = self.decoder.get_stats()
                info = f" | bursts: {s['bursts']} decodes: {s['decodes']}"
            self.stats_text.set(
                f"Readings: {len(self.messages)} | Runtime: {runtime}s | "
                f"CSV: {csv_status}{info}")
            if self.rtl_sdr:
                signal_db = self.rtl_sdr.signal_dbfs()
                self.signal_var.set(f"{signal_db:.1f} dBFS")
                self.signal_bar['value'] = max(0, min(100, signal_db + 60))

        self.root.after(500, self.update_display)

    def add_message_to_display(self, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        sensor = msg.get('sensor_id', msg.get('station_id', 0))
        value = msg['value']
        match_count = msg.get('match_count', 0)

        # ALERT2 reports carry a 16-bit MANT source address (the site) plus a
        # small per-site sensor id, so they are not ERRTS ids and must not be
        # looked up as such.
        if msg.get('protocol') == 'ALERT2':
            src = msg.get('source_address')
            val = msg.get('value')
            shown = (f"{val:.3f}" if isinstance(val, float) else str(val))
            label = f"ALERT2 site {src} - {msg.get('report_type', 'report')}"
            if msg.get('test_flag'):
                label += "  [TEST DATA]"
            self.message_tree.insert('', 0, values=(
                f"{src}:{sensor}", shown, timestamp, label))
            self.messages.append(msg)
            if self.csv_file:
                self.csv_writer.writerow([timestamp, src, sensor, val,
                                          'ALERT2', shown, label])
                self.csv_file.flush()
            self.log(f"ALERT2: site {src} sensor {sensor} = {shown}")
            return

        sensor_info = self.sensor_db.get_sensor_info(sensor)
        if sensor_info:
            sensor_type = sensor_info['sensor_type']
            site_name = sensor_info['site_name']
            site_number = sensor_info.get('site_number', '')
            decoded_value = self.sensor_db.format_decoded_value(sensor, value)
        else:
            # Station decoded cleanly but isn't in the site register - show it
            # rather than hide it (the register is not exhaustive, and an
            # installed copy ships without one at all, since site registers
            # are agency data and not ours to redistribute).
            sensor_type = "?"
            site_name = ("no site register loaded - raw value"
                         if not getattr(self.sensor_db, 'sensors', None)
                         else "not in site register - raw value")
            site_number = ""
            decoded_value = str(value)

        label = site_name if not sensor_type or sensor_type == "?" \
            else f"{site_name}  ({sensor_type})"
        self.message_tree.insert('', 0, values=(
            sensor, decoded_value, timestamp, label))
        self.messages.append(msg)

        if self.csv_file:
            self.csv_writer.writerow([
                timestamp, site_number, sensor, value,
                sensor_type, decoded_value, site_name])
            self.csv_file.flush()

        quality = f"[{match_count}/16]" if match_count else ""
        self.log(f"Message: ERRTS={sensor} ({sensor_type}), "
                 f"Value={decoded_value} {quality}")


def main():
    # Make Sensors.xlsx discoverable when frozen
    os.chdir(_resource_dir())
    root = tk.Tk()
    app = ALERTFieldApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
