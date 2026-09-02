package tech.floodwarning.alertdecoder

import android.app.Activity
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import android.provider.OpenableColumns
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel

private val BarBlue = Color(0xFF10469E)
private val BtnBlue = Color(0xFF12489F)
private val WarnOrange = Color(0xFFEF6C00)
private val PageGrey = Color(0xFFF4F4F4)
private val TextGrey = Color(0xFF5F6368)
private val HeldAmber = Color(0xFFB26A00)

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = lightColorScheme(primary = BtnBlue)) {
                val vm: DecoderViewModel = viewModel()
                val launcher = rememberLauncherForActivityResult(
                    ActivityResultContracts.StartActivityForResult()
                ) { res ->
                    if (res.resultCode == Activity.RESULT_OK) vm.start()
                    else vm.setError("Driver refused or no dongle detected. Install \"RTL2832U driver\", connect the SDR via OTG, then retry.")
                }
                val ctx = androidx.compose.ui.platform.LocalContext.current
                var toast by remember { mutableStateOf<String?>(null) }
                val csvPicker = rememberLauncherForActivityResult(
                    ActivityResultContracts.OpenDocument()
                ) { uri ->
                    if (uri != null) {
                        var name: String? = null
                        try {
                            ctx.contentResolver.query(uri, null, null, null, null)?.use { c ->
                                val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                                if (i >= 0 && c.moveToFirst()) name = c.getString(i)
                            }
                        } catch (_: Exception) { }
                        toast = vm.importMetadata(uri, name)
                    }
                }
                DecoderScreen(
                    vm = vm,
                    toast = toast,
                    onImportCsv = { csvPicker.launch(arrayOf("text/csv", "text/comma-separated-values", "text/plain", "*/*")) },
                    onStart = {
                        vm.setDriverLaunched()
                        try { launcher.launch(vm.source.buildOpenIntent()) }
                        catch (e: Exception) { vm.setError("RTL2832U driver app is not installed") }
                    }
                )
            }
        }
    }
}

@Composable
private fun DecoderScreen(
    vm: DecoderViewModel,
    toast: String? = null,
    onImportCsv: () -> Unit = {},
    onStart: () -> Unit
) {
    val rows by vm.rows.collectAsState()
    val state by vm.state.collectAsState()
    val status by vm.status.collectAsState()
    val clipping by vm.clipping.collectAsState()
    val metaInfo by vm.metaInfo.collectAsState()
    val tuning by vm.tuning.collectAsState()

    Column(Modifier.fillMaxSize().background(PageGrey)) {

        // ---- app bar -------------------------------------------------
        Box(
            Modifier.fillMaxWidth().background(BarBlue).statusBarsPadding()
                .padding(horizontal = 20.dp, vertical = 18.dp)
        ) {
            Text(
                "ALERT Decoder",
                color = Color.White,
                fontSize = 26.sp,
                fontWeight = FontWeight.Bold
            )
        }

        // ---- protocol selector ---------------------------------------
        // One protocol at a time. ALERT Binary and Enhanced iFLOWS share the
        // same 300-baud air interface but different 40-bit frame formats, and
        // Enhanced iFLOWS is loosely enough constrained that running both
        // together produces ghost stations. The hint under the row says which
        // real transmitter each has been decoded from.
        var proto by remember { mutableStateOf(vm.protocol) }
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            DecoderViewModel.Protocol.values().forEach { p ->
                val selected = proto == p
                Button(
                    onClick = { proto = p; vm.protocol = p },
                    modifier = Modifier.weight(1f).height(44.dp),
                    contentPadding = PaddingValues(horizontal = 2.dp),
                    shape = RoundedCornerShape(4.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = if (selected) BtnBlue else Color(0xFFDDE3EE)
                    )
                ) {
                    Text(p.label, color = if (selected) Color.White else TextGrey,
                        fontSize = 12.sp, fontWeight = FontWeight.Medium,
                        maxLines = 1)
                }
            }
        }
        Text(
            proto.hint, color = TextGrey, fontSize = 12.sp,
            modifier = Modifier.padding(horizontal = 16.dp).padding(bottom = 8.dp)
        )

        // ---- tuning --------------------------------------------------
        // Editable because ALERT is not on one channel nationally, and
        // because gain is the control for front-end overload.
        var mhz by remember(tuning) { mutableStateOf(tuning.megahertzText) }
        var gain by remember(tuning) { mutableStateOf(tuning.gainText) }
        var ppm by remember(tuning) { mutableStateOf(tuning.ppm.toString()) }
        var tuneError by remember { mutableStateOf<String?>(null) }

        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.Bottom
        ) {
            TuneField("Freq (MHz)", mhz, Modifier.weight(1.6f)) { mhz = it }
            TuneField("Gain (dB)", gain, Modifier.weight(1f)) { gain = it }
            TuneField("PPM", ppm, Modifier.weight(0.9f)) { ppm = it }
            Button(
                onClick = { tuneError = vm.applyTuning(mhz, gain, ppm) },
                modifier = Modifier.height(52.dp),
                shape = RoundedCornerShape(4.dp),
                contentPadding = PaddingValues(horizontal = 12.dp),
                colors = ButtonDefaults.buttonColors(containerColor = BtnBlue)
            ) { Text("SET", color = Color.White, fontSize = 13.sp) }
        }
        if (tuneError != null) {
            Text(
                tuneError!!, color = WarnOrange, fontSize = 13.sp,
                modifier = Modifier.padding(horizontal = 20.dp, vertical = 4.dp)
            )
        }

        // ---- header + status ----------------------------------------
        Column(Modifier.padding(horizontal = 20.dp, vertical = 14.dp)) {
            Text("Decoded sensor readings", color = TextGrey, fontSize = 19.sp)
            Spacer(Modifier.height(6.dp))
            val statusColor = when (state) {
                DecoderViewModel.State.ERROR -> WarnOrange
                DecoderViewModel.State.LISTENING -> Color(0xFF2E7D32)
                else -> TextGrey
            }
            Text(status, color = statusColor, fontSize = 15.sp)
            Spacer(Modifier.height(4.dp))
            Text(metaInfo, color = TextGrey, fontSize = 13.sp)
            if (toast != null) {
                Spacer(Modifier.height(4.dp))
                Text(toast, color = BarBlue, fontSize = 13.sp)
            }
            if (clipping > 0.1) {
                Spacer(Modifier.height(4.dp))
                Text(
                    "Front end overloading (%.2f%% clipping) — lower the gain".format(clipping),
                    color = WarnOrange, fontSize = 14.sp
                )
            }
            if (rows.isEmpty() && state == DecoderViewModel.State.LISTENING) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "No readings yet. Stations transmit intermittently — leave this running.",
                    color = TextGrey, fontSize = 14.sp
                )
            }
        }

        // ---- readings ------------------------------------------------
        LazyColumn(Modifier.weight(1f).fillMaxWidth()) {
            items(rows) { r -> ReadingRow(r) }
        }

        // ---- buttons -------------------------------------------------
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            val listening = state == DecoderViewModel.State.LISTENING
            BigButton(if (listening) "STOP" else "START LISTENING") {
                if (listening) vm.stop() else onStart()
            }
            BigButton("IMPORT SITE CSV") { onImportCsv() }
            BigButton("CLEAR") { vm.clear() }
            Spacer(Modifier.navigationBarsPadding())
        }
    }
}

