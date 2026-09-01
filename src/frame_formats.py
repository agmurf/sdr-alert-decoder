"""
ALERT / ERRTS frame-format decoders.

All ERRTS messages are 4 x 10-bit UART words = 40 bits total.
Per word w (w = 0..3), occupying bit indices [w*10 .. w*10+9]:
    idx w*10+0          : START bit (0)
    idx w*10+1+k (k0-7) : data bit k, LSB-FIRST  -> byte Bw = sum bit_k<<k
    idx w*10+9          : STOP bit (1)
The two MSBs of each data byte are the format/check bits:
    B6 = bit value 0x40 = bit at idx w*10+7
    B7 = bit value 0x80 = bit at idx w*10+8

The message TYPE is identified by bits 6,7 (MSB-first) of byte 1:
    00 EXTENDED   01 BINARY   10 ASCII   11 ENHANCED BINARY
(Ref: BoM ERRTS spec 7/88; OneRain "Decoding ALERT" alert_bin.pl;
 ELPRO ERRTS Protocol Document Appendix F.)

Each parser takes the 40-bit list and returns a dict or None:
  {format, sensor_id, value, framing_score (0-8), aux{...}}
framing_score = number of the 8 format/check bits that match the
format's expected pattern (used by the calibrator's vote metric;
8/8 == perfect framing for that format).
"""
from typing import List, Optional, Dict

# ---- low-level helpers -----------------------------------------------------

def _bytes_from_bits(bits40: List[int]):
    """Return the four UART data bytes (LSB-first) or None if length wrong."""
    if len(bits40) < 40:
        return None
    out = []
    for w in range(4):
        b = 0
        base = w * 10
        for k in range(8):
            if bits40[base + 1 + k]:
                b |= (1 << k)
        out.append(b)
    return out


def _fmt2(byte_):
    """Return the (B7,B6) format pair as a 2-tuple of 0/1 (MSB first)."""
    return ((byte_ >> 7) & 1, (byte_ >> 6) & 1)


def _score(pairs_expected, words):
    """Count how many of the 8 format/check bits match expectation."""
    s = 0
    for exp, w in zip(pairs_expected, words):
        got = _fmt2(w)
        s += (got[0] == exp[0]) + (got[1] == exp[1])
    return s


# ---- BINARY  (Format A) ----------------------------------------------------
# Word format pairs (B7,B6): w1=01 w2=01 w3=11 w4=11
#   -> byte&0xC0:  0x40, 0x40, 0xC0, 0xC0
# 13-bit sensor ID, 11-bit accumulator value. The standard flood format.
_BINARY_A_PAIRS = [(0, 1), (0, 1), (1, 1), (1, 1)]

def parse_binary_a(bits40: List[int]) -> Optional[Dict]:
    wb = _bytes_from_bits(bits40)
    if wb is None:
        return None
    b1, b2, b3, b4 = wb
    sid = (b1 & 0x3F) + 64 * (b2 & 0x3F) + 4096 * (b3 & 0x01)
    val = (b4 & 0x3F) * 32 + ((b3 & 0x3E) >> 1)
    return {
        'format': 'BINARY',
        'sensor_id': sid,
        'value': val,
        'framing_score': _score(_BINARY_A_PAIRS, wb),
        'aux': {},
    }


# ---- BINARY  Format C  (Modified: High Data bits + Battery Status) ---------
# Words 1,2 unchanged (01,01). Words 3,4 check bits changed 11 -> 01.
#   -> byte&0xC0: 0x40, 0x40, 0x40, 0x40
# Same 13-bit ID as the preceding Format-A check message. Carries the 5
# high accumulator bits (bits 11-15 of a 16-bit counter), a 4-bit battery
# status code and a data-error flag.  (Ref ERRTS spec 21.4.3 / chapter 20.)
# The exact in-byte placement of the high/battery bits is only loosely
# specified in the BoM document; ID extraction (what the calibrator votes
# on) is unambiguous, aux fields are best-effort.
_BINARY_C_PAIRS = [(0, 1), (0, 1), (0, 1), (0, 1)]

# Battery-status nibble -> approx volts (ERRTS chapter 20 table; 0.15V steps
# from >12.90V at 0000 down to <10.75V at 1111, with the documented
# irregular 1101=10.90 / 1110=11.80 entries preserved verbatim).
_BATT_TABLE = {
    0x0: 12.95, 0x1: 12.75, 0x2: 12.60, 0x3: 12.45, 0x4: 12.30,
    0x5: 12.15, 0x6: 12.00, 0x7: 11.85, 0x8: 11.70, 0x9: 11.55,
    0xA: 11.40, 0xB: 11.25, 0xC: 11.10, 0xD: 10.90, 0xE: 11.80,
    0xF: 10.70,
}

