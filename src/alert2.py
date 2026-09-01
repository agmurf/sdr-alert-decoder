"""
ALERT2 AirLink protocol - bit-level codec.

Built from the primary specifications (now in Manuals/):
  ALERT2 AirLink Layer Specification v1.1 (March 2012), NHWC ALERT2 TWG
  ALERT2 MANT Layer Protocol Specification v1.2
  ALERT2 Application Layer Protocol Specification v1.3

PHY (AirLink 2.2-2.3):
  FSK, FM-modulated audio into a COTS transceiver
  4800 bps +/- 3%, NRZ, MSB of each byte first, most significant byte first
  root-raised-cosine pulse shaping, T = 1/4800, beta = 0.96
  bit sync / correlation  : 48 bits  0xEB90B433AAAA
                            (NASA 0xEB90 + CCIR M.903-2 0xB433 + 0xAAAA)
  frame sync (CCSDS ASM)  : 32 bits  0x352EF853
  bit synchronous - NO start/stop bits (unlike ALERT1/iFLOWS)

Coding chain, transmit order (AirLink 3.x):
  AirLink header (2 bytes: 2-bit version=0, 4-bit reserved=0, 10-bit length)
  -> block:  first block 24 payload bytes, follow-on 32, final partial 1-31
  -> scramble each block   17-bit MLS, X^17 + X^3 + 1, preload 0x01,
                           re-initialised per block, BEFORE Reed-Solomon
  -> Reed-Solomon          shortened RS(255,239), 8-bit symbols, 16 parity,
                           corrects 8 symbol errors, NO symbol interleaving
  -> convolutional         rate 1/2, k=7, polynomials 0x6D and 0x4F,
                           second output NOT inverted (the spec's one
                           variation from the NASA standard). The first block
                           is encoded separately from the combined follow-on
                           blocks so a receiver can decode the header early.

UNVERIFIED CHOICES - the spec defers Reed-Solomon detail to "Section 3.2 of
the CCSDS Blue Book" without restating the field polynomial or first
consecutive root, so those are CONFIGURABLE here and default to the common
0x11D / fcr=0. The convolutional register/bit convention is likewise a
reading of the spec's figure. Everything below round-trips against its own
encoder, which proves self-consistency but NOT agreement with a real
transmitter. Confirm against live ALERT2 traffic before trusting field
decodes - see alert2_probe.py, which sweeps the ambiguous parameters.
"""
import numpy as np

BAUD = 4800
RRC_BETA = 0.96
BIT_SYNC = 0xEB90B433AAAA          # 48 bits
BIT_SYNC_LEN = 48
FRAME_SYNC = 0x352EF853            # 32 bits, CCSDS ASM
FRAME_SYNC_LEN = 32

FIRST_BLOCK_PAYLOAD = 24           # includes the 2-byte AirLink header
FOLLOW_BLOCK_PAYLOAD = 32
RS_PARITY = 16


# ---------------------------------------------------------------- bit helpers

def bits_of_int(value, nbits):
    """MSB-first bit list of an integer."""
    return [(value >> (nbits - 1 - i)) & 1 for i in range(nbits)]


def bytes_to_bits(data):
    """MSB-first bits, most significant byte first (AirLink 2.2.5)."""
    out = []
    for b in data:
        out.extend((b >> (7 - i)) & 1 for i in range(8))
    return out


def bits_to_bytes(bits):
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        v = 0
        for j in range(8):
            v = (v << 1) | (bits[i + j] & 1)
        out.append(v)
    return bytes(out)


# ------------------------------------------------------- MLS scrambler (3.3)

def mls_scramble(data, preload=0x01):
    """17-bit maximal length sequence, tap polynomial X^17 + X^3 + 1.

    XOR with a keystream, so the same call both scrambles and descrambles.
    The register is re-initialised per block by the caller (spec 3.3).
    """
    reg = preload & 0x1FFFF
    out = bytearray()
    for byte in data:
        v = 0
        for i in range(8):
            fb = ((reg >> 16) ^ (reg >> 2)) & 1      # taps X^17 and X^3
            reg = ((reg << 1) | fb) & 0x1FFFF
            v = (v << 1) | (((byte >> (7 - i)) & 1) ^ fb)
        out.append(v)
    return bytes(out)


