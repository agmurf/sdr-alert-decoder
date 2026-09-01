package tech.floodwarning.alertdecoder

import android.content.Intent
import android.net.Uri
import java.io.DataInputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket

/**
 * IQ source backed by the "RTL2832U driver" app (the free driver used by
 * SDR Touch). That app owns the USB device and serves an rtl_tcp stream on
 * localhost, which keeps this app free of NDK/libusb and USB permission
 * handling — we just speak the rtl_tcp protocol over a loopback socket.
 *
 * Flow:  buildOpenIntent() -> startActivityForResult -> connect() -> read()
 */
class RtlSource(
    private val port: Int = 14423,
    private val sampleRate: Int = AlertDsp.FS_IN
) {
    private var socket: Socket? = null
    private var input: DataInputStream? = null
    private var output: OutputStream? = null

    /** rtl_tcp command opcodes. */
    private object Cmd {
        const val FREQ = 0x01
        const val SAMPLE_RATE = 0x02
        const val GAIN_MODE = 0x03      // 0 = auto, 1 = manual
        const val GAIN = 0x04           // tenths of a dB
        const val FREQ_CORRECTION = 0x05
        const val AGC_MODE = 0x08
    }

    /**
     * Intent that asks the driver app to open the dongle and start serving.
     * Launch with startActivityForResult; a non-OK result means no driver
     * installed, no OTG device, or the user denied USB permission.
     */
    fun buildOpenIntent(): Intent =
        Intent(Intent.ACTION_VIEW).apply {
            data = Uri.parse("iqsrc://-a 127.0.0.1 -p $port -s $sampleRate")
        }

    /** True once the driver is serving and we have read its header. */
    val isConnected: Boolean get() = socket?.isConnected == true

    /**
     * Connect to the driver's local rtl_tcp server and consume the 12-byte
     * header ("RTL0" + tuner type + gain count).
     */
    fun connect(timeoutMs: Int = 8000): Boolean {
        close()
        return try {
            val s = Socket()
            s.connect(InetSocketAddress("127.0.0.1", port), timeoutMs)
            s.tcpNoDelay = true
            socket = s
            input = DataInputStream(s.getInputStream())
            output = s.getOutputStream()
            val header = ByteArray(12)
            input!!.readFully(header)
            String(header, 0, 4) == "RTL0"
        } catch (e: Exception) {
            close()
            false
        }
    }

    private fun send(cmd: Int, param: Int) {
        val o = output ?: return
        val b = ByteArray(5)
        b[0] = cmd.toByte()
        b[1] = (param shr 24).toByte()
        b[2] = (param shr 16).toByte()
        b[3] = (param shr 8).toByte()
        b[4] = param.toByte()
        try { o.write(b); o.flush() } catch (_: Exception) {}
    }

    fun tune(freqHz: Int, gainTenthDb: Int, ppm: Int) {
        send(Cmd.SAMPLE_RATE, sampleRate)
        send(Cmd.AGC_MODE, 0)
        if (gainTenthDb < 0) {
            send(Cmd.GAIN_MODE, 0)                 // tuner AGC
        } else {
            send(Cmd.GAIN_MODE, 1)
            send(Cmd.GAIN, gainTenthDb)
        }
        if (ppm != 0) send(Cmd.FREQ_CORRECTION, ppm)
        send(Cmd.FREQ, freqHz)
    }

    fun setGain(gainTenthDb: Int) {
        if (gainTenthDb < 0) send(Cmd.GAIN_MODE, 0)
        else { send(Cmd.GAIN_MODE, 1); send(Cmd.GAIN, gainTenthDb) }
    }

    /** Blocking read of exactly [buf].size bytes of interleaved u8 IQ. */
    fun readFully(buf: ByteArray): Boolean = try {
        input?.readFully(buf); true
    } catch (e: Exception) { false }

    fun close() {
        try { socket?.close() } catch (_: Exception) {}
        socket = null; input = null; output = null
    }
}
