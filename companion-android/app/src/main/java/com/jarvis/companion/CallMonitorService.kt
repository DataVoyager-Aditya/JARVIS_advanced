package com.jarvis.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.os.Build
import android.os.IBinder
import android.telecom.TelecomManager
import android.content.BroadcastReceiver
import android.content.IntentFilter
import android.telephony.SmsManager
import android.telephony.TelephonyManager
import android.util.Log
import androidx.core.app.NotificationCompat
import java.util.concurrent.Executors

/**
 * The always-on heart of the companion. A foreground service that:
 *   1. Watches phone state via the PHONE_STATE broadcast (gives the incoming number too, with
 *      READ_CALL_LOG) and reports ring / answered / missed to the JARVIS backend.
 *   2. While a call is ringing, long-polls /calls/commands and executes decline / answer / silence
 *      via TelecomManager + AudioManager.
 *   3. On a ring, checks the cached auto-handle rules (Phase 8.5) and may auto-text / auto-answer /
 *      auto-decline a matching caller — entirely on the phone.
 *
 * Audio note: this companion does NOT touch the call's voice stream (Android blocks that for
 * third-party apps). Conversational answering is the separate PC-as-Bluetooth-handset path.
 */
class CallMonitorService : Service() {

    private val io = Executors.newSingleThreadExecutor()
    private lateinit var api: Api
    private lateinit var tm: TelephonyManager
    private lateinit var telecom: TelecomManager

    @Volatile private var ringing = false
    @Volatile private var handledThisCall = false   // a command/rule already acted on this ring
    @Volatile private var currentNumber = ""
    @Volatile private var currentRef = ""
    @Volatile private var lastState = TelephonyManager.CALL_STATE_IDLE
    @Volatile private var rules: List<Triple<String, String, String>> = emptyList()
    @Volatile private var running = true
    private var poller: Thread? = null

