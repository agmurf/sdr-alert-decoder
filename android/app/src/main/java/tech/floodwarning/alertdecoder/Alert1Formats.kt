package tech.floodwarning.alertdecoder

/**
 * The three 40-bit ALERT frame formats that share the 300-baud AFSK air
 * interface (ALERT2 Application Layer Protocol Specification v1.3,
 * Appendix 2). All are four 10-bit UART bytes - start bit, 8 data bits, stop
 * bit - and differ only in how each byte's 8 data bits are used.
 *
 *   iFLOWS / ALERT BINARY (5.1)   markers 01/01/11/11, A12 in byte2 bit0,
 *                                 no checksum; integrity is the 16 fixed bits
 *   ALERT ASCII (5.2)             four ASCII digits, address/value 0-99
 *   ENHANCED iFLOWS (5.3)         ONLY byte0 is marked (both bits set), A12
 *                                 in byte1 bit6, and a 6-bit CRC in byte3,
 *                                 polynomial x^6 + x^4 + x^3 + 1
 *
 * The 4078 test rig transmits ENHANCED iFLOWS. Decoding it as BINARY puts
 * A12 in the wrong byte and shifts every data bit - battery read 414 rather
 * than 121 (12.1 V) and DI3 read 36 rather than 528 mm.
 *
 * IMPORTANT: only ONE of these may be enabled at a time. Enhanced iFLOWS has
 * just 8 bits of constraint (2 marker bits + the 6-bit CRC) against Binary's
 * 16 fixed bits, so a strong Binary burst yields systematically CRC-valid
 * Enhanced iFLOWS mis-parses that no vote threshold removes.
 */
object Alert1Formats {

    const val BINARY = "BINARY"
    const val ASCII = "ASCII"
    const val ENHANCED_IFLOWS = "ENHANCED_IFLOWS"

    /** UART start/stop positions every ALERT frame has. */
    private val UART = intArrayOf(0, 9, 10, 19, 20, 29, 30, 39)
    private val UART_VAL = intArrayOf(0, 1, 0, 1, 0, 1, 0, 1)

    /** BINARY marker positions and their required values. */
    private val MARK = intArrayOf(7, 8, 17, 18, 27, 28, 37, 38)
    private val MARK_VAL = intArrayOf(1, 0, 1, 0, 1, 1, 1, 1)

    private const val CRC6_POLY = 0b011001    // x^6 + x^4 + x^3 + 1

    data class Parsed(
        val format: String,
        val sensorId: Int,
        val value: Int,
        val crcOk: Boolean?,     // null when the format carries no checksum
        val fixed: Int
    )

    fun uartOk(fr: IntArray): Boolean {
        for (i in UART.indices) if (fr[UART[i]] != UART_VAL[i]) return false
        return true
    }

    private fun markerHits(fr: IntArray): Int {
        var n = 0
        for (i in MARK.indices) if (fr[MARK[i]] == MARK_VAL[i]) n++
        return n
    }

    /** Four 8-bit data bytes; bits are transmitted LSB-first within a byte. */
    fun frameBytes(fr: IntArray): IntArray {
        val w = IntArray(4)
        for (k in 0 until 4) {
            var b = 0
            for (j in 0 until 8) if (fr[10 * k + 1 + j] == 1) b = b or (1 shl j)
            w[k] = b
        }
        return w
    }

    /**
     * CRC over bytes 0-2 plus D9/D10, LSB-first, reflected output. The spec
     * names only the polynomial; this convention was established empirically
     * against five real 4078 frames and validates all of them.
     */
    fun crc6Enhanced(b0: Int, b1: Int, b2: Int, b3: Int): Int {
        var reg = 0
        val bits = ArrayList<Int>(26)
        for (b in intArrayOf(b0, b1, b2)) for (i in 0 until 8) bits.add((b shr i) and 1)
        bits.add(b3 and 1)              // D9
        bits.add((b3 shr 1) and 1)      // D10
        for (bit in bits) {
            val fb = ((reg shr 5) and 1) xor bit
            reg = (reg shl 1) and 0x3F
            if (fb == 1) reg = reg xor CRC6_POLY
        }
        var refl = 0                     // reflect the 6-bit register
        for (i in 0 until 6) if ((reg shr i) and 1 == 1) refl = refl or (1 shl (5 - i))
        return refl
    }

    fun parseBinary(fr: IntArray): Parsed? {
        for (i in MARK.indices) if (fr[MARK[i]] != MARK_VAL[i]) return null
        val w = frameBytes(fr)
        val sid = (w[0] and 63) + 64 * (w[1] and 63) + 4096 * (w[2] and 1)
        val v = (w[3] and 63) * 32 + ((w[2] and 62) shr 1)
        return Parsed(BINARY, sid, v, null, 16)
    }

    fun parseAscii(fr: IntArray): Parsed? {
        val w = frameBytes(fr)
        val d = IntArray(4)
        for (i in 0 until 4) {
            if ((w[i] and 0x70) != 0x30) return null
            val dig = w[i] and 0x0F
            if (dig > 9) return null
            d[i] = dig
        }
        return Parsed(ASCII, d[0] + 10 * d[1], d[2] + 10 * d[3], null, 16)
    }

    fun parseEnhancedIflows(fr: IntArray): Parsed? {
        val w = frameBytes(fr)
        val b0 = w[0]; val b1 = w[1]; val b2 = w[2]; val b3 = w[3]
        if ((b0 shr 6) != 0b11) return null
        val sid = (b0 and 63) or ((b1 and 63) shl 6) or (((b1 shr 6) and 1) shl 12)
        var v = (b1 shr 7) and 1                       // D0
        for (k in 0 until 8) v = v or (((b2 shr k) and 1) shl (k + 1))   // D1..D8
        v = v or ((b3 and 1) shl 9)                    // D9
        v = v or (((b3 shr 1) and 1) shl 10)           // D10
        val crc = (((b3 shr 7) and 1) shl 5) or (((b3 shr 6) and 1) shl 4) or
                (((b3 shr 5) and 1) shl 3) or (((b3 shr 4) and 1) shl 2) or
                (((b3 shr 3) and 1) shl 1) or ((b3 shr 2) and 1)
        val ok = crc6Enhanced(b0, b1, b2, b3) == crc
        return Parsed(ENHANCED_IFLOWS, sid, v, ok, if (ok) 16 else 8)
    }

    /** Parse a 40-bit frame under the single enabled format. */
    fun parse(fr: IntArray, format: String): Parsed? {
        if (!uartOk(fr)) return null
        val r = when (format) {
            BINARY -> parseBinary(fr)
            ASCII -> parseAscii(fr)
            ENHANCED_IFLOWS -> parseEnhancedIflows(fr)
            else -> null
        } ?: return null
        if (r.value !in 0..2047 || r.sensorId !in 0..8191) return null
        if (format == BINARY && markerHits(fr) < 8) return null
        return r
    }
}
