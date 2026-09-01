package tech.floodwarning.alertdecoder

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * On-device ALERT1 decoder — a direct port of the proven desktop chain
 * (src/fsk_pll_decode.py + src/iq_decoder.py).
 *
 * The signal is AFSK over narrowband FM: audio mark 1300.8 Hz / space
 * 2109.4 Hz at 300 baud. An audio tone f puts RF sideband lines at BOTH
 * ±f around the FM carrier, so we matched-filter all four lines and
 * combine each tone's pair non-coherently — that uses the full signal
 * energy and is ~6-10 dB better than detecting one sideband.
 *
 * Chain: u8 IQ @240k -> carrier search -> mix+decimate /20 -> 12 kHz
 *        -> 4-sideband matched filter -> Gardner + brute phase
 *        -> 40-bit ALERT frame -> vote consensus.
 */
object AlertDsp {

    const val FS_IN = 240_000          // request this from the dongle: 240k/20 = 12k exactly
    const val DECIM = 20
    const val FS_BB = FS_IN / DECIM    // 12 000 Hz
    const val BAUD = 300
    const val SPB = FS_BB / BAUD       // 40 samples/symbol, integer — no timing drift

    const val AFSK_F1 = 1300.8         // mark / idle tone
    const val AFSK_F2 = 2109.4         // space tone

    private const val SEARCH_HZ = 10_000.0
    private const val DC_NOTCH_HZ = 200.0

    /** Marker + UART bits of a valid ALERT BINARY frame (16 positions). */
    private val FIXED = mapOf(
        0 to 0, 7 to 1, 8 to 0, 9 to 1,
        10 to 0, 17 to 1, 18 to 0, 19 to 1,
        20 to 0, 27 to 1, 28 to 1, 29 to 1,
        30 to 0, 37 to 1, 38 to 1, 39 to 1
    )

    /** The 8 UART start/stop positions — structure every ALERT frame has. */
    private val UART_BITS = mapOf(
        0 to 0, 9 to 1, 10 to 0, 19 to 1,
        20 to 0, 29 to 1, 30 to 0, 39 to 1
    )

    /**
     * Which 40-bit frame format to decode. Exactly ONE at a time: Enhanced
     * iFLOWS has only 8 bits of constraint against iFLOWS/Binary's 16, so
     * running both makes a strong Binary burst yield systematically
     * CRC-valid Enhanced iFLOWS ghosts that no vote threshold removes.
     */
    var frameFormat: String = Alert1Formats.BINARY

    /** Anti-alias lowpass for the /20 decimation (cutoff 5 kHz of 120 kHz Nyquist). */
    private val LPF: FloatArray = firwin(121, 5_000.0 / (FS_IN / 2.0))

    data class Reading(
        val sensorId: Int,
        val value: Int,
        val votes: Int,
        val fixedBits: Int,
        val bit12: Int,
        val timestampMs: Long = System.currentTimeMillis()
    )

    // ---------------------------------------------------------------- filters

    private fun firwin(numTaps: Int, cutoffNorm: Double): FloatArray {
        // windowed-sinc lowpass, Hamming window; cutoffNorm is fraction of Nyquist
        val h = FloatArray(numTaps)
        val m = numTaps - 1
        var sum = 0.0
        for (i in 0 until numTaps) {
            val n = i - m / 2.0
            val sinc = if (n == 0.0) cutoffNorm else sin(PI * cutoffNorm * n) / (PI * n)
            val w = 0.54 - 0.46 * cos(2.0 * PI * i / m)
            val v = sinc * w
            h[i] = v.toFloat()
            sum += v
        }
        for (i in 0 until numTaps) h[i] = (h[i] / sum).toFloat()
        return h
    }

    // -------------------------------------------------------------------- FFT