# ------------------------------------------------- Reed-Solomon over GF(256)

class GF256:
    def __init__(self, prim=0x11D):
        self.exp = [0] * 512
        self.log = [0] * 256
        x = 1
        for i in range(255):
            self.exp[i] = x
            self.log[x] = i
            x <<= 1
            if x & 0x100:
                x ^= prim
        for i in range(255, 512):
            self.exp[i] = self.exp[i - 255]

    def mul(self, a, b):
        if a == 0 or b == 0:
            return 0
        return self.exp[self.log[a] + self.log[b]]

    def div(self, a, b):
        if b == 0:
            raise ZeroDivisionError("divide by zero in GF(256)")
        if a == 0:
            return 0
        return self.exp[(self.log[a] - self.log[b]) % 255]

    def inv(self, a):
        return self.exp[(255 - self.log[a]) % 255]

    def pow(self, a, n):
        if a == 0:
            return 0
        return self.exp[(self.log[a] * n) % 255]


class ReedSolomon:
    """Shortened systematic RS(255, 255-nparity); parity appended."""

    def __init__(self, nparity=RS_PARITY, prim=0x11D, fcr=0, generator=2):
        self.gf = GF256(prim)
        self.nparity = nparity
        self.fcr = fcr
        self.generator = generator
        self.gen = self._make_generator(nparity)

    def _make_generator(self, nparity):
        """Generator polynomial in DESCENDING degree order with a leading 1,
        which is what the synthetic-division encoder below expects."""
        gf = self.gf
        g = [1]
        for i in range(nparity):
            root = gf.pow(self.generator, i + self.fcr)
            ng = [0] * (len(g) + 1)
            for j, c in enumerate(g):
                ng[j] ^= c                       # multiply by x
                ng[j + 1] ^= gf.mul(c, root)     # multiply by the root
            g = ng
        return g

    def encode(self, data):
        gf = self.gf
        work = list(data) + [0] * self.nparity
        for i in range(len(data)):
            coef = work[i]
            if coef == 0:
                continue
            for j in range(1, len(self.gen)):
                work[i + j] ^= gf.mul(self.gen[j], coef)
        return bytes(data) + bytes(work[len(data):])

    def _syndromes(self, msg):
        gf = self.gf
        out = []
        for i in range(self.nparity):
            x = gf.pow(self.generator, i + self.fcr)
            acc = 0
            for c in msg:
                acc = gf.mul(acc, x) ^ c
            out.append(acc)
        return out

    # --- polynomial helpers, all DESCENDING order (poly[0] = highest degree)

    def _poly_scale(self, p, x):
        return [self.gf.mul(c, x) for c in p]

    def _poly_add(self, a, b):
        """XOR two descending polynomials, right-aligned."""
        n = max(len(a), len(b))
        out = [0] * n
        for i, c in enumerate(a):
            out[i + n - len(a)] ^= c
        for i, c in enumerate(b):
            out[i + n - len(b)] ^= c
        return out

    def _poly_eval(self, p, x):
        acc = 0
        for c in p:
            acc = self.gf.mul(acc, x) ^ c
        return acc

    def _poly_mul(self, a, b):
        gf = self.gf
        out = [0] * (len(a) + len(b) - 1)
        for i, ca in enumerate(a):
            if ca == 0:
                continue
            for j, cb in enumerate(b):
                out[i + j] ^= gf.mul(ca, cb)
        return out

    def decode(self, msg):
        """Return (data_bytes, n_corrected) or (None, -1) if unrecoverable."""
        gf = self.gf
        msg = list(msg)
        n = len(msg)
        synd = self._syndromes(msg)
        if all(s == 0 for s in synd):
            return bytes(msg[:-self.nparity]), 0

        # --- Berlekamp-Massey (descending polynomials; the scale-and-add
        #     update runs on EVERY nonzero discrepancy, not only when the
        #     degree did not grow - getting that wrong zeroes the leading 1).
        err_loc = [1]
        old_loc = [1]
        for i in range(self.nparity):
            delta = synd[i]
            for j in range(1, len(err_loc)):
                delta ^= gf.mul(err_loc[-(j + 1)], synd[i - j])
            old_loc = old_loc + [0]
            if delta != 0:
                if len(old_loc) > len(err_loc):
                    new_loc = self._poly_scale(old_loc, delta)
                    old_loc = self._poly_scale(err_loc, gf.inv(delta))
                    err_loc = new_loc
                err_loc = self._poly_add(
                    err_loc, self._poly_scale(old_loc, delta))

        while len(err_loc) > 1 and err_loc[0] == 0:
            err_loc.pop(0)
        nerr = len(err_loc) - 1
        if nerr <= 0 or nerr * 2 > self.nparity:
            return None, -1

        # Work in ASCENDING order from here: sig[k] is the coefficient of x^k.
        sig = err_loc[::-1]

        # --- Chien search over actual codeword positions. Byte p carries
        #     locator X = alpha^(n-1-p) and sigma vanishes at X^-1. Searching
        #     alpha^i for i in range(n) misses it entirely, because X^-1 is
        #     alpha^(255-(n-1-p)) which is far outside that range.
        positions = []
        for p in range(n):
            x_inv = gf.inv(gf.pow(self.generator, (n - 1 - p) % 255))
            acc = 0
            for k, c in enumerate(sig):
                acc ^= gf.mul(c, gf.pow(x_inv, k))
            if acc == 0:
                positions.append(p)
        if len(positions) != nerr:
            return None, -1

        # --- Forney. omega(x) = [S(x) * sigma(x)] mod x^nparity, ascending.
        omega = [0] * self.nparity
        for k in range(self.nparity):
            acc = 0
            for j, c in enumerate(sig):
                if 0 <= k - j < self.nparity:
                    acc ^= gf.mul(c, synd[k - j])
            omega[k] = acc

        for p in positions:
            x = gf.pow(self.generator, (n - 1 - p) % 255)
            x_inv = gf.inv(x)
            num = 0
            for k, c in enumerate(omega):
                num ^= gf.mul(c, gf.pow(x_inv, k))
            # formal derivative in GF(2): only odd-degree terms survive
            den = 0
            for k in range(1, len(sig), 2):
                den ^= gf.mul(sig[k], gf.pow(x_inv, k - 1))
            if den == 0:
                return None, -1
            corr = gf.div(num, den)
            corr = gf.mul(corr, gf.pow(x, 1 - self.fcr))
            msg[p] ^= corr

        if any(s != 0 for s in self._syndromes(msg)):
            return None, -1
        return bytes(msg[:-self.nparity]), len(positions)


