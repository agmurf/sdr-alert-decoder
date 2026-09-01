"""
ALERT2 MANT and Application layer parsing.

Turns an AirLink payload (from alert2.decode_frame) into sensor readings.

  ALERT2 MANT Layer Protocol Specification v1.2, section 2.2
  ALERT2 Application Layer Protocol Specification v1.3, sections 2.1-2.4

MANT header, 48 bits fixed then optional fields:
    2  version            3  protocol id        1  timestamp svc request
    1  add-path request   1  DA included        4  port
    1  encrypted          2  reserved           1  ACK
    1  added header       3  hop limit         12  payload length
   16  source address    [16 destination address if DA-included]
   [8  MANT PDU id if protocol id == 1]
   [8  count + 2*n added source addresses if add-path set]

Application PDU:
    control byte, then optional 16-bit timestamp, then one or more reports of
    (type, length, body). Control byte bits are numbered from the LSB:
      0-1 version   2 timestamp present   3 test flag
      4-6 APDU id   7 extensibility (a second control byte follows)

Value fields carry a Format/Length byte: high nibble is the numeric format
(1 unsigned, 2 signed two's complement, 3 floating point, 4 UTF-8), low
nibble is the byte length. 0x32 is the ALERT2-specific "FP2" 16-bit float,
0x34 is IEEE-754 binary32, 0x38 is binary64.
"""
import struct

# Report type codes seen in the specification's examples.
RPT_GSR = 1          # General Sensor Report
RPT_TBRG = 2         # Tipping Bucket Rain Gage
RPT_TSD = 5          # Time Series Data
RPT_SET = 251
RPT_GET = 252

REPORT_NAMES = {RPT_GSR: "General Sensor", RPT_TBRG: "Tipping Bucket Rain",
                RPT_TSD: "Time Series", RPT_SET: "SET", RPT_GET: "GET"}


class Bits:
    """MSB-first bit reader over a byte string."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, n):
        v = 0
        for _ in range(n):
            byte = self.pos >> 3
            if byte >= len(self.data):
                raise ValueError("out of data")
            bit = (self.data[byte] >> (7 - (self.pos & 7))) & 1
            v = (v << 1) | bit
            self.pos += 1
        return v

    @property
    def byte_pos(self):
        return (self.pos + 7) >> 3


def parse_mant(payload):
    """Parse one MANT PDU. Returns (header_dict, app_pdu_bytes, consumed)."""
    if len(payload) < 6:
        return None, b"", 0
    b = Bits(payload)
    h = {
        "version": b.read(2),
        "protocol_id": b.read(3),
        "ts_service_request": b.read(1),
        "add_path_request": b.read(1),
        "da_included": b.read(1),
        "port": b.read(4),
        "encrypted": b.read(1),
        "reserved": b.read(2),
        "ack": b.read(1),
        "added_header": b.read(1),
        "hop_limit": b.read(3),
        "payload_length": b.read(12),
        "source_address": b.read(16),
    }
    off = 6
    if h["da_included"]:
        if len(payload) < off + 2:
            return h, b"", len(payload)
        h["destination_address"] = (payload[off] << 8) | payload[off + 1]
        off += 2
    if h["protocol_id"] == 1:                    # reliable datagram service
        if len(payload) > off:
            h["pdu_id"] = payload[off]
            off += 1
    if h["add_path_request"]:
        if len(payload) > off:
            n = payload[off]
            off += 1
            h["added_source_addresses"] = [
                (payload[off + 2 * i] << 8) | payload[off + 2 * i + 1]
                for i in range(n) if off + 2 * i + 1 < len(payload)]
            off += 2 * n
    end = min(len(payload), off + h["payload_length"])
    return h, payload[off:end], end


def decode_value(fmt_len, raw):
    """Decode a value field given its Format/Length byte."""
    fmt = (fmt_len >> 4) & 0xF
    n = fmt_len & 0xF
    if len(raw) < n or n == 0:
        return None
    v = raw[:n]
    if fmt == 1:
        return int.from_bytes(v, "big", signed=False)
    if fmt == 2:
        return int.from_bytes(v, "big", signed=True)
    if fmt == 3:
        if n == 4:
            return struct.unpack(">f", v)[0]
        if n == 8:
            return struct.unpack(">d", v)[0]
        if n == 2:
            return _fp2(int.from_bytes(v, "big"))
        return None
    if fmt == 4:
        try:
            return v.decode("utf-8", errors="replace")
        except Exception:
            return None
    return int.from_bytes(v, "big", signed=False)


def _fp2(word):
    """ALERT2 'FP2' 16-bit float (Application spec, Appendix 3): sign,
    5-bit exponent, 10-bit mantissa - the IEEE half-precision layout."""
    sign = -1.0 if word & 0x8000 else 1.0
    exp = (word >> 10) & 0x1F
    man = word & 0x3FF
    if exp == 0:
        return sign * (man / 1024.0) * (2.0 ** -14)
    if exp == 0x1F:
        return float("nan") if man else sign * float("inf")
    return sign * (1.0 + man / 1024.0) * (2.0 ** (exp - 15))


def parse_application(pdu):
    """Parse an Application PDU into a list of report dicts."""
    if not pdu:
        return []
    i = 0
    ctrl = pdu[i]
    i += 1
    out_common = {
        "version": ctrl & 0x3,
        "test_flag": bool((ctrl >> 3) & 1),
        "apdu_id": (ctrl >> 4) & 0x7,
    }
    while (ctrl >> 7) & 1 and i < len(pdu):      # extensibility chain
        ctrl = pdu[i]
        i += 1
    timestamp = None
    if (pdu[0] >> 2) & 1:
        if i + 1 >= len(pdu):
            return []
        timestamp = (pdu[i] << 8) | pdu[i + 1]
        i += 2

    reports = []
    while i + 1 < len(pdu):
        rtype = pdu[i]
        rlen = pdu[i + 1]
        body = pdu[i + 2:i + 2 + rlen]
        i += 2 + rlen
        rep = dict(out_common)
        rep.update({"type": rtype,
                    "type_name": REPORT_NAMES.get(rtype, f"Type {rtype}"),
                    "timestamp": timestamp,
                    "readings": []})
        if rtype == RPT_GSR:
            j = 0
            while j + 1 < len(body):
                sid = body[j]
                fl = body[j + 1]
                n = fl & 0xF
                val = decode_value(fl, body[j + 2:j + 2 + n])
                rep["readings"].append({"sensor_id": sid, "value": val,
                                        "format_length": fl})
                j += 2 + n
        elif rtype == RPT_TBRG:
            if len(body) >= 2:
                sid = body[0]
                fl = body[1]
                n = fl & 0xF
                accum = decode_value(fl, body[2:2 + n])
                rep["readings"].append({"sensor_id": sid, "value": accum,
                                        "format_length": fl,
                                        "time_offsets": list(body[2 + n:])})
        else:
            rep["raw"] = bytes(body)
        reports.append(rep)
        if rlen == 0:
            break
    return reports


def parse_airlink_payload(payload):
    """AirLink payload -> [(mant_header, [reports])] for each MANT PDU."""
    out = []
    off = 0
    guard = 0
    while off < len(payload) and guard < 16:
        guard += 1
        hdr, app, consumed = parse_mant(payload[off:])
        if not hdr or consumed <= 0:
            break
        out.append((hdr, parse_application(app)))
        off += consumed
    return out
