package tech.floodwarning.alertdecoder

/**
 * ALERT2 MANT and Application layer parsing (port of src/alert2_app.py).
 *
 *   MANT v1.2 section 2.2  - 48-bit fixed header then optional fields
 *   Application v1.3       - control byte, optional 16-bit timestamp, then
 *                            (type, length, body) reports
 *
 * Value fields carry a Format/Length byte: high nibble is the numeric format
 * (1 unsigned, 2 signed two's complement, 3 float, 4 UTF-8), low nibble is
 * the byte length. 0x32 is the ALERT2 "FP2" 16-bit float, 0x34 IEEE binary32,
 * 0x38 binary64.
 */
object Alert2App {

    const val RPT_GSR = 1
    const val RPT_TBRG = 2
    const val RPT_TSD = 5
    const val RPT_SET = 251
    const val RPT_GET = 252

    fun reportName(t: Int) = when (t) {
        RPT_GSR -> "General Sensor"
        RPT_TBRG -> "Tipping Bucket Rain"
        RPT_TSD -> "Time Series"
        RPT_SET -> "SET"
        RPT_GET -> "GET"
        else -> "Type $t"
    }

    data class MantHeader(
        val version: Int, val protocolId: Int, val port: Int,
        val hopLimit: Int, val payloadLength: Int, val sourceAddress: Int,
        val destinationAddress: Int?, val encrypted: Boolean
    )

    data class Reading(
        val sensorId: Int, val value: Double?, val text: String?,
        val formatLength: Int, val timeOffsets: List<Int> = emptyList()
    )

    data class Report(
        val type: Int, val typeName: String, val timestamp: Int?,
        val testFlag: Boolean, val readings: List<Reading>
    )

    private class BitReader(val d: ByteArray) {
        var pos = 0
        fun read(n: Int): Int {
            var v = 0
            repeat(n) {
                val byte = pos shr 3
                if (byte >= d.size) throw IndexOutOfBoundsException()
                v = (v shl 1) or ((d[byte].toInt() shr (7 - (pos and 7))) and 1)
                pos++
            }
            return v
        }
    }

    /** Returns Triple(header, applicationPdu, bytesConsumed) or null. */
    fun parseMant(payload: ByteArray): Triple<MantHeader, ByteArray, Int>? {
        if (payload.size < 6) return null
        return try {
            val b = BitReader(payload)
            val version = b.read(2)
            val protocolId = b.read(3)
            b.read(1)                       // timestamp service request
            val addPath = b.read(1)
            val daIncluded = b.read(1)
            val port = b.read(4)
            val encrypted = b.read(1)
            b.read(2)                       // reserved
            b.read(1)                       // ack
            b.read(1)                       // added header
            val hop = b.read(3)
            val plen = b.read(12)
            val sa = b.read(16)
            var off = 6
            var da: Int? = null
            if (daIncluded == 1 && payload.size >= off + 2) {
                da = ((payload[off].toInt() and 0xFF) shl 8) or (payload[off + 1].toInt() and 0xFF)
                off += 2
            }
            if (protocolId == 1 && payload.size > off) off += 1        // PDU id
            if (addPath == 1 && payload.size > off) {
                val n = payload[off].toInt() and 0xFF
                off += 1 + 2 * n
            }
            val end = minOf(payload.size, off + plen)
            if (off > payload.size) return null
            Triple(
                MantHeader(version, protocolId, port, hop, plen, sa, da, encrypted == 1),
                payload.copyOfRange(off, maxOf(off, end)), end
            )
        } catch (e: Exception) {
            null
        }
    }

    fun decodeValue(fl: Int, raw: ByteArray): Pair<Double?, String?> {
        val fmt = (fl shr 4) and 0xF
        val n = fl and 0xF
        if (n == 0 || raw.size < n) return Pair(null, null)
        var u = 0L
        for (i in 0 until n) u = (u shl 8) or (raw[i].toLong() and 0xFF)
        return when (fmt) {
            1 -> Pair(u.toDouble(), null)
            2 -> {
                val signBit = 1L shl (n * 8 - 1)
                val s = if (u and signBit != 0L) u - (1L shl (n * 8)) else u
                Pair(s.toDouble(), null)
            }
            3 -> when (n) {
                4 -> Pair(java.lang.Float.intBitsToFloat(u.toInt()).toDouble(), null)
                8 -> Pair(java.lang.Double.longBitsToDouble(u), null)
                2 -> Pair(fp2(u.toInt()), null)
                else -> Pair(null, null)
            }
            4 -> Pair(null, String(raw, 0, n, Charsets.UTF_8))
            else -> Pair(u.toDouble(), null)
        }
    }