    // PHONE_STATE broadcast gives BOTH the call state AND the incoming number (the number only
    // when READ_CALL_LOG is granted). The newer TelephonyCallback DROPS the number on Android 12+,
    // which is why saved callers showed "unknown" — so we read the broadcast instead.
    private val phoneReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            if (intent.action != "android.intent.action.PHONE_STATE") return
            val number = intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER) ?: ""
            val state = when (intent.getStringExtra(TelephonyManager.EXTRA_STATE)) {
                TelephonyManager.EXTRA_STATE_RINGING -> TelephonyManager.CALL_STATE_RINGING
                TelephonyManager.EXTRA_STATE_OFFHOOK -> TelephonyManager.CALL_STATE_OFFHOOK
                else -> TelephonyManager.CALL_STATE_IDLE
            }
            handleState(state, number.ifEmpty { null })
        }
    }

    override fun onBind(i: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val p = Prefs(this)
        api = Api(p.backend, p.token)
        tm = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
        telecom = getSystemService(Context.TELECOM_SERVICE) as TelecomManager
        startForeground(NOTIF_ID, buildNotification("JARVIS is watching your calls"))
        registerPhoneState()
        startPoller()                                // always-on: ring commands AND outbound dials
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val p = Prefs(this)
        api.configure(p.backend, p.token)
        return START_STICKY                          // Android restarts us if killed
    }

    // ---- phone-state watching --------------------------------------------- //
    private fun registerPhoneState() {
        registerReceiver(phoneReceiver, IntentFilter("android.intent.action.PHONE_STATE"))
    }

    private fun handleState(state: Int, incomingNumber: String?) {
        when (state) {
            TelephonyManager.CALL_STATE_RINGING -> {
                val number = incomingNumber ?: ""
                if (!ringing) onRingStart(number)
                else if (number.isNotEmpty() && currentNumber.isEmpty()) {
                    // a later RINGING broadcast finally carried the number — fix the report
                    currentNumber = number
                    io.execute { api.reportIncoming(number, contactName(number), currentRef) }
                }
            }
            TelephonyManager.CALL_STATE_OFFHOOK -> {
                // Picked up (by him or by us). Report answered, stop polling.
                if (ringing) reportEnded(answered = true)
                stopRingHandling()
            }
            TelephonyManager.CALL_STATE_IDLE -> {
                if (lastState == TelephonyManager.CALL_STATE_RINGING && !handledThisCall) {
                    // Rang then went idle without being answered -> a missed call.
                    val num = currentNumber
                    io.execute { api.reportMissed(num, contactName(num), "miss-$num-${System.currentTimeMillis()}") }
                } else if (ringing) {
                    reportEnded(answered = false)
                }
                stopRingHandling()
            }
        }
        lastState = state
    }

    private fun onRingStart(number: String) {
        if (ringing) return
        ringing = true
        handledThisCall = false
        currentNumber = number
        currentRef = "ring-$number-${System.currentTimeMillis()}"
        val name = contactName(number)
        io.execute {
            api.reportIncoming(number, name, currentRef)
            applyRule(number, name)            // auto-handle rule may end/answer it; the always-on
        }                                      // poller (started in onCreate) carries any commands
    }

    private fun stopRingHandling() {
        ringing = false                        // the poller is permanent (handles dials too) — leave it
    }

    private fun reportEnded(answered: Boolean) {
        val num = currentNumber; val ref = currentRef
        io.execute { api.reportEnded(num, ref, answered) }
    }

    // ---- command polling (decline / answer / silence / dial) -------------- //
    // ONE always-on poller for the service's life: ring commands act on the live call; a "dial"
    // command places an outbound call even when nothing is ringing. Rules are refreshed alongside.
    private fun startPoller() {
        poller = Thread {
            var tick = 0
            try {
                while (running && !Thread.currentThread().isInterrupted) {
                    for (cmd in api.pollCommands()) execute(cmd)
                    if (tick++ % 15 == 0) refreshRules()      // ~every 30s, pick up rule changes
                    Thread.sleep(POLL_MS)
                }
            } catch (_: InterruptedException) { }
        }.also { it.start() }
    }

    private fun execute(cmd: Command) {
        when (cmd.action) {
            "dial" -> placeCall(cmd.number)                   // outbound — works any time
            "decline" -> if (ringing) {
                endCall()
                if (cmd.message.isNotBlank())                 // decline AND text the caller back
                    sendSms(cmd.number.ifEmpty { currentNumber }, cmd.message)
                handledThisCall = true; stopRingHandling()
            }
            "answer"  -> if (ringing) { answerCall(); handledThisCall = true }
            "silence" -> if (ringing) silenceRinger()
            else -> Log.w(TAG, "unknown command: ${cmd.action}")
        }
    }

    // ---- auto-handle rules (Phase 8.5) ------------------------------------ //
    /** Returns true if a rule terminally handled the call (so we skip command polling). */
    private fun applyRule(number: String, name: String): Boolean {
        val hay = "$name $number".lowercase()
        val rule = rules.firstOrNull { it.first.isNotEmpty() && hay.contains(it.first.lowercase()) } ?: return false
        return when (rule.second) {
            "auto_decline" -> { endCall(); handledThisCall = true; stopRingHandling(); true }
            "auto_text" -> {
                endCall()
                sendSms(number, rule.third.ifEmpty { "Can't take your call right now — I'll call you back." })
                handledThisCall = true; stopRingHandling(); true
            }
            "auto_answer" -> { answerCall(); speakerOn(); handledThisCall = true; false }
            else -> false
        }
    }

    private fun refreshRules() {                  // called on the poller thread (already off-main)
        val r = api.pullRules()
        if (r.isNotEmpty() || rules.isEmpty()) rules = r
    }

    // ---- telephony actions ------------------------------------------------ //
    private fun endCall() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) telecom.endCall()
        } catch (e: SecurityException) { Log.w(TAG, "endCall denied — grant ANSWER_PHONE_CALLS: ${e.message}") }
    }

    private fun answerCall() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) telecom.acceptRingingCall()
        } catch (e: SecurityException) { Log.w(TAG, "acceptRingingCall denied — grant ANSWER_PHONE_CALLS: ${e.message}") }
    }

    private fun silenceRinger() = try { telecom.silenceRinger() } catch (e: Exception) { Log.w(TAG, "silence: ${e.message}") }

    /** Phase 8.5 — place an OUTBOUND call from the phone ("JARVIS, call Mom"). Needs CALL_PHONE.
     *  JARVIS only dials; the boss talks on the phone. */
    private fun placeCall(number: String) {
        if (number.isBlank()) return
        val uri = android.net.Uri.fromParts("tel", number, null)
        val hasPerm = checkSelfPermission(android.Manifest.permission.CALL_PHONE) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        if (!hasPerm) { Log.w(TAG, "dial blocked — CALL_PHONE not granted"); return }
        try {
            // TelecomManager.placeCall works from a background service; ACTION_CALL via
            // startActivity is blocked by Android 10+ background-activity-launch rules.
            telecom.placeCall(uri, null)
            Log.i(TAG, "dialing $number via Telecom")
        } catch (e: Exception) {
            Log.w(TAG, "telecom.placeCall failed (${e.message}) — falling back to ACTION_CALL")
            try {
                startActivity(Intent(Intent.ACTION_CALL, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            } catch (e2: Exception) { Log.w(TAG, "dial failed: ${e2.message}") }
        }
    }

    private fun speakerOn() = try {
        (getSystemService(Context.AUDIO_SERVICE) as AudioManager).isSpeakerphoneOn = true
    } catch (e: Exception) { Log.w(TAG, "speaker: ${e.message}") }

    private fun sendSms(number: String, text: String) = try {
        val sms = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            getSystemService(SmsManager::class.java) else @Suppress("DEPRECATION") SmsManager.getDefault()
        sms.sendTextMessage(number, null, text, null, null)
    } catch (e: Exception) { Log.w(TAG, "sms: ${e.message}") }

    private fun contactName(number: String): String {
        if (number.isBlank()) return ""
        return try {
            val uri = android.net.Uri.withAppendedPath(
                android.provider.ContactsContract.PhoneLookup.CONTENT_FILTER_URI, android.net.Uri.encode(number))
            contentResolver.query(uri, arrayOf(android.provider.ContactsContract.PhoneLookup.DISPLAY_NAME),
                null, null, null)?.use { if (it.moveToFirst()) it.getString(0) else "" } ?: ""
        } catch (e: Exception) { "" }
    }

    // ---- notification ----------------------------------------------------- //
    private fun buildNotification(text: String): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(CHANNEL) == null)
                nm.createNotificationChannel(NotificationChannel(CHANNEL, "JARVIS Calls", NotificationManager.IMPORTANCE_LOW))
        }
        return NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("JARVIS Companion")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        super.onDestroy()
        running = false
        poller?.interrupt()
        try { unregisterReceiver(phoneReceiver) } catch (_: Exception) { }
        io.shutdownNow()
    }

    companion object {
        private const val TAG = "JarvisCallSvc"
        private const val CHANNEL = "jarvis_calls"
        private const val NOTIF_ID = 7801
        private const val POLL_MS = 2000L
    }
}
