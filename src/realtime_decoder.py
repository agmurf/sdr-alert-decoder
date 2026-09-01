"""
Real-time ALERT decoder.
Buffers a sliding window of audio and decodes ALERT messages,
with deduplication to prevent repeated reports.
"""
import numpy as np
from collections import deque
import time
from typing import List, Optional, Callable
from decode_alert import ALERTDecoder


class RealtimeALERTDecoder:
    def __init__(self, sample_rate=24000, callback=None,
                 buffer_seconds=5.0, decode_interval=1.5,
                 dedup_seconds=30.0):
        self.sample_rate = sample_rate
        self.callback = callback
        self.decoder = ALERTDecoder(min_match_bits=14, weak_match_bits=13)
        self.auto_tuner = None  # set by field_application to share freq_offset

        self.buffer_size = int(sample_rate * buffer_seconds)
        self.audio_buffer = deque(maxlen=self.buffer_size)
        self.samples_since_decode = 0

        self.decode_interval = decode_interval
        self.last_decode_time = 0
        self.samples_per_decode = int(sample_rate * decode_interval)

        self.dedup_seconds = dedup_seconds
        self.recent_messages = {}

        self.messages_decoded = 0
        self.samples_processed = 0
        self.bursts_detected = 0

    def process_audio_chunk(self, audio: np.ndarray):
        """Process incoming audio chunk from RTL-SDR."""
        self.audio_buffer.extend(audio)
        self.samples_processed += len(audio)
        self.samples_since_decode += len(audio)
        if self.samples_since_decode >= self.samples_per_decode:
            self._decode_buffer()
            self.samples_since_decode = 0

    def _decode_buffer(self):
        """Decode current audio buffer for ALERT messages."""
        if len(self.audio_buffer) < self.sample_rate:
            return

        audio = np.array(self.audio_buffer, dtype=np.float32)

        # Apply auto-tuner's frequency offset, and let it measure offset
        if self.auto_tuner is not None:
            self.decoder.freq_offset = getattr(
                self.auto_tuner, 'freq_offset', 0.0)
            try:
                bursts = self.decoder._detect_bursts(audio, self.sample_rate)
                for binfo in bursts:
                    ba = audio[binfo['start_sample']:binfo['end_sample']]
                    if len(ba) > self.sample_rate * 0.25:
                        off = self.auto_tuner.measure_freq_offset(ba)
                        if off is not None:
                            self.auto_tuner.freq_offset_measurements.append(off)
                            self.auto_tuner._check_freq_offset_update()
            except Exception:
                pass

        messages = self.decoder.decode_audio_chunk(audio, self.sample_rate)

        now = time.time()
        self.last_decode_time = now

        expired = [k for k, t in self.recent_messages.items()
                   if now - t > self.dedup_seconds]
        for k in expired:
            del self.recent_messages[k]

        for msg in messages:
            sid = msg['sensor_id']
            val = msg['value']
            key = (sid, val)
            if key in self.recent_messages:
                continue
            self.recent_messages[key] = now
            self.messages_decoded += 1
            if self.callback:
                self.callback({
                    'station_id': sid,
                    'sensor_id': sid,
                    'value': val,
                    'match_count': msg.get('match_count', 0),
                })

    def get_stats(self) -> dict:
        return {
            'samples_processed': self.samples_processed,
            'messages_decoded': self.messages_decoded,
            'buffer_fill': len(self.audio_buffer) / self.buffer_size
                          if self.buffer_size > 0 else 0,
            'dedup_cache_size': len(self.recent_messages),
        }
