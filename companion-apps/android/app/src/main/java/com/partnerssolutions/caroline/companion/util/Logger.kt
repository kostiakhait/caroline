package com.partnerssolutions.caroline.companion.util

import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.FileProvider
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Minimal file logger + "share logs" action, same idea as the Ratatosk
 * Android app's own Logger. Writes timestamped lines to
 * filesDir/logs/companion.log (single file, truncated when it gets past
 * ~1MB so it can't grow without bound), mirrors to logcat, and can hand
 * the file to any share target via a FileProvider.
 */
object Logger {
    private const val TAG = "CarolineCompanion"
    private const val MAX_BYTES = 1_000_000L
    private var logFile: File? = null

    fun init(context: Context) {
        val dir = File(context.filesDir, "logs").apply { mkdirs() }
        logFile = File(dir, "companion.log")
    }

    @Synchronized
    fun i(message: String) = write("I", message)

    @Synchronized
    fun w(message: String, throwable: Throwable? = null) =
        write("W", if (throwable != null) "$message\n${Log.getStackTraceString(throwable)}" else message)

    @Synchronized
    fun e(message: String, throwable: Throwable? = null) =
        write("E", if (throwable != null) "$message\n${Log.getStackTraceString(throwable)}" else message)

    private fun write(level: String, message: String) {
        Log.println(
            when (level) { "E" -> Log.ERROR; "W" -> Log.WARN; else -> Log.INFO },
            TAG, message,
        )
        val file = logFile ?: return
        try {
            if (file.exists() && file.length() > MAX_BYTES) file.writeText("")
            val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US).format(Date())
            file.appendText("$stamp $level $message\n")
        } catch (_: Exception) {
            // Logging must never itself throw into the caller.
        }
    }

    /** Fires an ACTION_SEND chooser with the current log file attached. */
    fun shareLogs(context: Context) {
        val file = logFile ?: return
        if (!file.exists()) {
            i("shareLogs: no log file yet")
            return
        }
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_STREAM, uri)
            putExtra(Intent.EXTRA_SUBJECT, "Caroline Companion logs")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(Intent.createChooser(intent, "Share logs").apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        })
    }
}
