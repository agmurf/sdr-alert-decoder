package tech.floodwarning.alertdecoder

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * ALERT2 AirLink decoder - a port of the verified desktop implementation
 * (src/alert2.py + src/alert2_app.py), built from:
 *   ALERT2 AirLink Layer Specification v1.1 (NHWC ALERT2 TWG, March 2012)
 *   ALERT2 MANT Layer Protocol Specification v1.2
 *   ALERT2 Application Layer Protocol Specification v1.3
 *
 * ALERT2 shares nothing with iFLOWS at the radio layer: 4800 bps instead of
 * 300 baud, bit-synchronous instead of 10-bit UART bytes, root-raised-cosine
 * shaping, and concatenated Reed-Solomon + convolutional forward error
 * correction instead of 16 fixed marker bits.
 */
object Alert2 {

    const val BAUD = 4800
    const val RRC_BETA = 0.96
    const val CHANNEL_RATE = 48_000          // 10 samples per bit
    const val SPS = CHANNEL_RATE / BAUD

    val BIT_SYNC = longArrayOf(0xEB90B433AAAAL)[0]   // 48 bits
    const val BIT_SYNC_LEN = 48
    const val FRAME_SYNC = 0x352EF853L               // 32 bits, CCSDS ASM
    const val FRAME_SYNC_LEN = 32

    const val FIRST_BLOCK_PAYLOAD = 24       // includes the 2-byte AirLink header
    const val FOLLOW_BLOCK_PAYLOAD = 32
    const val RS_PARITY = 16

    // ------------------------------------------------------------ bit helpers

    fun bitsOfLong(v: Long, n: Int): IntArray =
        IntArray(n) { ((v shr (n - 1 - it)) and 1L).toInt() }

    fun bytesToBits(data: ByteArray): IntArray {
        val out = IntArray(data.size * 8)
        var k = 0
        for (b in data) {
            val v = b.toInt() and 0xFF
            for (i in 7 downTo 0) out[k++] = (v shr i) and 1
        }
        return out
    }

    fun bitsToBytes(bits: IntArray): ByteArray {
        val out = ByteArray(bits.size / 8)
        for (i in out.indices) {
            var v = 0
            for (j in 0 until 8) v = (v shl 1) or (bits[i * 8 + j] and 1)
            out[i] = v.toByte()
        }
        return out
    }

    // --------------------------------------------------- MLS scrambler (3.3)

    /** 17-bit MLS, X^17 + X^3 + 1, preload 0x01. Self-inverse. */
    fun mlsScramble(data: ByteArray, preload: Int = 0x01): ByteArray {
        var reg = preload and 0x1FFFF
        val out = ByteArray(data.size)
        for (i in data.indices) {
            var v = 0
            val b = data[i].toInt() and 0xFF
            for (j in 7 downTo 0) {
                val fb = ((reg shr 16) xor (reg shr 2)) and 1
                reg = ((reg shl 1) or fb) and 0x1FFFF
                v = (v shl 1) or (((b shr j) and 1) xor fb)
            }
            out[i] = v.toByte()
        }
        return out
    }

    // ---------------------------------------------- Reed-Solomon over GF(256)

    class GF256(prim: Int = 0x11D) {
        val exp = IntArray(512)
        val log = IntArray(256)

        init {
            var x = 1
            for (i in 0 until 255) {
                exp[i] = x; log[x] = i
                x = x shl 1
                if (x and 0x100 != 0) x = x xor prim
            }
            for (i in 255 until 512) exp[i] = exp[i - 255]
        }

        fun mul(a: Int, b: Int) = if (a == 0 || b == 0) 0 else exp[log[a] + log[b]]
        fun div(a: Int, b: Int) = if (a == 0) 0 else exp[((log[a] - log[b]) % 255 + 255) % 255]
        fun inv(a: Int) = exp[(255 - log[a]) % 255]
        fun pow(a: Int, n: Int) = if (a == 0) 0 else exp[((log[a] * n) % 255 + 255) % 255]
    }

