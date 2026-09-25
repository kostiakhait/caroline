package com.partnerssolutions.caroline.companion.util

import android.content.Context
import android.media.MediaPlayer
import android.util.Base64
import java.io.File

/** Plays one synthesized reply (base64 MP3 from `ai:tts`); starting a new one stops the previous. */
class VoicePlayer(private val context: Context) {
    private var player: MediaPlayer? = null
    private var file: File? = null

    fun play(base64Mp3: String, onDone: () -> Unit) {
        stop()
        val f = File.createTempFile("tts_", ".mp3", context.cacheDir)
        f.writeBytes(Base64.decode(base64Mp3, Base64.DEFAULT))
        file = f
        val mp = MediaPlayer()
        player = mp
        try {
            mp.setDataSource(f.absolutePath)
            mp.setOnCompletionListener { stop(); onDone() }
            mp.setOnErrorListener { _, what, extra ->
                Logger.e("VoicePlayer error what=$what extra=$extra")
                stop(); onDone(); true
            }
            mp.prepare()
            mp.start()
        } catch (exc: Exception) {
            Logger.e("VoicePlayer failed", exc)
            stop()
            onDone()
        }
    }

    fun stop() {
        runCatching { player?.stop() }
        runCatching { player?.release() }
        player = null
        file?.delete()
        file = null
    }
}
