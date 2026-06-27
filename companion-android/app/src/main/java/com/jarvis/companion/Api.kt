package com.jarvis.companion

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL

/** A command from JARVIS: a ring action (decline/answer/silence) or a dial (number set).
 *  `message` (decline only) = also text the caller this. */
data class Command(val action: String, val number: String, val message: String = "")

/**
 * Tiny dependency-free HTTP client for talking to the JARVIS backend. Plain HttpURLConnection so
 * the APK stays small and has no networking libraries to keep updated. All calls are blocking and
 * MUST be made off the main thread (the service uses a single worker thread).
 */
class Api(private var base: String, private var token: String) {

    fun configure(base: String, token: String) {
        this.base = base.trimEnd('/')
        this.token = token
    }

    private fun open(path: String, method: String): HttpURLConnection {
        val c = URL("$base$path").openConnection() as HttpURLConnection
        c.requestMethod = method
        c.connectTimeout = 6000
        c.readTimeout = 12000
        c.setRequestProperty("x-jarvis-token", token)
        c.setRequestProperty("Accept", "application/json")
        c.setRequestProperty("ngrok-skip-browser-warning", "true")  // skip ngrok's free interstitial
        return c
    }

    /** POST a JSON body; returns the response text or null on failure. */
    fun postJson(path: String, body: JSONObject): String? = try {
        val c = open(path, "POST")
        c.doOutput = true
        c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(body.toString().toByteArray()) }
        readIf2xx(c)
    } catch (e: Exception) {
        Log.w(TAG, "POST $path failed: ${e.message}"); null
    }

    /** GET; returns the response text or null on failure. */
    fun get(path: String): String? = try {
        readIf2xx(open(path, "GET"))
    } catch (e: Exception) {
        Log.w(TAG, "GET $path failed: ${e.message}"); null
    }

    private fun readIf2xx(c: HttpURLConnection): String? {
        return try {
            if (c.responseCode in 200..299)
                c.inputStream.bufferedReader().use(BufferedReader::readText)
            else { Log.w(TAG, "HTTP ${c.responseCode} on ${c.url}"); null }
        } finally { c.disconnect() }
    }

    // ---- typed helpers ---------------------------------------------------- //
    fun reportIncoming(number: String, name: String, ref: String) =
        postJson("/calls/incoming", evt(number, name, ref))

    fun reportMissed(number: String, name: String, ref: String) =
        postJson("/calls/missed", evt(number, name, ref))

    fun reportEnded(number: String, ref: String, answered: Boolean) =
        postJson("/calls/ended", JSONObject().put("number", number).put("ref", ref).put("answered", answered))

    /** Returns queued commands — ring actions (decline/answer/silence) AND outbound dials
     *  (action="dial", number set). */
    fun pollCommands(): List<Command> {
        val txt = get("/calls/commands") ?: return emptyList()
        return try {
            val arr = JSONObject(txt).optJSONArray("commands") ?: JSONArray()
            (0 until arr.length()).mapNotNull {
                val o = arr.getJSONObject(it)
                val a = o.optString("action")
                if (a.isEmpty()) null else Command(a, o.optString("number"), o.optString("message"))
            }
        } catch (e: Exception) { emptyList() }
    }

    /** Returns the auto-handle rules: list of (match, action, message). */
    fun pullRules(): List<Triple<String, String, String>> {
        val txt = get("/calls/rules") ?: return emptyList()
        return try {
            val arr = JSONObject(txt).optJSONArray("rules") ?: JSONArray()
            (0 until arr.length()).map {
                val o = arr.getJSONObject(it)
                Triple(o.optString("match"), o.optString("action"), o.optString("message"))
            }
        } catch (e: Exception) { emptyList() }
    }

    private fun evt(number: String, name: String, ref: String) = JSONObject()
        .put("number", number).put("name", name).put("ref", ref)

    companion object { private const val TAG = "JarvisApi" }
}