    class ReedSolomon(
        val nparity: Int = RS_PARITY,
        prim: Int = 0x11D,
        val fcr: Int = 0,
        val generator: Int = 2
    ) {
        val gf = GF256(prim)
        private val gen: IntArray = makeGenerator()

        /** Descending degree order with a leading 1. */
        private fun makeGenerator(): IntArray {
            var g = intArrayOf(1)
            for (i in 0 until nparity) {
                val root = gf.pow(generator, i + fcr)
                val ng = IntArray(g.size + 1)
                for (j in g.indices) {
                    ng[j] = ng[j] xor g[j]
                    ng[j + 1] = ng[j + 1] xor gf.mul(g[j], root)
                }
                g = ng
            }
            return g
        }

        fun encode(data: ByteArray): ByteArray {
            val work = IntArray(data.size + nparity)
            for (i in data.indices) work[i] = data[i].toInt() and 0xFF
            for (i in data.indices) {
                val coef = work[i]
                if (coef == 0) continue
                for (j in 1 until gen.size) work[i + j] = work[i + j] xor gf.mul(gen[j], coef)
            }
            val out = ByteArray(data.size + nparity)
            for (i in out.indices) out[i] = work[i].toByte()
            System.arraycopy(data, 0, out, 0, data.size)
            return out
        }

        private fun syndromes(msg: IntArray): IntArray {
            val s = IntArray(nparity)
            for (i in 0 until nparity) {
                val x = gf.pow(generator, i + fcr)
                var acc = 0
                for (c in msg) acc = gf.mul(acc, x) xor c
                s[i] = acc
            }
            return s
        }

        private fun polyScale(p: IntArray, x: Int) = IntArray(p.size) { gf.mul(p[it], x) }

        private fun polyAddRightAligned(a: IntArray, b: IntArray): IntArray {
            val n = maxOf(a.size, b.size)
            val out = IntArray(n)
            for (i in a.indices) out[i + n - a.size] = out[i + n - a.size] xor a[i]
            for (i in b.indices) out[i + n - b.size] = out[i + n - b.size] xor b[i]
            return out
        }

        /** Returns the data bytes, or null if unrecoverable. */
        fun decode(codeword: ByteArray): ByteArray? {
            val msg = IntArray(codeword.size) { codeword[it].toInt() and 0xFF }
            val n = msg.size
            val synd = syndromes(msg)
            if (synd.all { it == 0 }) return ByteArray(n - nparity) { msg[it].toByte() }

            // Berlekamp-Massey. The scale-and-add must run on EVERY nonzero
            // discrepancy, not only when the degree grew.
            var errLoc = intArrayOf(1)
            var oldLoc = intArrayOf(1)
            for (i in 0 until nparity) {
                var delta = synd[i]
                for (j in 1 until errLoc.size)
                    delta = delta xor gf.mul(errLoc[errLoc.size - 1 - j], synd[i - j])
                oldLoc = oldLoc + 0
                if (delta != 0) {
                    if (oldLoc.size > errLoc.size) {
                        val newLoc = polyScale(oldLoc, delta)
                        oldLoc = polyScale(errLoc, gf.inv(delta))
                        errLoc = newLoc
                    }
                    errLoc = polyAddRightAligned(errLoc, polyScale(oldLoc, delta))
                }
            }
            var lead = 0
            while (lead < errLoc.size - 1 && errLoc[lead] == 0) lead++
            errLoc = errLoc.copyOfRange(lead, errLoc.size)
            val nerr = errLoc.size - 1
            if (nerr <= 0 || nerr * 2 > nparity) return null

            // ascending coefficients
            val sig = IntArray(errLoc.size) { errLoc[errLoc.size - 1 - it] }

            // Chien over real positions: byte p has locator alpha^(n-1-p) and
            // sigma vanishes at its INVERSE (searching alpha^i for i<n misses it).
            val positions = ArrayList<Int>()
            for (p in 0 until n) {
                val xInv = gf.inv(gf.pow(generator, (n - 1 - p) % 255))
                var acc = 0
                for (k in sig.indices) acc = acc xor gf.mul(sig[k], gf.pow(xInv, k))
                if (acc == 0) positions.add(p)
            }
            if (positions.size != nerr) return null

            // Forney
            val omega = IntArray(nparity)
            for (k in 0 until nparity) {
                var acc = 0
                for (j in sig.indices) if (k - j in 0 until nparity)
                    acc = acc xor gf.mul(sig[j], synd[k - j])
                omega[k] = acc
            }
            for (p in positions) {
                val x = gf.pow(generator, (n - 1 - p) % 255)
                val xInv = gf.inv(x)
                var num = 0
                for (k in omega.indices) num = num xor gf.mul(omega[k], gf.pow(xInv, k))
                var den = 0
                var k = 1
                while (k < sig.size) { den = den xor gf.mul(sig[k], gf.pow(xInv, k - 1)); k += 2 }
                if (den == 0) return null
                var corr = gf.div(num, den)
                corr = gf.mul(corr, gf.pow(x, 1 - fcr))
                msg[p] = msg[p] xor corr
            }
            if (syndromes(msg).any { it != 0 }) return null
            return ByteArray(n - nparity) { msg[it].toByte() }
        }
    }