# --------------------------------------- convolutional code, rate 1/2, k=7

CC_POLY_A = 0x6D
CC_POLY_B = 0x4F
CC_K = 7
CC_STATES = 1 << (CC_K - 1)


def _parity(x):
    x ^= x >> 4
    x ^= x >> 2
    x ^= x >> 1
    return x & 1


def cc_encode(bits, tail=True):
    """Rate 1/2, k=7. Second output NOT inverted (AirLink 3.4.2)."""
    reg = 0
    out = []
    seq = list(bits) + ([0] * (CC_K - 1) if tail else [])
    for b in seq:
        reg = ((reg << 1) | (b & 1)) & 0x7F
        out.append(_parity(reg & CC_POLY_A))
        out.append(_parity(reg & CC_POLY_B))
    return out


# Precomputed transition table: (state, bit) -> (next_state, out_a, out_b)
_CC_TRANS = []
for _st in range(CC_STATES):
    row = []
    for _b in (0, 1):
        _reg = ((_st << 1) | _b) & 0x7F
        row.append((_reg & (CC_STATES - 1),
                    _parity(_reg & CC_POLY_A),
                    _parity(_reg & CC_POLY_B)))
    _CC_TRANS.append(row)


def cc_decode(soft, nbits):
    """Soft-input Viterbi. `soft[2t]`, `soft[2t+1]` are branch values where a
    POSITIVE value means the bit is more likely a 1. Returns nbits bits."""
    total = len(soft) // 2
    if total == 0:
        return []
    INF = float("inf")
    metric = [INF] * CC_STATES
    metric[0] = 0.0
    back = np.zeros((total, CC_STATES), dtype=np.int8)
    for t in range(total):
        a = soft[2 * t]
        b = soft[2 * t + 1]
        new = [INF] * CC_STATES
        for st in range(CC_STATES):
            m = metric[st]
            if m == INF:
                continue
            for bit in (0, 1):
                ns, ea, eb = _CC_TRANS[st][bit]
                # reward agreement between expected and received soft value
                cost = m - (a if ea else -a) - (b if eb else -b)
                if cost < new[ns]:
                    new[ns] = cost
                    back[t, ns] = st
        metric = new
    st = 0 if metric[0] != INF else int(np.argmin(metric))
    bits = [0] * total
    for t in range(total - 1, -1, -1):
        prev = int(back[t, st])
        # the input bit is the LSB of the state we moved into
        bits[t] = st & 1
        st = prev
    return bits[:nbits]