    /** In-place iterative radix-2 FFT. re/im length must be a power of two. */
    fun fft(re: FloatArray, im: FloatArray) {
        val n = re.size
        var j = 0
        for (i in 1 until n) {
            var bit = n shr 1
            while (j and bit != 0) { j = j xor bit; bit = bit shr 1 }
            j = j or bit
            if (i < j) {
                var t = re[i]; re[i] = re[j]; re[j] = t
                t = im[i]; im[i] = im[j]; im[j] = t
            }
        }
        var len = 2
        while (len <= n) {
            val ang = -2.0 * PI / len
            val wr = cos(ang).toFloat(); val wi = sin(ang).toFloat()
            var i = 0
            while (i < n) {
                var cr = 1f; var ci = 0f
                for (k in 0 until len / 2) {
                    val ur = re[i + k]; val ui = im[i + k]
                    val vr = re[i + k + len / 2] * cr - im[i + k + len / 2] * ci
                    val vi = re[i + k + len / 2] * ci + im[i + k + len / 2] * cr
                    re[i + k] = ur + vr; im[i + k] = ui + vi
                    re[i + k + len / 2] = ur - vr; im[i + k + len / 2] = ui - vi
                    val ncr = cr * wr - ci * wi
                    ci = cr * wi + ci * wr; cr = ncr
                }
                i += len
            }
            len = len shl 1
        }
    }

    /**
     * Spectral peaks within ±SEARCH_HZ (DC spike notched) → carrier-offset
     * candidates. Note these usually land on the strongest TONE line rather
     * than the FM carrier, which is why decode() also tries ±AFSK_F1.
     */
    fun carrierCandidates(ir: FloatArray, ii: FloatArray, maxCands: Int = 3): List<Double> {
        val n = 8192.coerceAtMost(Integer.highestOneBit(ir.size))
        if (n < 1024) return emptyList()
        val re = FloatArray(n); val im = FloatArray(n)
        System.arraycopy(ir, 0, re, 0, n); System.arraycopy(ii, 0, im, 0, n)
        fft(re, im)
        val binHz = FS_IN.toDouble() / n
        val mag = FloatArray(n)
        for (i in 0 until n) mag[i] = re[i] * re[i] + im[i] * im[i]
        // collect (freq, power) inside the search band, DC notched
        val cand = ArrayList<Pair<Double, Float>>()
        for (i in 0 until n) {
            val f = if (i <= n / 2) i * binHz else (i - n) * binHz
            if (abs(f) > SEARCH_HZ || abs(f) <= DC_NOTCH_HZ) continue
            cand.add(Pair(f, mag[i]))
        }
        if (cand.isEmpty()) return emptyList()
        val med = cand.map { it.second }.sorted()[cand.size / 2]
        val sorted = cand.sortedByDescending { it.second }
        val out = ArrayList<Double>()
        for ((f, p) in sorted) {
            if (p < 6f * med) break
            if (out.none { abs(it - f) < 600.0 }) out.add(f)
            if (out.size >= maxCands) break
        }
        return out
    }

    // ------------------------------------------------------ mix + decimate

    /**
     * Tune to [centerHz] and decimate by DECIM to FS_BB using a polyphase
     * lowpass (only the retained samples are computed).
     */
    fun mixDecimate(ir: FloatArray, ii: FloatArray, centerHz: Double): Pair<FloatArray, FloatArray> {
        val n = ir.size
        val taps = LPF.size
        // Mix the whole block once with an incremental phasor. Computing
        // sin/cos per sample per tap instead would be ~26M trig calls per
        // second on a phone - unusable.
        val mr = FloatArray(n); val mi = FloatArray(n)
        val w = -2.0 * PI * centerHz / FS_IN
        val dr = cos(w); val di = sin(w)
        var pr = 1.0; var pi = 0.0
        for (i in 0 until n) {
            mr[i] = (ir[i] * pr - ii[i] * pi).toFloat()
            mi[i] = (ir[i] * pi + ii[i] * pr).toFloat()
            val nr = pr * dr - pi * di
            pi = pr * di + pi * dr
            pr = nr
            // renormalise occasionally so rounding cannot shrink the phasor
            if (i and 0x3FF == 0) {
                val m = sqrt(pr * pr + pi * pi)
                if (m > 1e-9) { pr /= m; pi /= m }
            }
        }
        // Polyphase decimation: only the retained output samples are computed.
        val outN = (n - taps) / DECIM
        if (outN <= 0) return Pair(FloatArray(0), FloatArray(0))
        val or_ = FloatArray(outN); val oi = FloatArray(outN)
        var k = taps - 1
        for (o in 0 until outN) {
            var accR = 0f; var accI = 0f
            val base = k
            for (t in 0 until taps) {
                val h = LPF[t]; val idx = base - t
                accR += mr[idx] * h; accI += mi[idx] * h
            }
            or_[o] = accR; oi[o] = accI
            k += DECIM
        }
        return Pair(or_, oi)
    }

