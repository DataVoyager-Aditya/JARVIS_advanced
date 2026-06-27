package com.jarvis.companion

import android.content.Context

/** Persisted config: the backend URL + shared token. */
class Prefs(ctx: Context) {
    private val sp = ctx.getSharedPreferences("jarvis", Context.MODE_PRIVATE)

    var backend: String
        get() {
            var b = sp.getString("backend", "")!!.trim().trimEnd('/')
            // Tolerate a URL typed without a scheme ("xxx.ngrok-free.dev") — default to https,
            // otherwise java.net.URL throws "no protocol".
            if (b.isNotEmpty() && !b.startsWith("http://") && !b.startsWith("https://")) b = "https://$b"
            return b
        }
        set(v) = sp.edit().putString("backend", v.trim().trimEnd('/')).apply()

    var token: String
        get() = sp.getString("token", "jarvis-local-calls")!!
        set(v) = sp.edit().putString("token", v.trim()).apply()

    val configured: Boolean get() = backend.startsWith("http")
}