    // ------------------------------- convolutional code, rate 1/2, k=7

    private const val CC_POLY_A = 0x6D
    private const val CC_POLY_B = 0x4F
    private const val CC_K = 7
    private const val CC_STATES = 1 shl (CC_K - 1)

    private fun parity(x: Int): Int {
        var v = x
        v = v xor (v shr 4); v = v xor (v shr 2); v = v xor (v shr 1)
        return v and 1
    }

    private val ccNext = Array(CC_STATES) { st -> IntArray(2) { b -> ((st shl 1) or b) and (CC_STATES - 1) } }
    private val ccOutA = Array(CC_STATES) { st -> IntArray(2) { b -> parity((((st shl 1) or b) and 0x7F) and CC_POLY_A) } }
    private val ccOutB = Array(CC_STATES) { st -> IntArray(2) { b -> parity((((st shl 1) or b) and 0x7F) and CC_POLY_B) } }

    fun ccEncode(bits: IntArray, tail: Boolean = true): IntArray {
        var reg = 0
        val n = bits.size + if (tail) CC_K - 1 else 0
        val out = IntArray(n * 2)
        for (i in 0 until n) {
            val b = if (i < bits.size) bits[i] and 1 else 0
            reg = ((reg shl 1) or b) and 0x7F
            out[2 * i] = parity(reg and CC_POLY_A)
            out[2 * i + 1] = parity(reg and CC_POLY_B)
        }
        return out
    }

    /** Soft-input Viterbi; positive soft value means the bit is likely 1. */
    fun ccDecode(soft: DoubleArray, nbits: Int): IntArray {
        val total = soft.size / 2
        if (total == 0) return IntArray(0)
        val INF = Double.MAX_VALUE / 4
        var metric = DoubleArray(CC_STATES) { if (it == 0) 0.0 else INF }
        val back = Array(total) { ByteArray(CC_STATES) }
        for (t in 0 until total) {
            val a = soft[2 * t]
            val b = soft[2 * t + 1]
            val next = DoubleArray(CC_STATES) { INF }
            for (st in 0 until CC_STATES) {
                val m = metric[st]
                if (m >= INF) continue
                for (bit in 0..1) {
                    val ns = ccNext[st][bit]
                    val ea = ccOutA[st][bit]
                    val eb = ccOutB[st][bit]
                    val cost = m - (if (ea == 1) a else -a) - (if (eb == 1) b else -b)
                    if (cost < next[ns]) { next[ns] = cost; back[t][ns] = st.toByte() }
                }
            }
            metric = next
        }
        var st = if (metric[0] < INF) 0 else metric.indices.minByOrNull { metric[it] }!!
        val bits = IntArray(total)
        for (t in total - 1 downTo 0) {
            bits[t] = st and 1
            st = back[t][st].toInt() and 0xFF
        }
        return bits.copyOf(minOf(nbits, total))
    }