    // -------------------------------------------------- AFSK soft decision

    /**
     * Matched-filter all four sideband lines and combine each tone's pair
     * non-coherently:  d = (|MF(+f1)|+|MF(-f1)|) - (|MF(+f2)|+|MF(-f2)|)
     */
    fun afskSoft(br: FloatArray, bi: FloatArray): FloatArray {
        val n = br.size
        if (n < SPB * 4) return FloatArray(0)
        val outN = n - SPB + 1
        val d = FloatArray(outN)
        // precompute the four reference tones over one symbol
        val tones = doubleArrayOf(AFSK_F1, -AFSK_F1, AFSK_F2, -AFSK_F2)
        val cr = Array(4) { FloatArray(SPB) }
        val ci = Array(4) { FloatArray(SPB) }
        for (t in 0 until 4) for (s in 0 until SPB) {
            val ph = -2.0 * PI * tones[t] * s / FS_BB
            cr[t][s] = cos(ph).toFloat(); ci[t][s] = sin(ph).toFloat()
        }
        for (i in 0 until outN) {
            var e = FloatArray(4)
            for (t in 0 until 4) {
                var ar = 0f; var ai = 0f
                for (s in 0 until SPB) {
                    val xr = br[i + s]; val xi = bi[i + s]
                    // correlate with conj(tone)
                    ar += xr * cr[t][s] + xi * ci[t][s]
                    ai += xi * cr[t][s] - xr * ci[t][s]
                }
                e[t] = hypot(ar.toDouble(), ai.toDouble()).toFloat()
            }
            d[i] = (e[0] + e[1]) - (e[2] + e[3])
        }
        // normalise
        var mean = 0.0
        for (v in d) mean += v.toDouble()
        mean /= outN
        var sd = 0.0
        for (v in d) { val z = v - mean; sd += z * z }
        sd = sqrt(sd / outN) + 1e-9
        for (i in 0 until outN) d[i] = ((d[i] - mean) / sd).toFloat()
        return d
    }

    // ------------------------------------------------- timing recovery

    /** Gardner timing recovery — PI loop steering the symbol period. */
    fun gardner(d: FloatArray, sps: Int, kp: Double = 0.02, ki: Double = 0.001): FloatArray {
        val n = d.size
        if (n < sps * 4) return FloatArray(0)
        fun interp(pos: Double): Float {
            val k = pos.toInt()
            if (k < 0 || k + 1 >= n) return 0f
            val f = (pos - k).toFloat()
            return d[k] * (1 - f) + d[k + 1] * f
        }
        val out = ArrayList<Float>(n / sps + 4)
        var period = sps.toDouble()
        var p = sps.toDouble()
        var prev = interp(p - period)
        var integ = 0.0
        while (p < n - 1) {
            val curr = interp(p)
            val mid = interp(p - period / 2.0)
            var e = (mid * (curr - prev)).toDouble()
            e /= (abs(curr) + abs(prev) + 1e-9)
            integ += e
            val corr = kp * e + ki * integ
            out.add(curr)
            prev = curr
            p += period - corr * period
            period = period.coerceIn(sps * 0.95, sps * 1.05)
        }
        return out.toFloatArray()
    }

    // ------------------------------------------------------------ framing

    private fun parseFrame(fr: IntArray): Triple<Int, Int, Int> {
        val w = IntArray(4)
        for (k in 0 until 4) {
            var b = 0
            for (j in 0 until 8) if (fr[k * 10 + 1 + j] != 0) b = b or (1 shl j)
            w[k] = b
        }
        val sid = (w[0] and 63) + 64 * (w[1] and 63) + 4096 * (w[2] and 1)
        val v = (w[3] and 63) * 32 + ((w[2] and 62) shr 1)
        return Triple(sid, v, w[2] and 1)
    }

    /** Slice symbols (both polarities) into frames of the chosen format. */
    private fun framesFrom(soft: FloatArray, out: MutableList<Alert1Formats.Parsed>) {
        if (soft.size < 40) return
        for (inv in 0..1) {
            val bits = IntArray(soft.size) {
                val v = if (inv == 1) -soft[it] else soft[it]
                if (v > 0) 1 else 0
            }
            for (p in 0..bits.size - 40) {
                val fr = IntArray(40) { bits[p + it] }
                val r = Alert1Formats.parse(fr, frameFormat) ?: continue
                // sid 5461 is the 0x55 preamble parsing as a frame, never a
                // real station.
                if (r.sensorId == 5461 && r.format == Alert1Formats.BINARY) continue
                if (r.crcOk == false) continue
                out.add(r)
            }
        }
    }

