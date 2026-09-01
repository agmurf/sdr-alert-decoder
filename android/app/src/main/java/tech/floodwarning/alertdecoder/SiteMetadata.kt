package tech.floodwarning.alertdecoder

import android.content.Context
import android.net.Uri
import java.io.File

/**
 * Optional, user-supplied site metadata.
 *
 * The app ships NO site database. Sensor registers are agency data and are
 * not ours to redistribute, so the decoder reports exactly what came off the
 * air - sensor id, value and timestamp - and nothing more. An operator may
 * import their own CSV, which is stored privately to this app and used only
 * to label the decoded readings.
 *
 * Expected CSV (header required, column order free, extra columns ignored):
 *
 *     id,name,type,unit,multiplier
 *     1001,Example Creek D/S Levee,River,m,0.01
 *     1002,Example Creek U/S Levee,Rain,mm,0.254
 *
 *   id          integer sensor / ALERT address (required)
 *   name        free text shown beside the reading
 *   type        free text, e.g. River / Rain / Batt
 *   unit        engineering unit shown after the scaled value
 *   multiplier  raw value is multiplied by this to give engineering units
 *
 * Only `id` is required; anything missing simply is not displayed.
 */
object SiteMetadata {

    data class Site(
        val id: Int,
        val name: String,
        val type: String,
        val unit: String,
        val multiplier: Double
    )

    private const val FILE_NAME = "site_metadata.csv"

    private val byId = HashMap<Int, Site>()

    /** Null until a CSV has been imported. */
    var sourceName: String? = null
        private set

    val count: Int get() = byId.size
    val isLoaded: Boolean get() = byId.isNotEmpty()

    private fun file(ctx: Context) = File(ctx.filesDir, FILE_NAME)

    /** Load any previously imported CSV. Safe to call repeatedly. */
    fun load(ctx: Context) {
        if (byId.isNotEmpty()) return
        val f = file(ctx)
        if (!f.exists()) return
        try {
            parse(f.readText())
            sourceName = ctx.getSharedPreferences("meta", Context.MODE_PRIVATE)
                .getString("source", f.name)
        } catch (_: Exception) {
            byId.clear()
        }
    }

    /**
     * Import a CSV chosen by the user. Returns the number of rows accepted,
     * or -1 if the file could not be read or contained no usable rows.
     */
    fun import(ctx: Context, uri: Uri, displayName: String?): Int {
        return try {
            val text = ctx.contentResolver.openInputStream(uri)?.use {
                it.readBytes().toString(Charsets.UTF_8)
            } ?: return -1
            byId.clear()
            val n = parse(text)
            if (n <= 0) {
                byId.clear()
                return -1
            }
            file(ctx).writeText(text)          // keep it for next launch
            sourceName = displayName ?: "imported CSV"
            ctx.getSharedPreferences("meta", Context.MODE_PRIVATE).edit()
                .putString("source", sourceName).apply()
            n
        } catch (e: Exception) {
            -1
        }
    }

    fun clear(ctx: Context) {
        byId.clear()
        sourceName = null
        try {
            file(ctx).delete()
            ctx.getSharedPreferences("meta", Context.MODE_PRIVATE).edit()
                .remove("source").apply()
        } catch (_: Exception) {
        }
    }

    /** Split a CSV line honouring simple double-quoted fields. */
    private fun splitCsv(line: String): List<String> {
        val out = ArrayList<String>()
        val sb = StringBuilder()
        var inQuotes = false
        for (c in line) {
            when {
                c == '"' -> inQuotes = !inQuotes
                c == ',' && !inQuotes -> { out.add(sb.toString().trim()); sb.setLength(0) }
                else -> sb.append(c)
            }
        }
        out.add(sb.toString().trim())
        return out
    }

    private fun parse(text: String): Int {
        val lines = text.lineSequence()
            .map { it.trim() }
            .filter { it.isNotEmpty() && !it.startsWith("#") }
            .toList()
        if (lines.isEmpty()) return 0

        val header = splitCsv(lines[0]).map { it.lowercase() }
        // Accept a few common spellings so an agency export usually just works.
        fun col(vararg names: String): Int =
            names.firstNotNullOfOrNull { n ->
                header.indexOf(n).takeIf { it >= 0 }
            } ?: -1

        val iId = col("id", "sensor_id", "sensorid", "errts id", "errts_id", "address")
        val iName = col("name", "site_name", "site name", "site", "description")
        val iType = col("type", "sensor_type", "sensor type")
        val iUnit = col("unit", "units")
        val iMult = col("multiplier", "mult", "scale", "factor")
        if (iId < 0) return 0                 // without an id there is nothing to key on

        var n = 0
        for (line in lines.drop(1)) {
            val p = splitCsv(line)
            if (iId >= p.size) continue
            val id = p[iId].toIntOrNull() ?: continue
            fun at(i: Int) = if (i in 0 until p.size) p[i] else ""
            val mult = at(iMult).toDoubleOrNull() ?: 1.0
            byId[id] = Site(id, at(iName), at(iType), at(iUnit), mult)
            n++
        }
        return n
    }

    fun site(id: Int): Site? = byId[id]

    /** Site label, or empty when no metadata has been imported for this id. */
    fun label(id: Int): String {
        val s = byId[id] ?: return ""
        return listOf(s.name, s.type).filter { it.isNotBlank() }.joinToString("  ·  ")
    }

    /**
     * Engineering rendering. With no metadata the RAW value is shown - the
     * decoder never invents units it was not given.
     */
    fun render(id: Int, raw: Int): String {
        val s = byId[id] ?: return raw.toString()
        if (s.unit.isBlank() && s.multiplier == 1.0) return raw.toString()
        val v = raw * s.multiplier
        val dp = when {
            s.multiplier <= 0.001 -> 3
            s.multiplier < 1.0 -> 2
            else -> 1
        }
        return if (s.unit.isBlank()) String.format("%.${dp}f", v)
        else String.format("%.${dp}f %s", v, s.unit)
    }
}