    /** ALERT2 "FP2" 16-bit float - IEEE half-precision layout. */
    private fun fp2(w: Int): Double {
        val sign = if (w and 0x8000 != 0) -1.0 else 1.0
        val exp = (w shr 10) and 0x1F
        val man = w and 0x3FF
        return when (exp) {
            0 -> sign * (man / 1024.0) * Math.pow(2.0, -14.0)
            0x1F -> if (man != 0) Double.NaN else sign * Double.POSITIVE_INFINITY
            else -> sign * (1.0 + man / 1024.0) * Math.pow(2.0, (exp - 15).toDouble())
        }
    }

    fun parseApplication(pdu: ByteArray): List<Report> {
        if (pdu.isEmpty()) return emptyList()
        var i = 0
        val ctrl0 = pdu[0].toInt() and 0xFF
        var ctrl = ctrl0
        i++
        // control byte bits are numbered from the LSB
        val testFlag = ((ctrl0 shr 3) and 1) == 1
        while (((ctrl shr 7) and 1) == 1 && i < pdu.size) { ctrl = pdu[i].toInt() and 0xFF; i++ }
        var timestamp: Int? = null
        if (((ctrl0 shr 2) and 1) == 1) {
            if (i + 1 >= pdu.size) return emptyList()
            timestamp = ((pdu[i].toInt() and 0xFF) shl 8) or (pdu[i + 1].toInt() and 0xFF)
            i += 2
        }
        val out = ArrayList<Report>()
        while (i + 1 < pdu.size) {
            val rtype = pdu[i].toInt() and 0xFF
            val rlen = pdu[i + 1].toInt() and 0xFF
            val bodyEnd = minOf(pdu.size, i + 2 + rlen)
            val body = pdu.copyOfRange(minOf(i + 2, pdu.size), bodyEnd)
            i += 2 + rlen
            val readings = ArrayList<Reading>()
            when (rtype) {
                RPT_GSR -> {
                    var j = 0
                    while (j + 1 < body.size) {
                        val sid = body[j].toInt() and 0xFF
                        val fl = body[j + 1].toInt() and 0xFF
                        val n = fl and 0xF
                        if (j + 2 + n > body.size) break
                        val (v, s) = decodeValue(fl, body.copyOfRange(j + 2, j + 2 + n))
                        readings.add(Reading(sid, v, s, fl))
                        j += 2 + n
                    }
                }
                RPT_TBRG -> {
                    if (body.size >= 2) {
                        val sid = body[0].toInt() and 0xFF
                        val fl = body[1].toInt() and 0xFF
                        val n = fl and 0xF
                        if (2 + n <= body.size) {
                            val (v, s) = decodeValue(fl, body.copyOfRange(2, 2 + n))
                            val offs = (2 + n until body.size).map { body[it].toInt() and 0xFF }
                            readings.add(Reading(sid, v, s, fl, offs))
                        }
                    }
                }
            }
            out.add(Report(rtype, reportName(rtype), timestamp, testFlag, readings))
            if (rlen == 0) break
        }
        return out
    }

    fun parseAirLinkPayload(payload: ByteArray): List<Pair<MantHeader, List<Report>>> {
        val out = ArrayList<Pair<MantHeader, List<Report>>>()
        var off = 0
        var guard = 0
        while (off < payload.size && guard < 16) {
            guard++
            val r = parseMant(payload.copyOfRange(off, payload.size)) ?: break
            val (hdr, app, consumed) = r
            if (consumed <= 0) break
            out.add(Pair(hdr, parseApplication(app)))
            off += consumed
        }
        return out
    }
}