    // ------------------------------------------------------------- decode

    /**
     * Decode one window of raw u8 IQ (interleaved I,Q as delivered by
     * rtl_tcp). Returns readings that cleared the vote threshold.
     */
    /**
     * @param minVotesCrc bar for CRC-bearing frames (Enhanced iFLOWS).
     *   Calibrated on a real 4078 burst: genuine frames score 6-7 here while
     *   chance CRC matches score 1 and noise yields nothing. The phone sweeps
     *   fewer parameter combinations than the desktop decoder, so its counts
     *   are far lower than the desktop's 70+ - do not copy that threshold.
     */
    fun decodeWindow(iq: ByteArray, minVotes: Int = 4,
                     minVotesCrc: Int = 4): List<Reading> {
        val n = iq.size / 2
        if (n < FS_IN) return emptyList()
        val ir = FloatArray(n); val ii = FloatArray(n)
        for (i in 0 until n) {
            ir[i] = ((iq[2 * i].toInt() and 0xFF) - 127.5f)
            ii[i] = ((iq[2 * i + 1].toInt() and 0xFF) - 127.5f)
        }
        val cands = carrierCandidates(ir, ii)
        if (cands.isEmpty()) return emptyList()

        val votes = HashMap<Long, Int>()
        val best = HashMap<Long, IntArray>()   // key -> [fixed, bit12]
        val frames = ArrayList<Alert1Formats.Parsed>()

        for (c in cands) {
            // the FFT peak is usually a tone line, so try the carrier at
            // c, c-f1 and c+f1
            for (hyp in doubleArrayOf(c, c - AFSK_F1, c + AFSK_F1)) {
                // Sweep the carrier a little either side. The desktop decoder
                // tries ~72 (centre, shift, baud, stream) combinations per
                // burst; too few here and real frames sit under the vote
                // threshold — that is what made the battery frame drop out.
                for (dc in doubleArrayOf(-150.0, -75.0, 0.0, 75.0, 150.0)) {
                    val (br, bi) = mixDecimate(ir, ii, hyp + dc)
                    val d = afskSoft(br, bi)
                    if (d.size < SPB * 20) continue
                    frames.clear()
                    framesFrom(gardner(d, SPB), frames)
                    // brute-force symbol phases (Gardner can fail to lock low SNR)
                    val scores = (0 until SPB).map { ph ->
                        var s = 0f; var i = ph
                        while (i < d.size) { s += abs(d[i]); i += SPB }
                        Pair(ph, s)
                    }.sortedByDescending { it.second }.take(6)
                    for ((ph, _) in scores) {
                        val strideN = (d.size - ph + SPB - 1) / SPB
                        if (strideN < 40) continue
                        val s = FloatArray(strideN) { d[ph + it * SPB] }
                        framesFrom(s, frames)
                    }
                    val seen = HashSet<Long>()
                    for (f in frames) {
                        val key = f.sensorId.toLong() * 4096L + f.value.toLong()
                        if (seen.add(key)) votes[key] = (votes[key] ?: 0) + 1
                        val prev = best[key]
                        val fx = f.fixed
                        if (prev == null || fx > prev[0])
                            best[key] = intArrayOf(fx, if (f.crcOk == true) 1 else 0)
                    }
                }
            }
        }
        val out = ArrayList<Reading>()
        for ((key, v) in votes) {
            val meta = best[key] ?: intArrayOf(0, 0)
            val crcBacked = meta[1] == 1
            // A 6-bit CRC is only a 1-in-64 filter and thousands of bit
            // positions are tested per burst, so it lowers the bar without
            // replacing consensus - measured, real rig frames score 70+.
            val need = if (crcBacked) minVotesCrc else minVotes
            if (v < need) continue
            out.add(Reading((key / 4096L).toInt(), (key % 4096L).toInt(),
                            v, meta[0], meta[1]))
        }
        return out.sortedByDescending { it.votes }
    }
}