    // ------------------------------------------------------ framing helpers

    data class AirLinkHeader(val version: Int, val reserved: Int, val length: Int)

    fun parseAirLinkHeader(b0: Int, b1: Int): AirLinkHeader {
        val v = ((b0 and 0xFF) shl 8) or (b1 and 0xFF)
        return AirLinkHeader((v shr 14) and 3, (v shr 10) and 0xF, v and 0x3FF)
    }

    fun airLinkHeader(len: Int, version: Int = 0): ByteArray {
        val v = ((version and 3) shl 14) or (len and 0x3FF)
        return byteArrayOf(((v shr 8) and 0xFF).toByte(), (v and 0xFF).toByte())
    }

    fun preambleBits(): IntArray =
        bitsOfLong(BIT_SYNC, BIT_SYNC_LEN) + bitsOfLong(FRAME_SYNC, FRAME_SYNC_LEN)

    fun encodeFrame(payload: ByteArray, rs: ReedSolomon = ReedSolomon()): IntArray {
        val body = airLinkHeader(payload.size) + payload
        val first = ByteArray(FIRST_BLOCK_PAYLOAD)
        System.arraycopy(body, 0, first, 0, minOf(FIRST_BLOCK_PAYLOAD, body.size))
        var out = ccEncode(bytesToBits(rs.encode(mlsScramble(first))))
        if (body.size > FIRST_BLOCK_PAYLOAD) {
            val rest = body.copyOfRange(FIRST_BLOCK_PAYLOAD, body.size)
            var follow = IntArray(0)
            var i = 0
            while (i < rest.size) {
                val blk = rest.copyOfRange(i, minOf(i + FOLLOW_BLOCK_PAYLOAD, rest.size))
                follow += bytesToBits(rs.encode(mlsScramble(blk)))
                i += FOLLOW_BLOCK_PAYLOAD
            }
            out += ccEncode(follow)
        }
        return out
    }

    class Frame(val header: AirLinkHeader, val payload: ByteArray, val sps: Double)

    fun decodeFrame(soft: DoubleArray, rs: ReedSolomon = ReedSolomon(), sps: Double = SPS.toDouble()): Frame? {
        val firstDataBits = (FIRST_BLOCK_PAYLOAD + RS_PARITY) * 8
        val need = (firstDataBits + CC_K - 1) * 2
        if (soft.size < need) return null
        val blk = bitsToBytes(ccDecode(soft.copyOfRange(0, need), firstDataBits))
        val data = rs.decode(blk) ?: return null
        val first = mlsScramble(data)
        val hdr = parseAirLinkHeader(first[0].toInt(), first[1].toInt())
        val payload = ArrayList<Byte>()
        for (i in 2 until first.size) payload.add(first[i])

        val remaining = hdr.length - (FIRST_BLOCK_PAYLOAD - 2)
        if (remaining > 0) {
            // The final block is a PARTIAL block of 1-31 bytes; assuming a
            // uniform 32 breaks every multi-block frame.
            val sizes = ArrayList<Int>()
            repeat(remaining / FOLLOW_BLOCK_PAYLOAD) { sizes.add(FOLLOW_BLOCK_PAYLOAD) }
            if (remaining % FOLLOW_BLOCK_PAYLOAD != 0) sizes.add(remaining % FOLLOW_BLOCK_PAYLOAD)
            val fbits = sizes.sumOf { (it + RS_PARITY) * 8 }
            val need2 = (fbits + CC_K - 1) * 2
            if (soft.size >= need + need2) {
                val raw = bitsToBytes(ccDecode(soft.copyOfRange(need, need + need2), fbits))
                var off = 0
                for (s in sizes) {
                    val step = s + RS_PARITY
                    if (off + step > raw.size) break
                    val d = rs.decode(raw.copyOfRange(off, off + step)) ?: break
                    off += step
                    for (b in mlsScramble(d)) payload.add(b)
                }
            }
        }
        val n = minOf(hdr.length, payload.size)
        return Frame(hdr, ByteArray(n) { payload[it] }, sps)
    }

