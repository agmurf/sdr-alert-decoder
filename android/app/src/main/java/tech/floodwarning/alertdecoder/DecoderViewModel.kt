package tech.floodwarning.alertdecoder

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class DecoderViewModel(app: Application) : AndroidViewModel(app) {

    data class Row(
        val sensorId: Int,
        val raw: Int,
        val engineering: String,
        val name: String,
        val timeText: String,
        val votes: Int,
        val held: Boolean,         // rig's bit-12 flag was set on this frame
        val protocolLabel: String = ""
    )

    enum class State { IDLE, WAITING_DRIVER, CONNECTING, LISTENING, ERROR }

    /**
     * The three protocols, one at a time.
     *
     * Named for the FRAME FORMAT rather than the network. NSW operators call
     * their network "iFLOWS", but its frames are the ALERT Binary format -
     * all four bytes marked, no checksum - so labelling one option "iFLOWS"
     * and the other "Enhanced iFLOWS" made them look like two flavours of
     * one thing and named nothing an operator could check off air.
     *
     * ALERT Binary and Enhanced iFLOWS share the 300-baud AFSK air interface
     * but carry different 40-bit frames; ALERT2 is an entirely different
     * radio (4800 bps with FEC). They are never run together - see
     * Alert1Formats for why.
     */
    enum class Protocol(val label: String, val hint: String) {
        BINARY("ALERT Binary",
            "All four bytes marked, no checksum. What the live 151.5 MHz network sends."),
        ENHANCED_IFLOWS("Enhanced iFLOWS",
            "One byte marked plus a 6-bit CRC. What the ERT-A2 test rig sends."),
        ALERT2("ALERT2",
            "4800 bps with FEC - a different radio entirely.")
    }

    var protocol: Protocol = Protocol.BINARY

    /** What the radio is tuned to, as the UI shows and edits it. */
    data class Tuning(
        val frequencyHz: Int,
        val gainTenthDb: Int,      // -1 = tuner AGC
        val ppm: Int
    ) {
        val megahertzText: String get() = "%.4f".format(frequencyHz / 1e6)
        val gainText: String get() = if (gainTenthDb < 0) "auto" else "%.1f".format(gainTenthDb / 10.0)
    }

    private val _state = MutableStateFlow(State.IDLE)
    val state: StateFlow<State> = _state

    private val _status = MutableStateFlow("Not started")
    val status: StateFlow<String> = _status

    private val _rows = MutableStateFlow<List<Row>>(emptyList())
    val rows: StateFlow<List<Row>> = _rows

    private val _clipping = MutableStateFlow(0.0)
    val clipping: StateFlow<Double> = _clipping

    private val prefs = app.getSharedPreferences("tuning", android.content.Context.MODE_PRIVATE)

    // Networks differ: 151.5 MHz is the NSW iFLOWS channel, but ALERT is
    // deployed on other VHF channels elsewhere, so this has to be editable
    // rather than baked in.
    private val _tuning = MutableStateFlow(
        Tuning(
            frequencyHz = prefs.getInt("freq_hz", 151_500_000),
            gainTenthDb = prefs.getInt("gain_tenth_db", 250),
            ppm = prefs.getInt("ppm", 0)
        )
    )
    val tuning: StateFlow<Tuning> = _tuning

    val frequencyHz: Int get() = _tuning.value.frequencyHz
    val gainTenthDb: Int get() = _tuning.value.gainTenthDb
    val ppm: Int get() = _tuning.value.ppm

    /**
     * Apply operator-entered tuning. Persists it, and re-tunes live if we are
     * already listening - rtl_tcp takes FREQ/GAIN commands at any time, so
     * there is no need to tear the stream down.
     *
     * Returns null on success, or a message explaining what was rejected.
     */
    fun applyTuning(mhzText: String, gainText: String, ppmText: String): String? {
        val mhz = mhzText.trim().toDoubleOrNull()
            ?: return "Frequency must be a number in MHz, e.g. 151.5"
        // R820T/R828D tuning range. Outside it the dongle silently reports
        // success and delivers noise, which is worse than refusing.
        if (mhz < 24.0 || mhz > 1766.0)
            return "Frequency must be between 24 and 1766 MHz"

        val g = gainText.trim().lowercase()
        val gainTenths = when {
            g.isEmpty() || g == "auto" || g == "agc" -> -1
            else -> {
                val v = g.toDoubleOrNull()
                    ?: return "Gain must be a number in dB, or \"auto\""
                (v.coerceIn(0.0, 49.6) * 10).toInt()
            }
        }

        val p = ppmText.trim().let { if (it.isEmpty()) 0 else it.toIntOrNull() }
            ?: return "PPM must be a whole number"
        if (p < -200 || p > 200) return "PPM must be between -200 and 200"

        val t = Tuning((mhz * 1e6).toInt(), gainTenths, p)
        _tuning.value = t
        prefs.edit()
            .putInt("freq_hz", t.frequencyHz)
            .putInt("gain_tenth_db", t.gainTenthDb)
            .putInt("ppm", t.ppm)
            .apply()

        if (_state.value == State.LISTENING) {
            // Socket write - must not touch the main thread.
            viewModelScope.launch(Dispatchers.IO) {
                source.tune(t.frequencyHz, t.gainTenthDb, t.ppm)
                _status.value = "Listening on ${t.megahertzText} MHz  ·  gain ${t.gainText}"
            }
        }
        return null
    }

    val source = RtlSource()
    private var job: Job? = null
    private val fmt = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    private val _metaInfo = MutableStateFlow("No site metadata - showing raw values")
    val metaInfo: StateFlow<String> = _metaInfo

    init {
        SiteMetadata.load(app)
        refreshMetaInfo()
    }

    private fun refreshMetaInfo() {
        _metaInfo.value = if (SiteMetadata.isLoaded)
            "Site metadata: ${SiteMetadata.count} sites (${SiteMetadata.sourceName})"
        else
            "No site metadata - showing raw values"
    }

    /** Import an agency-supplied CSV. Returns a message for the UI. */
    fun importMetadata(uri: android.net.Uri, displayName: String?): String {
        val n = SiteMetadata.import(getApplication(), uri, displayName)
        refreshMetaInfo()
        return if (n > 0) "Imported $n sites" else
            "Could not read that CSV - it needs a header row with an 'id' column"
    }

    fun clearMetadata() {
        SiteMetadata.clear(getApplication())
        refreshMetaInfo()
    }

    fun setDriverLaunched() { _state.value = State.CONNECTING; _status.value = "Connecting to driver…" }

    fun setError(msg: String) { _state.value = State.ERROR; _status.value = msg }

    fun start() {
        if (job?.isActive == true) return
        job = viewModelScope.launch(Dispatchers.IO) {
            if (!source.connect()) {
                withContext(Dispatchers.Main) { setError("Could not connect to the RTL2832U driver") }
                return@launch
            }
            val t = _tuning.value
            source.tune(t.frequencyHz, t.gainTenthDb, t.ppm)
            _state.value = State.LISTENING
            _status.value = "Listening on ${t.megahertzText} MHz  ·  gain ${t.gainText}"

            // 3-second decode windows, matching the desktop pipeline
            val windowBytes = AlertDsp.FS_IN * 2 * 3
            val buf = ByteArray(windowBytes)
            while (isActive) {
                if (!source.readFully(buf)) {
                    _state.value = State.ERROR
                    _status.value = "IQ stream ended"
                    break
                }
                var clipped = 0
                for (b in buf) {
                    val v = b.toInt() and 0xFF
                    if (v <= 1 || v >= 254) clipped++
                }
                _clipping.value = clipped * 100.0 / buf.size

                val now = fmt.format(Date())
                val add = ArrayList<Row>()

                if (protocol == Protocol.BINARY || protocol == Protocol.ENHANCED_IFLOWS) {
                    AlertDsp.frameFormat = if (protocol == Protocol.BINARY)
                        Alert1Formats.BINARY else Alert1Formats.ENHANCED_IFLOWS
                    val readings = try { AlertDsp.decodeWindow(buf) } catch (e: Exception) { emptyList() }
                    readings.forEach {
                        add.add(Row(
                            sensorId = it.sensorId,
                            raw = it.value,
                            engineering = SiteMetadata.render(it.sensorId, it.value),
                            name = SiteMetadata.label(it.sensorId),
                            timeText = now,
                            votes = it.votes,
                            held = false,
                            protocolLabel = protocol.label
                        ))
                    }
                }
                if (protocol == Protocol.ALERT2) {
                    val frames = try { Alert2Front.decode(buf) } catch (e: Exception) { emptyList() }
                    for (f in frames) {
                        for ((hdr, reports) in Alert2App.parseAirLinkPayload(f.payload)) {
                            for (rep in reports) for (rd in rep.readings) {
                                val shown = rd.text ?: rd.value?.let {
                                    if (it == Math.floor(it)) it.toLong().toString()
                                    else String.format("%.3f", it)
                                } ?: "?"
                                val site = SiteMetadata.label(hdr.sourceAddress)
                                add.add(Row(
                                    sensorId = rd.sensorId,
                                    raw = rd.value?.toInt() ?: 0,
                                    engineering = shown,
                                    name = "site ${hdr.sourceAddress}" +
                                            (if (site.isNotEmpty()) "  ·  $site" else "") +
                                            "  ·  ${rep.typeName}" +
                                            if (rep.testFlag) "  [TEST DATA]" else "",
                                    timeText = now,
                                    votes = 0,
                                    held = false,
                                    protocolLabel = "ALERT2"
                                ))
                            }
                        }
                    }
                }
                if (add.isNotEmpty()) _rows.value = (add + _rows.value).take(200)
            }
        }
    }

    fun stop() {
        job?.cancel(); job = null
        source.close()
        _state.value = State.IDLE
        _status.value = "Stopped"
    }

    fun clear() { _rows.value = emptyList() }

    override fun onCleared() { stop(); super.onCleared() }
}