# --------------------------------------------------------- framing helpers

def airlink_header(payload_len, version=0):
    """2 control bytes: 2-bit version, 4-bit reserved, 10-bit length."""
    v = ((version & 3) << 14) | (payload_len & 0x3FF)
    return bytes([(v >> 8) & 0xFF, v & 0xFF])


def parse_airlink_header(two_bytes):
    v = (two_bytes[0] << 8) | two_bytes[1]
    return {"version": (v >> 14) & 3,
            "reserved": (v >> 10) & 0xF,
            "length": v & 0x3FF}


def preamble_bits():
    """Bit-sync/correlation pattern followed by the CCSDS frame sync."""
    return (bits_of_int(BIT_SYNC, BIT_SYNC_LEN)
            + bits_of_int(FRAME_SYNC, FRAME_SYNC_LEN))


# ------------------------------------------------------- block level codec

def encode_frame(payload, rs=None):
    """AirLink payload bytes -> transmitted bit list (after preamble).

    Blocks, scrambles, Reed-Solomon and convolutionally encodes per AirLink
    3.x. The first block is CC-encoded separately from the follow-on stream.
    """
    rs = rs or ReedSolomon()
    hdr = airlink_header(len(payload))
    body = hdr + bytes(payload)

    first = body[:FIRST_BLOCK_PAYLOAD]
    first = first + bytes(FIRST_BLOCK_PAYLOAD - len(first))   # pad
    rest = body[FIRST_BLOCK_PAYLOAD:]

    blocks_rest = [rest[i:i + FOLLOW_BLOCK_PAYLOAD]
                   for i in range(0, len(rest), FOLLOW_BLOCK_PAYLOAD)]

    out_bits = list(cc_encode(bytes_to_bits(rs.encode(mls_scramble(first)))))
    follow_bits = []
    for blk in blocks_rest:
        follow_bits.extend(bytes_to_bits(rs.encode(mls_scramble(blk))))
    if follow_bits:
        out_bits.extend(cc_encode(follow_bits))
    return out_bits


