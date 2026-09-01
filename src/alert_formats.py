"""
The three 40-bit ALERT frame formats that share the 300-baud AFSK air
interface. All are four 10-bit UART bytes (start bit, 8 data bits, stop bit);
they differ only in how the 8 data bits of each byte are used.

Source: ALERT2 Application Layer Protocol Specification v1.3, Appendix 2
("ALERT Formats"), sections 5.1-5.3.

  ALERT BINARY (5.1)
      0 1 A5  A4  A3  A2  A1 A0
      0 1 A11 A10 A9  A8  A7 A6
      1 1 D4  D3  D2  D1  D0 A12
      1 1 D10 D9  D8  D7  D6 D5
      Marker bits 01/01/11/11. 13-bit address, 11-bit value. No checksum -
      integrity comes only from those 16 fixed bits.

  ALERT ASCII (5.2)
      x 0 1 1 Au3 Au2 Au1 Au0     each byte is an ASCII digit 0x30-0x39
      x 0 1 1 At3 At2 At1 At0     address = units + 10*tens   (0-99)
      x 0 1 1 Du3 Du2 Du1 Du0     value   = units + 10*tens   (0-99)
      x 0 1 1 Dt3 Dt2 Dt1 Dt0

  ENHANCED IFLOWS (5.3)
      1  1   A5  A4  A3  A2  A1 A0
      D0 A12 A11 A10 A9  A8  A7 A6
      D8 D7  D6  D5  D4  D3  D2 D1
      C0 C1  C2  C3  C4  C5  D10 D9
      Only byte 0 carries marker bits (both set). A 6-bit CRC occupies the
      high bits of byte 3, generator polynomial x^6 + x^4 + x^3 + 1.

WHY THIS MATTERS: the 4078 test rig transmits ENHANCED IFLOWS. Decoding it as
ALERT BINARY put A12 in the wrong place (byte 2 bit 0 rather than byte 1 bit
6), which manufactured a phantom "+4096 flag", and shifted every data bit -
battery read 414 instead of 121 (12.1 V) and DI3 read 36 instead of 528 mm.
Both now match the rig's configuration exactly, and the CRC confirms it on
every frame.

The CRC is also far stronger evidence than the vote-consensus heuristic used
for BINARY frames: a matching 6-bit CRC is a 1-in-64 coincidence per frame,
so an Enhanced IFLOWS frame can be trusted on its own.
"""

# Marker/UART bit positions within the 40-bit frame, MSB-first per byte with
# the start bit first (bit index 0 is byte 0's start bit).
UART_BITS = {0: 0, 9: 1, 10: 0, 19: 1, 20: 0, 29: 1, 30: 0, 39: 1}

BINARY_MARKERS = {7: 1, 8: 0, 17: 1, 18: 0, 27: 1, 28: 1, 37: 1, 38: 1}

FMT_BINARY = "BINARY"
FMT_ASCII = "ASCII"
FMT_ENHANCED_IFLOWS = "ENHANCED_IFLOWS"

CRC6_POLY = 0b011001          # x^6 + x^4 + x^3 + 1 (low six bits)


def frame_bytes(bits40):
    """Extract the four 8-bit data bytes from a 40-bit UART frame.

    Bits are transmitted LSB-first within each byte, which is why bit
    (10*k + 1 + j) carries data bit j.
    """
    out = []
    for k in range(4):
        b = 0
        for j in range(8):
            if bits40[10 * k + 1 + j]:
                b |= (1 << j)
        out.append(b)
    return out


def uart_ok(bits40):
    return all(bits40[i] == v for i, v in UART_BITS.items())


# ------------------------------------------------------------------- CRC-6

def crc6_enhanced(b0, b1, b2, b3):
    """CRC over bytes 0-2 plus D9/D10, LSB-first, reflected output.

    Determined empirically against five real 4078 frames - the specification
    names only the polynomial, not the bit order or reflection. All five
    validate, which for a 6-bit CRC is a 1-in-10^9 coincidence.
    """
    bits = []
    for b in (b0, b1, b2):
        bits.extend((b >> i) & 1 for i in range(8))
    bits.append(b3 & 1)             # D9
    bits.append((b3 >> 1) & 1)      # D10
    reg = 0
    for bit in bits:
        fb = ((reg >> 5) & 1) ^ bit
        reg = (reg << 1) & 0x3F
        if fb:
            reg ^= CRC6_POLY
    return int(f"{reg:06b}"[::-1], 2)      # reflected


# ----------------------------------------------------------------- parsers

def parse_binary(bits40):
    w = frame_bytes(bits40)
    if any(bits40[i] != v for i, v in BINARY_MARKERS.items()):
        return None
    sid = (w[0] & 63) + 64 * (w[1] & 63) + 4096 * (w[2] & 1)
    val = (w[3] & 63) * 32 + ((w[2] & 62) >> 1)
    return {"format": FMT_BINARY, "sensor_id": sid, "value": val,
            "crc_ok": None, "bytes": w}


def parse_ascii(bits40):
    w = frame_bytes(bits40)
    # each byte must be an ASCII digit; the spec's 'x011' marker means the
    # low nibble is the digit and bits 4-6 are 011
    digits = []
    for b in w:
        if (b & 0x70) != 0x30:
            return None
        d = b & 0x0F
        if d > 9:
            return None
        digits.append(d)
    sid = digits[0] + 10 * digits[1]
    val = digits[2] + 10 * digits[3]
    return {"format": FMT_ASCII, "sensor_id": sid, "value": val,
            "crc_ok": None, "bytes": w}


def parse_enhanced_iflows(bits40):
    w = frame_bytes(bits40)
    b0, b1, b2, b3 = w
    if (b0 >> 6) != 0b11:                 # both marker bits set on byte 0
        return None
    sid = (b0 & 63) | ((b1 & 63) << 6) | (((b1 >> 6) & 1) << 12)
    bits = [(b1 >> 7) & 1]                                  # D0
    bits += [(b2 >> k) & 1 for k in range(8)]               # D1..D8
    bits += [b3 & 1, (b3 >> 1) & 1]                         # D9, D10
    val = sum(bit << k for k, bit in enumerate(bits))
    crc = ((b3 >> 7) & 1) << 5 | ((b3 >> 6) & 1) << 4 | ((b3 >> 5) & 1) << 3 \
        | ((b3 >> 4) & 1) << 2 | ((b3 >> 3) & 1) << 1 | ((b3 >> 2) & 1)
    ok = (crc6_enhanced(b0, b1, b2, b3) == crc)
    return {"format": FMT_ENHANCED_IFLOWS, "sensor_id": sid, "value": val,
            "crc_ok": ok, "bytes": w}


PARSERS = (parse_enhanced_iflows, parse_binary, parse_ascii)


def parse_frame_any(bits40, formats=None):
    """Try every enabled format; return the parses that are structurally
    valid. Enhanced IFLOWS results carry crc_ok, which callers should
    require - it is far stronger than vote consensus."""
    if not uart_ok(bits40):
        return []
    out = []
    for p in PARSERS:
        if formats and p is parse_binary and FMT_BINARY not in formats:
            continue
        if formats and p is parse_ascii and FMT_ASCII not in formats:
            continue
        if formats and p is parse_enhanced_iflows and \
                FMT_ENHANCED_IFLOWS not in formats:
            continue
        try:
            r = p(bits40)
        except Exception:
            r = None
        if r and 0 <= r["value"] <= 2047 and 0 <= r["sensor_id"] <= 8191:
            out.append(r)
    return out
