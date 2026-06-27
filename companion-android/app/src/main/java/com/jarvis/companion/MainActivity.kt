package com.jarvis.companion

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * One-screen config + control. Enter the backend URL + token, grant permissions, start the
 * service. Keeps it deliberately minimal — the real work is in CallMonitorService.
 */
class MainActivity : AppCompatActivity() {

    private val needed = buildList {
        add(Manifest.permission.READ_PHONE_STATE)
        add(Manifest.permission.ANSWER_PHONE_CALLS)
        add(Manifest.permission.READ_CALL_LOG)
        add(Manifest.permission.READ_CONTACTS)
        add(Manifest.permission.SEND_SMS)
        add(Manifest.permission.CALL_PHONE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
            add(Manifest.permission.POST_NOTIFICATIONS)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        val prefs = Prefs(this)

        val url = findViewById<EditText>(R.id.url)
        val token = findViewById<EditText>(R.id.token)
        val status = findViewById<TextView>(R.id.status)
        url.setText(prefs.backend)
        token.setText(prefs.token)

        findViewById<Button>(R.id.save).setOnClickListener {
            prefs.backend = url.text.toString()
            prefs.token = token.text.toString()
            if (!prefs.configured) { toast("Enter a full http(s):// backend URL"); return@setOnClickListener }
            requestPerms()
            startService(Intent(this, CallMonitorService::class.java).also {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(it)
            })
            status.text = "Running — watching your calls."
            toast("JARVIS companion started")
        }

        findViewById<Button>(R.id.battery).setOnClickListener { askIgnoreBatteryOptim() }
        findViewById<Button>(R.id.test).setOnClickListener {
            // Save what's currently typed BEFORE testing (otherwise Test uses the old saved URL).
            prefs.backend = url.text.toString()
            prefs.token = token.text.toString()
            ping(prefs)
        }

        status.text = if (prefs.configured) "Configured. Tap Save & Start." else "Enter your backend URL + token."
    }

    private fun requestPerms() {
        val missing = needed.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) ActivityCompat.requestPermissions(this, missing.toTypedArray(), 1)
    }

    private fun askIgnoreBatteryOptim() {
        try {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")))
        } catch (e: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun ping(prefs: Prefs) = Thread {
        val msg = try {
            val c = java.net.URL("${prefs.backend}/health").openConnection() as java.net.HttpURLConnection
            c.connectTimeout = 6000
            c.setRequestProperty("ngrok-skip-browser-warning", "true")
            val code = c.responseCode
            c.disconnect()
            if (code in 200..299) "Backend reachable ✓" else "Reached server but got HTTP $code"
        } catch (e: Exception) { "Can't reach backend — ${e.javaClass.simpleName}: ${e.message}" }
        runOnUiThread { toast(msg) }
    }.start()

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
}
