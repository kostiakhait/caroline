namespace CarolineInstaller;

/// <summary>
/// Enforces a size cap on workspace\dehydrated\ at install/update time, as a
/// guaranteed backstop alongside the backend's own periodic prune (see
/// backend-py's app/archive_prune.py).
///
/// Why this exists here too (2026-10-05, found live): a stuck session's old
/// pre-compaction hook (fixed 2026-09-20) left 662 GB of near-duplicate
/// transcript dumps in workspace\dehydrated\ on one machine. The backend's
/// own prune only runs while Caroline is actually running, a couple of
/// minutes after each start, and only removes what it can PROVE is
/// redundant -- a sound design for day-to-day operation, but it offers no
/// guarantee that a machine left running for weeks, or one hit by some
/// future bug this file's logic doesn't yet know how to prove redundant,
/// ever gets swept. The install/update step is the one point execution is
/// guaranteed to reach, so it gets an unconditional size cap: unlike
/// archive_prune.py's own stages, this does NOT try to prove any individual
/// file redundant first -- it just deletes the oldest files once the
/// directory is over budget. See AppPaths.DehydratedDir's own comment for
/// why this is the one piece of workspace\ the installer is allowed to
/// touch at all.
///
/// Deliberately simple and dependency-free (no Python/backend runtime
/// required -- this must work on a fresh install too, before any runtime is
/// even downloaded) rather than reusing archive_prune.py's byte-level
/// redundancy proofs from C#. Never throws and never fails the install, same
/// posture as DefenderExclusion.cs; runs in silent/background-update mode
/// too (no UAC needed -- it's the user's own per-user folder), which matters
/// most here: a silent background update is exactly the unattended case
/// where nobody would otherwise notice disk usage creeping up.
/// </summary>
internal static class WorkspaceCleanup
{
    // Mirrors backend-py's archive_prune.MAX_DEHYDRATED_DIR_BYTES -- same
    // number, same reasoning, kept here too since this step must also work
    // on a machine the backend hasn't run on in a long time (or ever, on a
    // version old enough to predate archive_prune.py entirely).
    private const long MaxDehydratedDirBytes = 5L * 1024 * 1024 * 1024;

    // A file this fresh might be mid-write by a Caroline instance that's
    // still running (the installer doesn't force-kill it until later in the
    // flow) -- leave it for the backend's own prune, or next time.
    private static readonly TimeSpan YoungFileAge = TimeSpan.FromMinutes(2);

    public static Task RunAsync(CancellationToken ct) => Task.Run(() =>
    {
        try
        {
            var dir = AppPaths.DehydratedDir;
            if (!Directory.Exists(dir))
            {
                Logger.Log($"WorkspaceCleanup: {dir} doesn't exist yet, nothing to do.");
                return;
            }

            var files = new DirectoryInfo(dir).GetFiles("*", SearchOption.TopDirectoryOnly);
            var totalBytes = files.Sum(f => f.Length);
            if (totalBytes <= MaxDehydratedDirBytes)
            {
                Logger.Log($"WorkspaceCleanup: {dir} is {totalBytes / 1e9:F2} GB, under the {MaxDehydratedDirBytes / 1e9:F0} GB cap -- nothing to do.");
                return;
            }

            Logger.Log($"WorkspaceCleanup: {dir} is {totalBytes / 1e9:F2} GB, over the {MaxDehydratedDirBytes / 1e9:F0} GB cap -- evicting oldest files.");
            var now = DateTime.UtcNow;
            var deletedCount = 0;
            long deletedBytes = 0;
            foreach (var f in files.OrderBy(f => f.LastWriteTimeUtc))
            {
                if (ct.IsCancellationRequested) break;
                if (totalBytes <= MaxDehydratedDirBytes) break;
                if (now - f.LastWriteTimeUtc < YoungFileAge) continue;
                try
                {
                    var size = f.Length;
                    f.Delete();
                    totalBytes -= size;
                    deletedBytes += size;
                    deletedCount++;
                }
                catch (Exception ex)
                {
                    Logger.Log($"WorkspaceCleanup: failed to delete {f.FullName} (ignored, will retry next run): {ex.Message}");
                }
            }
            Logger.Log($"WorkspaceCleanup: deleted {deletedCount} file(s), {deletedBytes / 1e9:F2} GB; {dir} now {totalBytes / 1e9:F2} GB.");
        }
        catch (Exception ex)
        {
            // Never fails the install -- disk cleanup is a courtesy, not a requirement.
            Logger.Log($"WorkspaceCleanup: unexpected failure (ignored, install continues): {ex}");
        }
    }, ct);
}
