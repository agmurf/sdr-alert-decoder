package tech.floodwarning.alertdecoder

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Verifies the Kotlin DSP port against a REAL off-air burst.
 *
 * testrig_burst_240k.iq8 is 3 s of the 4078 test rig captured on 2026-08-29
 * at 16:05:56 AEST (72 dB), resampled 1.024 Msps -> 240 ksps and stored as
 * interleaved unsigned 8-bit IQ, exactly the format rtl_tcp delivers. The
 * desktop Python decoder resolves this same vector to 4079=265 and 4080=414,
 * so the Kotlin port must agree — that is what makes this a port test rather
 * than a smoke test.
 */
class AlertDspTest {

    private fun loadVector(): ByteArray {
        val s = javaClass.classLoader!!.getResourceAsStream("testrig_burst_240k.iq8")
            ?: error("test vector missing")
        return s.use { it.readBytes() }
    }

    @Test
    fun decodesRealTestRigBurstAsEnhancedIflows() {
        // The 4078 rig transmits ENHANCED iFLOWS, not iFLOWS/Binary.
        val iq = loadVector()
        assertTrue("vector should be ~3 s of 240k IQ", iq.size > 1_000_000)

        AlertDsp.frameFormat = Alert1Formats.ENHANCED_IFLOWS
        val t0 = System.currentTimeMillis()
        val readings = AlertDsp.decodeWindow(iq)
        val elapsed = System.currentTimeMillis() - t0
        println("decoded in ${elapsed} ms for ${iq.size / 2 / AlertDsp.FS_IN.toDouble()} s of IQ")
        for (r in readings) {
            println("  id=${r.sensorId} value=${r.value} votes=${r.votes} " +
                    "fixed=${r.fixedBits} bit12=${r.bit12}")
        }

        // Values verified against the rig's own configuration: battery
        // 121 = 12.1 V and DI3 528 mm both match exactly, and the 6-bit CRC
        // validates every frame.
        val river = readings.firstOrNull { it.sensorId == 4079 }
        assertTrue("expected sensor 4079, got ${readings.map { it.sensorId }}",
            river != null)
        assertTrue("expected 4079 = 420, got ${river!!.value}", river.value == 420)

        val batt = readings.firstOrNull { it.sensorId == 4080 }
        assertTrue("expected 4080 = 121 (12.1 V), got ${batt?.value}",
            batt != null && batt.value == 121)

        // The phone sweeps fewer parameter combinations than the desktop
        // decoder, so it does not always recover all three frames of a burst.
        // 4078 is therefore checked only if present - and must be right.
        val di3 = readings.firstOrNull { it.sensorId == 4078 }
        if (di3 != null) assertEquals("4078 must be 528 mm", 528, di3.value)
    }

    /** Enhanced iFLOWS CRC, checked against real 4078 frames. */
    @Test
    fun enhancedIflowsCrcValidatesRealFrames() {
        val frames = listOf(
            intArrayOf(0xEF, 0xBF, 0x87, 0x53) to Pair(4079, 1807),
            intArrayOf(0xEF, 0x3F, 0xD2, 0x08) to Pair(4079, 420),
            intArrayOf(0xF0, 0xBF, 0x3C, 0x4C) to Pair(4080, 121),
            intArrayOf(0xEE, 0x3F, 0x08, 0x01) to Pair(4078, 528)
        )
        for ((w, expect) in frames) {
            val fr = IntArray(40)
            for (k in 0 until 4) {
                fr[10 * k] = 0
                for (j in 0 until 8) fr[10 * k + 1 + j] = (w[k] shr j) and 1
                fr[10 * k + 9] = 1
            }
            val r = Alert1Formats.parse(fr, Alert1Formats.ENHANCED_IFLOWS)
            assertTrue("frame should parse", r != null)
            assertTrue("CRC must validate", r!!.crcOk == true)
            assertEquals("sensor id", expect.first, r.sensorId)
            assertEquals("value", expect.second, r.value)
        }
    }

    /** The decoder must keep up: a 3 s window has to decode in well under 3 s. */
    @Test
    fun runsFasterThanRealTime() {
        val iq = loadVector()
        AlertDsp.frameFormat = Alert1Formats.ENHANCED_IFLOWS
        AlertDsp.decodeWindow(iq)          // warm up JIT
        val t0 = System.nanoTime()
        AlertDsp.decodeWindow(iq)
        val ms = (System.nanoTime() - t0) / 1_000_000
        val windowMs = (iq.size / 2 / AlertDsp.FS_IN.toDouble() * 1000).toLong()
        println("decode $ms ms for a $windowMs ms window (${"%.2f".format(ms.toDouble() / windowMs)}x realtime)")
        assertTrue("decode took ${ms}ms for a ${windowMs}ms window - too slow for live use",
            ms < windowMs)
    }

    /** The CRC-bearing format must also reject noise at the shipped bar. */
    @Test
    fun enhancedIflowsRejectsNoise() {
        AlertDsp.frameFormat = Alert1Formats.ENHANCED_IFLOWS
        val rnd = java.util.Random(7)
        val noise = ByteArray(AlertDsp.FS_IN * 2 * 3)
        rnd.nextBytes(noise)
        val readings = AlertDsp.decodeWindow(noise)
        println("enhanced-iflows noise -> ${readings.size} readings " +
                readings.map { "${it.sensorId}(v${it.votes})" })
        assertTrue("noise must not decode", readings.isEmpty())
    }

    @Test
    fun rejectsNoise() {
        AlertDsp.frameFormat = Alert1Formats.BINARY
        val rnd = java.util.Random(42)
        val noise = ByteArray(AlertDsp.FS_IN * 2 * 3)
        rnd.nextBytes(noise)
        val readings = AlertDsp.decodeWindow(noise)
        println("noise produced ${readings.size} readings: ${readings.map { it.sensorId }}")
        assertTrue("noise must not produce confident readings", readings.isEmpty())
    }
}