    // ============================================================= PHY

    fun rrcTaps(sps: Int, beta: Double = RRC_BETA, span: Int = 6): DoubleArray {
        val n = span * sps
        val h = DoubleArray(n + 1)
        for (i in 0..n) {
            val t = (i - n / 2.0) / sps
            h[i] = when {
                abs(t) < 1e-9 -> 1.0 - beta + 4 * beta / PI
                beta > 0 && abs(abs(t) - 1.0 / (4 * beta)) < 1e-9 ->
                    (beta / sqrt(2.0)) * ((1 + 2 / PI) * sin(PI / (4 * beta)) +
                            (1 - 2 / PI) * cos(PI / (4 * beta)))
                else -> {
                    val num = sin(PI * t * (1 - beta)) + 4 * beta * t * cos(PI * t * (1 + beta))
                    val den = PI * t * (1 - (4 * beta * t) * (4 * beta * t))
                    num / den
                }
            }
        }
        var e = 0.0
        for (v in h) e += v * v
        val s = sqrt(e)
        for (i in h.indices) h[i] /= s
        return h
    }

    /** FM discriminate then matched-filter -> normalised soft bit stream. */
    fun softStream(ir: FloatArray, ii: FloatArray): DoubleArray {
        val n = ir.size - 1
        if (n < 16) return DoubleArray(0)
        val inst = DoubleArray(n)
        for (i in 0 until n) {
            val pr = ir[i + 1] * ir[i] + ii[i + 1] * ii[i]
            val pi = ii[i + 1] * ir[i] - ir[i + 1] * ii[i]
            inst[i] = Math.atan2(pi.toDouble(), pr.toDouble())
        }
        val taps = rrcTaps(SPS)
        val out = DoubleArray(n)
        for (i in inst.indices) {
            var acc = 0.0
            val kMax = minOf(taps.size - 1, i)
            for (k in 0..kMax) acc += taps[k] * inst[i - k]
            out[i] = acc
        }
        var mean = 0.0
        for (v in out) mean += v
        mean /= out.size
        var sd = 0.0
        for (v in out) { val d = v - mean; sd += d * d }
        sd = sqrt(sd / out.size) + 1e-12
        for (i in out.indices) out[i] = (out[i] - mean) / sd
        return out
    }

    private fun interp(s: DoubleArray, pos: Double): Double {
        val k = pos.toInt()
        if (k < 0 || k + 1 >= s.size) return 0.0
        val f = pos - k
        return s[k] * (1 - f) + s[k + 1] * f
    }

    private fun corrAt(s: DoubleArray, sps: Double, phase: Double, pat: IntArray): Double {
        val last = phase + (pat.size - 1) * sps
        if (last >= s.size - 1 || phase < 0) return -1e9
        var acc = 0.0
        for (i in pat.indices) {
            val want = if (pat[i] == 1) 1.0 else -1.0
            acc += want * interp(s, phase + i * sps)
        }
        return acc / pat.size
    }

    /** Symbol sampling with a tracked bit clock (spec allows 4800 bps +/-3%). */
    private fun sampleSymbols(s: DoubleArray, start: Double, sps: Double, count: Int): DoubleArray {
        val out = DoubleArray(count)
        var period = sps
        var p = start
        var prev = interp(s, p - period)
        var integ = 0.0
        for (i in 0 until count) {
            if (p >= s.size - 1) return out.copyOf(i)
            val curr = interp(s, p)
            val mid = interp(s, p - period / 2.0)
            out[i] = curr
            var e = mid * (curr - prev)
            e /= (abs(curr) + abs(prev) + 1e-9)
            integ += e
            p += period - (0.01 * e + 0.0004 * integ) * period
            period = minOf(sps * 1.06, maxOf(sps * 0.94, period))
            prev = curr
        }
        return out
    }

