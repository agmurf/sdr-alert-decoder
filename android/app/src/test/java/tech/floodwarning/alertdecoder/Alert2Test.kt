package tech.floodwarning.alertdecoder

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.random.Random

/**
 * Verifies the Kotlin ALERT2 port against the same checks the Python
 * implementation passes, including the specification's own worked example
 * and a modulated test vector the desktop decoder resolves correctly.
 */
class Alert2Test {

    @Test
    fun mlsScramblerIsSelfInverse() {
        val d = ByteArray(24) { it.toByte() }
        assertTrue(Alert2.mlsScramble(Alert2.mlsScramble(d)).contentEquals(d))
    }

    @Test
    fun reedSolomonCorrectsUpToEightSymbols() {
        val rs = Alert2.ReedSolomon()
        val msg = ByteArray(24) { Random(1).nextInt(256).toByte() }
        val cw = rs.encode(msg)
        assertEquals("codeword is 24 data + 16 parity", 40, cw.size)
        assertTrue("clean decode", rs.decode(cw)!!.contentEquals(msg))

        for (nerr in 1..8) {
            var good = 0
            for (trial in 0 until 20) {
                val rnd = Random(nerr * 100 + trial)
                val bad = cw.copyOf()
                val pos = (cw.indices).shuffled(rnd).take(nerr)
                for (p in pos) bad[p] = (bad[p].toInt() xor (1 + rnd.nextInt(255))).toByte()
                if (rs.decode(bad)?.contentEquals(msg) == true) good++
            }
            assertEquals("must correct $nerr symbol errors", 20, good)
        }
        // beyond capability it must fail rather than silently mis-correct
        var wrong = 0
        for (trial in 0 until 20) {
            val rnd = Random(9000 + trial)
            val bad = cw.copyOf()
            for (p in cw.indices.shuffled(rnd).take(10))
                bad[p] = (bad[p].toInt() xor (1 + rnd.nextInt(255))).toByte()
            val out = rs.decode(bad)
            if (out != null && !out.contentEquals(msg)) wrong++
        }
        assertEquals("must never silently mis-correct", 0, wrong)
    }

    @Test
    fun frameRoundTripsAcrossBlockBoundaries() {
        for (len in intArrayOf(1, 10, 22, 23, 40, 54, 80)) {
            val payload = ByteArray(len) { Random(len).nextInt(256).toByte() }
            val bits = Alert2.encodeFrame(payload)
            val soft = DoubleArray(bits.size) { if (bits[it] == 1) 1.0 else -1.0 }
            val f = Alert2.decodeFrame(soft)
            assertNotNull("payload $len bytes should decode", f)
            assertEquals("header length for $len", len, f!!.header.length)
            assertTrue("payload $len bytes roundtrip", f.payload.contentEquals(payload))
        }
    }

    /** The spec's Figure 4-5 Combined Report, byte for byte. */
    @Test
    fun parsesSpecificationWorkedExample() {
        val hex = "30 02 0A 00 14 00 00 00 68 14 0F 0A 02 01 08 12 12 03 24 13 22 02 76"
        val pdu = hex.split(" ").map { it.toInt(16).toByte() }.toByteArray()
        val reports = Alert2App.parseApplication(pdu)
        val all = reports.flatMap { it.readings }
        val byId = all.associateBy { it.sensorId }
        // spec: rain accumulator 104, pH (sensor 18) 804, water temp (19) 630
        assertEquals(104.0, byId[0]!!.value!!, 1e-9)
        assertEquals(804.0, byId[18]!!.value!!, 1e-9)
        assertEquals(630.0, byId[19]!!.value!!, 1e-9)
        assertEquals(listOf(20, 15, 10, 2), byId[0]!!.timeOffsets)
    }

    @Test
    fun decodesRealModulatedFrame() {
        val s = javaClass.classLoader!!.getResourceAsStream("alert2_frame_48k.iq8")
            ?: error("test vector missing")
        val raw = s.use { it.readBytes() }
        val n = raw.size / 2
        val ir = FloatArray(n)
        val ii = FloatArray(n)
        for (i in 0 until n) {
            ir[i] = (raw[2 * i].toInt() and 0xFF) - 127.5f
            ii[i] = (raw[2 * i + 1].toInt() and 0xFF) - 127.5f
        }
        val t0 = System.currentTimeMillis()
        val frames = Alert2.demodulate(ir, ii)
        println("demodulated ${frames.size} frame(s) in ${System.currentTimeMillis() - t0} ms")
        assertTrue("should find a frame", frames.isNotEmpty())

        val expected = "00 10 70 0b 0f ef 30 01 08 12 12 03 24 13 22 02 76"
            .split(" ").map { it.toInt(16).toByte() }.toByteArray()
        val hit = frames.firstOrNull { it.payload.contentEquals(expected) }
        assertNotNull("payload must match what the desktop decoder resolves", hit)

        // and it must parse through MANT + Application to real readings
        val parsed = Alert2App.parseAirLinkPayload(hit!!.payload)
        assertTrue("one MANT PDU", parsed.isNotEmpty())
        val (hdr, reports) = parsed[0]
        println("  MANT source address = ${hdr.sourceAddress}")
        assertEquals("MANT source address", 4079, hdr.sourceAddress)
        val readings = reports.flatMap { it.readings }.associateBy { it.sensorId }
        for (r in readings) println("  sensor ${r.key} = ${r.value.value}")
        assertEquals(804.0, readings[18]!!.value!!, 1e-9)
        assertEquals(630.0, readings[19]!!.value!!, 1e-9)
    }
}