def parse_binary_c(bits40: List[int]) -> Optional[Dict]:
    wb = _bytes_from_bits(bits40)
    if wb is None:
        return None
    b1, b2, b3, b4 = wb
    # ID is the same 13-bit field layout as Format A.
    sid = (b1 & 0x3F) + 64 * (b2 & 0x3F) + 4096 * (b3 & 0x01)
    batt_code = b4 & 0x0F
    high_bits = (b4 >> 1) & 0x1F          # 5 high accumulator bits (approx)
    data_err = (b4 >> 5) & 0x01
    return {
        'format': 'BINARY_C',
        'sensor_id': sid,
        'value': high_bits,               # high accumulator bits
        'framing_score': _score(_BINARY_C_PAIRS, wb),
        'aux': {
            'battery_code': batt_code,
            'battery_volts': _BATT_TABLE.get(batt_code),
            'data_error': bool(data_err),
            'high_data_bits': high_bits,
        },
    }


# ---- ENHANCED BINARY  (wind / gust, optional 6-bit CRC) -------------------
# Word 1 format bits = 11  -> byte1 & 0xC0 == 0xC0.
# 12 sensor-ID bits (so IDs 0-4095 only), 6 wind-direction bits, 5 velocity
# bits, a battery-status bit and 6 CRC/relative-gust bits. ENHANCED carries
# NO extra fixed check bits (those positions hold the CRC), so only word 1
# has a verifiable format signature -> max framing_score for this format is
# scaled to the 8-bit scale by counting word-1 (2 bits) x4.
# (Ref ERRTS spec 21.5, Figure 4.)

def parse_enhanced(bits40: List[int]) -> Optional[Dict]:
    wb = _bytes_from_bits(bits40)
    if wb is None:
        return None
    b1, b2, b3, b4 = wb
    # Only word 1's "11" identifier is fixed; require it.
    if _fmt2(b1) != (1, 1):
        return None
    # 12-bit ID: low 6 bits of B1, next 6 bits from B2.
    sid = (b1 & 0x3F) + 64 * (b2 & 0x3F)          # 0..4095
    velocity = b3 & 0x1F                           # 5-bit velocity accumulator
    wind_dir = ((b3 >> 5) & 0x07) | ((b4 & 0x07) << 3)  # 6-bit wind counter
    crc_gust = (b4 >> 3) & 0x3F                    # 6-bit CRC / rel. gust
    batt = (b4 >> 5) & 0x01
    # Word-1 contributes 2 verifiable bits; scale to the common 0-8 range
    # so the calibrator can compare formats on one scale.
    fs = 8 if _fmt2(b1) == (1, 1) else 0
    return {
        'format': 'ENHANCED',
        'sensor_id': sid,
        'value': velocity,
        'framing_score': fs,
        'aux': {
            'wind_dir': wind_dir,
            'velocity': velocity,
            'crc_gust': crc_gust,
            'battery_bit': batt,
        },
    }


# ---- ASCII  ---------------------------------------------------------------
# Word 1 format bits = 10  -> byte1 & 0xC0 == 0x80. The ERRTS doc states
# ASCII check bits are 1,1,0,1 across the words and the payload is 7-bit
# printable ASCII. Not used by the Australian BoM network, so it cannot be
# validated against the ERRTS sensor database; the calibrator treats a run
# of correctly-framed printable characters as the (weaker) validity signal.
_ASCII_PAIRS = [(1, 0), (1, 0), (0, 1), (1, 1)]  # "10 10 01 11" best-effort

def parse_ascii(bits40: List[int]) -> Optional[Dict]:
    wb = _bytes_from_bits(bits40)
    if wb is None:
        return None
    chars = [w & 0x7F for w in wb]                 # low 7 bits = ASCII
    printable = all(32 <= c < 127 for c in chars)
    if not printable:
        return None
    return {
        'format': 'ASCII',
        'sensor_id': None,                         # no numeric ID in a frame
        'value': None,
        'framing_score': _score(_ASCII_PAIRS, wb),
        'aux': {'text': ''.join(chr(c) for c in chars)},
    }


# ---- registry -------------------------------------------------------------
# name -> (parser, db_validatable, perfect_framing_score)
# db_validatable: whether sensor_id can be checked against Sensors.xlsx
FRAME_PARSERS = {
    'BINARY':   (parse_binary_a, True, 8),
    'BINARY_C': (parse_binary_c, True, 8),
    'ENHANCED': (parse_enhanced, True, 8),
    'ASCII':    (parse_ascii,    False, 8),
}

# Default for the live decoder when no calibration profile exists.
DEFAULT_FORMAT = 'BINARY'