def decode_frame(soft_bits, rs=None, max_blocks=8):
    """Inverse of encode_frame. `soft_bits` are post-sync soft values.

    Returns dict with header, payload and diagnostics, or None.
    """
    rs = rs or ReedSolomon()
    first_data_bits = (FIRST_BLOCK_PAYLOAD + RS_PARITY) * 8
    need = (first_data_bits + (CC_K - 1)) * 2
    if len(soft_bits) < need:
        return None
    bits = cc_decode(soft_bits[:need], first_data_bits)
    blk = bits_to_bytes(bits)
    data, nerr = rs.decode(blk)
    if data is None:
        return None
    first = mls_scramble(data)
    hdr = parse_airlink_header(first[:2])
    payload = bytearray(first[2:])

    consumed = need
    remaining = hdr["length"] - (FIRST_BLOCK_PAYLOAD - 2)
    if remaining > 0:
        # The final block is a PARTIAL block of 1-31 bytes (AirLink 3.4.1),
        # so block sizes must be derived from the header length - assuming a
        # uniform 32 bytes silently breaks every multi-block frame.
        sizes = [FOLLOW_BLOCK_PAYLOAD] * (remaining // FOLLOW_BLOCK_PAYLOAD)
        if remaining % FOLLOW_BLOCK_PAYLOAD:
            sizes.append(remaining % FOLLOW_BLOCK_PAYLOAD)
        sizes = sizes[:max_blocks]
        fbits = sum((s + RS_PARITY) * 8 for s in sizes)
        need2 = (fbits + (CC_K - 1)) * 2
        seg = soft_bits[consumed:consumed + need2]
        if len(seg) >= need2:
            raw = bits_to_bytes(cc_decode(seg, fbits))
            off = 0
            for s in sizes:
                step = s + RS_PARITY
                chunk = raw[off:off + step]
                off += step
                if len(chunk) < step:
                    break
                d, _e = rs.decode(chunk)
                if d is None:
                    break
                payload.extend(mls_scramble(d))
    return {"header": hdr,
            "payload": bytes(payload[:hdr["length"]]),
            "rs_errors_first_block": nerr}


# =====================================================================
# PHY: 4800 bps FSK over NBFM with root-raised-cosine shaping
# =====================================================================

def rrc_taps(sps, beta=RRC_BETA, span=6):
    """Root raised cosine impulse response, T = 1/BAUD (AirLink 2.3)."""
    n = np.arange(-span * sps / 2, span * sps / 2 + 1, dtype=np.float64)
    t = n / sps
    h = np.zeros_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-9:
            h[i] = 1.0 - beta + 4 * beta / np.pi
        elif beta > 0 and abs(abs(ti) - 1.0 / (4 * beta)) < 1e-9:
            h[i] = (beta / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * beta))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta)))
        else:
            num = (np.sin(np.pi * ti * (1 - beta))
                   + 4 * beta * ti * np.cos(np.pi * ti * (1 + beta)))
            den = np.pi * ti * (1 - (4 * beta * ti) ** 2)
            h[i] = num / den
    return h / np.sqrt(np.sum(h ** 2))


def modulate(bits, fs, deviation=2400.0, sps=None, baud=BAUD):
    """Bits -> complex baseband NBFM carrying RRC-shaped NRZ FSK.

    Generates reference signals so the demodulator can be verified against
    the specification before any real ALERT2 traffic exists.

    The waveform is built at an INTEGER samples-per-bit rate and then
    resampled to `fs`. Building directly at fs would quantise the symbol
    period (1.024 MHz / 4800 = 213.33 samples) and drift more than a whole
    bit across a frame, which looks exactly like a broken receiver.
    """
    from scipy import signal as _sig
    from fractions import Fraction
    if sps is None:
        sps = 10
    gen_fs = baud * sps
    nrz = np.repeat(np.array([1.0 if b else -1.0 for b in bits]), sps)
    shaped = np.convolve(nrz, rrc_taps(sps), mode="same")
    phase = 2 * np.pi * deviation * np.cumsum(shaped) / gen_fs
    iq = np.exp(1j * phase)
    if abs(gen_fs - fs) > 1e-6:
        fr = Fraction(int(fs), int(gen_fs)).limit_denominator(10000)
        iq = _sig.resample_poly(iq, fr.numerator, fr.denominator)
    return iq.astype(np.complex64)


def fm_discriminate(iq, fs):
    """FM demodulate complex baseband -> instantaneous frequency (Hz)."""
    d = iq[1:] * np.conj(iq[:-1])
    return np.angle(d) * fs / (2 * np.pi)


def soft_bits(iq, fs, sps=None):
    """IQ -> matched-filtered soft bit stream at `sps` samples per bit."""
    from scipy import signal as _sig
    sps = sps or int(round(fs / BAUD))
    inst = fm_discriminate(iq, fs)
    mf = _sig.lfilter(rrc_taps(sps), 1.0, inst)
    mf = mf - np.mean(mf)
    s = np.std(mf)
    return mf / (s + 1e-12), sps


def _corr_at(stream, sps, phase, pattern_bits):
    """Correlate a bit pattern against the soft stream at a symbol phase.

    `sps` may be fractional (the recovered symbol period), so sample points
    are linearly interpolated rather than used as integer indices.
    """
    pos = phase + np.arange(len(pattern_bits)) * float(sps)
    if pos[-1] >= len(stream) - 1 or pos[0] < 0:
        return -1e9
    k = pos.astype(int)
    frac = pos - k
    got = stream[k] * (1.0 - frac) + stream[k + 1] * frac
    want = np.array([1.0 if b else -1.0 for b in pattern_bits])
    return float(np.dot(want, got) / len(want))


