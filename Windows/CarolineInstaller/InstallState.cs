using System.Text.Json;

namespace CarolineInstaller;

/// <summary>
/// Single JSON file holding every piece of root-level state the installer itself needs
/// to persist across runs: which sha256 build is currently installed, and which app-*
/// directory under AppPaths.Root is the active one. Per explicit instruction
/// (2026-09-06): this replaces the old plain-text installed.sha256 marker (and avoids
/// adding a second, separate ad hoc text file for the active app directory) with one
/// place, rather than growing more scattered root-level files. Load() transparently
/// migrates an existing installed.sha256 from before this file existed.
/// </summary>
internal sealed class InstallState
{
    public string? InstalledSha256 { get; set; }
    public string ActiveAppDir { get; set; } = "app";

    /// <summary>Directory names (siblings of ActiveAppDir under AppPaths.Root) that used to
    /// be an active install and are no longer referenced by anything, but couldn't be
    /// deleted outright at swap time (a lingering lock -- see ExtractWithRetryAsync). Tracked
    /// explicitly here, rather than inferred by scanning Root for "app-* dirs that aren't the
    /// active one", so cleanup is driven by an auditable list instead of a naming-convention
    /// guess. Both this installer (a quick best-effort pass on every run) and the running
    /// Caroline app itself (which can retry for as long as it's alive) consume this list.</summary>
    public List<string> PendingDeletion { get; set; } = new();
}

internal static class InstallStateStore
{
    private static string StatePath => Path.Combine(AppPaths.Root, "state.json");
    private static string LegacySha256Path => Path.Combine(AppPaths.Root, "installed.sha256");

    public static InstallState Load()
    {
        if (File.Exists(StatePath))
        {
            try
            {
                var state = JsonSerializer.Deserialize<InstallState>(File.ReadAllText(StatePath));
                if (state != null) return state;
            }
            catch (Exception ex)
            {
                Logger.Log($"InstallStateStore: {StatePath} unreadable/corrupt, falling back to defaults (and any legacy migration below): {ex.Message}");
            }
        }

        // Migration: an install from before this file existed only has the old plain-text
        // marker. Carrying its value forward means a same-version re-run still correctly
        // skips re-downloading instead of looking "never installed" and fetching again.
        var migrated = new InstallState();
        if (File.Exists(LegacySha256Path))
        {
            try
            {
                migrated.InstalledSha256 = File.ReadAllText(LegacySha256Path).Trim();
                Logger.Log($"InstallStateStore: migrated installedSha256 from legacy {LegacySha256Path} into {StatePath}");
            }
            catch (Exception ex)
            {
                Logger.Log($"InstallStateStore: failed to read legacy {LegacySha256Path} during migration (starting fresh): {ex.Message}");
            }
        }
        return migrated;
    }

    public static void Save(InstallState state)
    {
        Directory.CreateDirectory(AppPaths.Root);
        var json = JsonSerializer.Serialize(state, new JsonSerializerOptions { WriteIndented = true });
        var tmpPath = StatePath + ".tmp-" + Guid.NewGuid().ToString("N");
        File.WriteAllText(tmpPath, json);
        File.Move(tmpPath, StatePath, overwrite: true);

        // Best-effort: once the unified file exists and is authoritative, the old
        // plain-text marker is dead weight -- not fatal to leave behind if this fails.
        try { File.Delete(LegacySha256Path); }
        catch (Exception ex)
        {
            Logger.Log($"InstallStateStore: cleanup of legacy {LegacySha256Path} failed (ignored, harmless leftover): {ex.Message}");
        }
    }

    /// <summary>One pass over every directory listed in PendingDeletion: deletes what it can,
    /// drops successfully-deleted entries from the list, and saves if anything changed.
    /// Never throws -- a directory that's still locked just stays in the list for the next
    /// caller (this installer's next run, or Caroline's own periodic retry) to try again.</summary>
    public static void TryCleanupPending(InstallState state)
    {
        if (state.PendingDeletion.Count == 0) return;
        var stillPending = new List<string>();
        foreach (var dirName in state.PendingDeletion)
        {
            var path = Path.Combine(AppPaths.Root, dirName);
            if (!Directory.Exists(path))
            {
                Logger.Log($"InstallStateStore.TryCleanupPending: {path} already gone");
                continue;
            }
            try
            {
                Directory.Delete(path, recursive: true);
                Logger.Log($"InstallStateStore.TryCleanupPending: removed {path}");
            }
            catch (Exception ex)
            {
                Logger.Log($"InstallStateStore.TryCleanupPending: {path} still not removable (will retry later): {ex.Message}");
                // Same diagnostic used during the extraction swap itself -- an empty result
                // here doesn't mean "nothing is wrong", it means the lock is on the directory
                // node itself, which this file-based check can't see (confirmed live, 2026-09-06).
                try
                {
                    var files = Directory.GetFiles(path, "*", SearchOption.AllDirectories);
                    var lockers = RestartManagerHelper.WhoIsLocking(files);
                    Logger.Log(lockers.Count > 0
                        ? $"InstallStateStore.TryCleanupPending: {path} locked by: {string.Join(", ", lockers)}"
                        : $"InstallStateStore.TryCleanupPending: Restart Manager found no process holding any of {files.Length} file(s) in {path} locked (the lock is most likely on the directory node itself, e.g. a transient AV/indexer scan)");
                }
                catch (Exception diagEx)
                {
                    Logger.Log($"InstallStateStore.TryCleanupPending: lock diagnostic for {path} itself failed (ignored): {diagEx.Message}");
                }
                stillPending.Add(dirName);
            }
        }
        if (stillPending.Count != state.PendingDeletion.Count)
        {
            state.PendingDeletion = stillPending;
            Save(state);
        }
    }
}
