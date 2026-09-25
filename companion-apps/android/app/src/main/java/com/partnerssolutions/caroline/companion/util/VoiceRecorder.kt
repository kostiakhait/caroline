package com.partnerssolutions.caroline.companion.util

import android.content.Context
import android.media.MediaRecorder
import android.util.Base64
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import java.io.File
import kotlin.coroutines.coroutineContext

/**
 * Records one voice message for speech-to-text: tap to start, tap to stop
 * (no silence auto-stop) -- same behavior as Ratatosk's VoiceRecorder, which
 * this is ported from. AAC/m4a because MediaRecorder encodes it directly, so
 * there is no raw PCM handling and Caroline's `ai:stt` accepts it as-is.
 */
class VoiceRecorder(private val context: Context) {
    companion object {
        private const val POLL_INTERVAL_MS = 100L
        const val FORMAT = "m4a"
    }

    private var outputFile: File? = null

    /** Suspends until [shouldStop] returns true; returns the base64 audio, or null if nothing was captured. */
    suspend fun recordUntilStop(shouldStop: () -> Boolean): String? {
        val file = File.createTempFile("voice_", ".m4a", context.cacheDir)
        outputFile = file
        @Suppress("DEPRECATION")
        val recorder = MediaRecorder().apply {
            setAudioSource(MediaRecorder.AudioSource.MIC)
            setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            setOutputFile(file.absolutePath)
        }
        try {
            recorder.prepare()
            recorder.start()
        } catch (exc: Exception) {
            Logger.e("VoiceRecorder start failed", exc)
            runCatching { recorder.release() }
            cleanup()
            return null
        }
        try {
            while (coroutineContext.isActive && !shouldStop()) delay(POLL_INTERVAL_MS)
        } finally {
            runCatching { recorder.stop() }
            runCatching { recorder.release() }
        }
        val bytes = if (file.exists()) file.readBytes() else ByteArray(0)
        cleanup()
        return if (bytes.isEmpty()) null else Base64.encodeToString(bytes, Base64.NO_WRAP)
    }

    private fun cleanup() {
        outputFile?.delete()
        outputFile = null
    }
}