    fun demodulate(ir: FloatArray, ii: FloatArray, rs: ReedSolomon = ReedSolomon(),
                   threshold: Double = 0.55): List<Frame> {
        val s = softStream(ir, ii)
        if (s.isEmpty()) return emptyList()
        val bs = bitsOfLong(BIT_SYNC, BIT_SYNC_LEN)
        val fsync = bitsOfLong(FRAME_SYNC, FRAME_SYNC_LEN)
        val need = ((FIRST_BLOCK_PAYLOAD + RS_PARITY) * 8 + CC_K - 1) * 2
        val out = ArrayList<Frame>()
        val span = (BIT_SYNC_LEN + FRAME_SYNC_LEN) * SPS
        val limit = s.size - span - 4 * SPS
        var p = 0
        while (p < limit) {
            if (corrAt(s, SPS.toDouble(), p.toDouble(), bs) > threshold) {
                var bestScore = -1e9; var bestQ = p.toDouble(); var bestSps = SPS.toDouble()
                var q = maxOf(0, p - SPS)
                while (q < minOf(limit, p + SPS)) {
                    for (k in 0 until 19) {
                        val cand = SPS * (0.955 + 0.09 * k / 18.0)
                        val c1 = corrAt(s, cand, q.toDouble(), bs)
                        if (c1 <= threshold) continue
                        val fp = q + BIT_SYNC_LEN * cand
                        val c2 = corrAt(s, cand, fp, fsync)
                        if (c2 > threshold && c1 + c2 > bestScore) {
                            bestScore = c1 + c2; bestQ = q.toDouble(); bestSps = cand
                        }
                    }
                    q++
                }
                if (bestScore > -1e8) {
                    val start = bestQ + (BIT_SYNC_LEN + FRAME_SYNC_LEN) * bestSps
                    val avail = ((s.size - start) / bestSps).toInt()
                    if (avail >= need) {
                        val sym = sampleSymbols(s, start, bestSps, avail)
                        decodeFrame(sym, rs, bestSps)?.let { out.add(it) }
                    }
                    p = (bestQ + span).toInt()
                    continue
                }
            }
            p++
        }
        return out
    }
}

/**
 * Front end for the phone's 240 kHz IQ stream: decimate 240k -> 48k (exactly
 * 5:1, giving the integer 10 samples/bit ALERT2 needs) and demodulate.
 */
object Alert2Front {
    private const val DECIM = AlertDsp.FS_IN / Alert2.CHANNEL_RATE   // 240000/48000 = 5
    private val LPF: DoubleArray = run {
        // windowed-sinc lowpass, cutoff 18 kHz of the 120 kHz Nyquist
        val n = 81
        val cutoff = 18_000.0 / (AlertDsp.FS_IN / 2.0)
        val h = DoubleArray(n)
        val m = n - 1
        var sum = 0.0
        for (i in 0 until n) {
            val k = i - m / 2.0
            val sinc = if (k == 0.0) cutoff else Math.sin(Math.PI * cutoff * k) / (Math.PI * k)
            val w = 0.54 - 0.46 * Math.cos(2.0 * Math.PI * i / m)
            h[i] = sinc * w
            sum += h[i]
        }
        for (i in 0 until n) h[i] /= sum
        h
    }

    /** Raw interleaved u8 IQ at 240 kHz -> decoded ALERT2 frames. */
    fun decode(iq: ByteArray): List<Alert2.Frame> {
        val n = iq.size / 2
        if (n < AlertDsp.FS_IN / 2) return emptyList()
        val outN = (n - LPF.size) / DECIM
        if (outN <= 0) return emptyList()
        val ir = FloatArray(outN)
        val ii = FloatArray(outN)
        var k = LPF.size - 1
        for (o in 0 until outN) {
            var ar = 0.0
            var ai = 0.0
            for (t in LPF.indices) {
                val idx = k - t
                ar += ((iq[2 * idx].toInt() and 0xFF) - 127.5) * LPF[t]
                ai += ((iq[2 * idx + 1].toInt() and 0xFF) - 127.5) * LPF[t]
            }
            ir[o] = ar.toFloat()
            ii[o] = ai.toFloat()
            k += DECIM
        }
        return Alert2.demodulate(ir, ii)
    }
}