def find_frames(stream, sps, threshold=0.55):
    """Locate ALERT2 frames and recover their true symbol period.

    Correlates the 48-bit bit-sync pattern, then refines samples-per-bit by
    re-correlating the 32-bit CCSDS frame sync at a range of periods. The
    spec allows 4800 bps +/- 3%, which is over 20 bits of drift across a
    frame, so a fixed stride cannot work on real traffic.

    Returns [(payload_start_sample, refined_sps)].
    """
    bs = bits_of_int(BIT_SYNC, BIT_SYNC_LEN)
    fsync = bits_of_int(FRAME_SYNC, FRAME_SYNC_LEN)
    hits = []
    span = (BIT_SYNC_LEN + FRAME_SYNC_LEN) * sps
    limit = len(stream) - span - 4 * sps
    p = 0
    while p < limit:
        if _corr_at(stream, sps, p, bs) > threshold:
            best = (-1e9, p, sps)
            lo = max(0, p - sps)
            hi = min(limit, p + sps)
            for q in range(lo, hi):
                for cand in np.linspace(sps * 0.955, sps * 1.045, 19):
                    c1 = _corr_at(stream, cand, q, bs)
                    if c1 <= threshold:
                        continue
                    fp = q + BIT_SYNC_LEN * cand
                    c2 = _corr_at(stream, cand, fp, fsync)
                    if c2 > threshold and (c1 + c2) > best[0]:
                        best = (c1 + c2, q, cand)
            if best[0] > -1e8:
                _, q, cand = best
                start = q + (BIT_SYNC_LEN + FRAME_SYNC_LEN) * cand
                hits.append((start, cand))
                p = int(q + span)
                continue
        p += 1
    return hits


def _interp(stream, pos):
    k = int(pos)
    if k < 0 or k + 1 >= len(stream):
        return 0.0
    f = pos - k
    return stream[k] * (1.0 - f) + stream[k + 1] * f


def _sample_symbols(stream, start, sps, count, kp=0.01, ki=0.0004):
    """Sample symbols with a TRACKED bit clock (Gardner timing error).

    A single fixed period is not enough: the spec allows 4800 bps +/- 3%,
    and a 3% period error accumulates more than 20 bits of drift across a
    frame. The loop re-estimates the period continuously, exactly as the
    AirLink spec recommends ("bits be parsed ... using a bit clock recovered
    from the bit stream itself").
    """
    out = np.zeros(count, dtype=float)
    period = float(sps)
    p = float(start)
    prev = _interp(stream, p - period)
    integ = 0.0
    for i in range(count):
        if p >= len(stream) - 1:
            return out[:i]
        curr = _interp(stream, p)
        mid = _interp(stream, p - period / 2.0)
        out[i] = curr
        e = mid * (curr - prev)
        e /= (abs(curr) + abs(prev) + 1e-9)
        integ += e
        p += period - (kp * e + ki * integ) * period
        period = min(sps * 1.06, max(sps * 0.94, period))
        prev = curr
    return out


def demodulate(iq, fs, rs=None, threshold=0.55):
    """Full receive chain: IQ -> list of decoded AirLink frames."""
    stream, sps = soft_bits(iq, fs)
    out = []
    # Minimum symbols to decode the first block: (24+16) coded bytes plus
    # the 6-bit CC tail, at rate 1/2.
    need = ((FIRST_BLOCK_PAYLOAD + RS_PARITY) * 8 + (CC_K - 1)) * 2
    for start, rsps in find_frames(stream, sps, threshold):
        avail = int((len(stream) - start) / rsps)
        if avail < need:
            continue
        sym = _sample_symbols(stream, start, rsps, avail)
        frame = demod_frame_from_soft(sym, rs)
        if frame:
            frame["bit_offset"] = int(start)
            frame["sps"] = float(rsps)
            out.append(frame)
    return out


def demod_frame_from_soft(sym, rs=None):
    return decode_frame(list(np.asarray(sym, dtype=float)), rs)
