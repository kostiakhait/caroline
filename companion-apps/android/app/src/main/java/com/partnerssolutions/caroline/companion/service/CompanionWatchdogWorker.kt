package com.partnerssolutions.caroline.companion.service

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.partnerssolutions.caroline.companion.data.companion.restartCompanionServiceIfNeeded
import com.partnerssolutions.caroline.companion.util.Logger
import java.util.concurrent.TimeUnit

/**
 * Periodic backstop for CompanionOpsService's own resilience mechanisms
 * (START_STICKY, onTaskRemoved+AlarmManager, BootReceiver -- see
 * CompanionOpsService's own doc comment) and CompanionTabsScreen's
 * cold-start/ON_RESUME checks: all of those only fire on a specific EVENT
 * (a kill, a reboot, the user opening the app). Per explicit instruction
 * (2026-09-30) -- "перезапускать... если он отключился по таймеру" -- this
 * is the one that fires on its OWN schedule regardless of whether the user
 * ever opens the app or any of those other events happen to occur, so a
 * service an OEM battery optimizer quietly stopped doesn't just stay dead
 * until the user happens to notice SMS/contacts aren't working.
 *
 * 15 minutes is WorkManager's own hard floor for PeriodicWorkRequest
 * (setPeriodicityTimeMillis enforces MIN_PERIODIC_INTERVAL_MILLIS at the
 * platform level) -- not a choice made here, the real minimum Android
 * allows for any app's periodic background work.
 */
class CompanionWatchdogWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val restarted = restartCompanionServiceIfNeeded(applicationContext)
        if (restarted) Logger.w("CompanionWatchdogWorker: service was enabled but not running -- restarted it")
        return Result.success()
    }

    companion object {
        private const val WORK_NAME = "companion_ops_watchdog"

        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<CompanionWatchdogWorker>(15, TimeUnit.MINUTES).build()
            // KEEP, not REPLACE: CompanionApplication.onCreate runs on
            // every process start, and re-enqueuing a periodic worker that
            // already exists would just reset its own internal schedule
            // without changing anything real -- same "don't stack up
            // duplicate loops" reasoning as CompanionOpsService's own
            // pollJob guard.
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, request)
        }
    }
}