/** Compact labelled field for the tuning row. */
@Composable
private fun TuneField(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    onChange: (String) -> Unit
) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text(label, fontSize = 11.sp) },
        singleLine = true,
        textStyle = androidx.compose.ui.text.TextStyle(fontSize = 15.sp),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Text),
        modifier = modifier
    )
}

@Composable
private fun ReadingRow(r: DecoderViewModel.Row) {
    Column(
        Modifier.fillMaxWidth().background(Color.White)
            .padding(horizontal = 20.dp, vertical = 12.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                r.sensorId.toString(),
                fontSize = 26.sp,
                fontWeight = FontWeight.Bold,
                color = BarBlue,
                modifier = Modifier.width(96.dp)
            )
            Text(
                r.engineering,
                fontSize = 24.sp,
                fontWeight = FontWeight.SemiBold,
                color = Color(0xFF202124),
                modifier = Modifier.weight(1f)
            )
            Text(r.timeText, fontSize = 15.sp, color = TextGrey)
        }
        Spacer(Modifier.height(2.dp))
        // With no imported metadata there is no site name to show - the
        // decoder reports only what came off the air.
        val sub = listOf(r.protocolLabel, r.name).filter { it.isNotBlank() }
            .joinToString("  ·  ")
        if (sub.isNotEmpty()) Text(sub, fontSize = 13.sp, color = TextGrey)
        if (r.held) {
            Text(
                "flag bit set — value may be held, not a fresh reading",
                fontSize = 12.sp, color = HeldAmber
            )
        }
    }
    HorizontalDivider(color = Color(0xFFE0E0E0))
}

@Composable
private fun BigButton(label: String, onClick: () -> Unit) {
    Button(
        onClick = onClick,
        modifier = Modifier.fillMaxWidth().height(56.dp),
        shape = RoundedCornerShape(4.dp),
        colors = ButtonDefaults.buttonColors(containerColor = BtnBlue)
    ) {
        Text(
            label, color = Color.White, fontSize = 18.sp,
            fontWeight = FontWeight.Medium, textAlign = TextAlign.Center
        )
    }
}
