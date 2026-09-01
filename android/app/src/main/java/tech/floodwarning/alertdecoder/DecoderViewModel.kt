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
     * The three protocols, one at a time. iFLOWS and Enhanced iFLOWS share
     * the 300-baud AFSK air interface but use different 40-bit frame
     * formats; ALERT2 is an entirely different radio (4800 bps with FEC).
     * They are never run together - see Alert1Formats for why.
     */
    enum class Protocol(val label: String) {
        IFLOWS("iFLOWS"),
        ENHANCED_IFLOWS("Enhanced iFLOWS"),
        ALERT2("ALERT2")
    }

    var protocol: Protocol = Protocol.IFLOWS

    private val _state = MutableStateFlow(State.IDLE)
    val state: StateFlow<State> = _state

    private val _status = MutableStateFlow("Not started")
    val status: StateFlow<String> = _status

    private val _rows = MutableStateFlow<List<Row>>(emptyList())
    val rows: StateFlow<List<Row>> = _rows

    private val _clipping = MutableStateFlow(0.0)
    val clipping: StateFlow<Double> = _clipping

    var frequencyHz: Int = 151_500_000
    var gainTenthDb: Int = 250          // 25.0 dB; -1 = tuner AGC
    var ppm: Int = 0

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
            source.tune(frequencyHz, gainTenthDb, ppm)
            _state.value = State.LISTENING
            _status.value = "Listening on ${frequencyHz / 1_000_000.0} MHz"

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

                if (protocol == Protocol.IFLOWS || protocol == Protocol.ENHANCED_IFLOWS) {
                    AlertDsp.frameFormat = if (protocol == Protocol.IFLOWS)
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
