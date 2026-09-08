using System.IO;
using Caroline.Services;

namespace Caroline.Native;

/// <summary>
/// Retries deleting old app-* install directories that CarolineInstaller's own extraction
/// swap couldn't remove at update time (see CarolineInstaller's InstallState.cs --
/// PendingDeletion is the same file, same list). Per explicit instruction (2026-09-06):
/// the short-lived installer process can only try a couple of times before it has to exit;
/// this long-lived process can keep retrying for as long as it takes for whatever
/// transient lock (an AV/indexer scan, typically) to clear. Only ever touches entries
/// explicitly listed here -- never guesses by scanning for "app-* dirs that look old".
/// </summary>
internal static class StaleInstallCleanup
{
    public static void TryCleanupPending()
    {
        var state = InstallStateStore.Load();
        if (state.PendingDeletion.Count == 0) return;

        var stillPending = new List<string>();
        foreach (var dirName in state.PendingDeletion)
        {
            var path = Path.Combine(InstallStateStore.Root, dirName);
            if (!Directory.Exists(path))
            {
                Logger.Log($"StaleInstallCleanup: {path} already gone");
                continue;
            }
            try
            {
                Directory.Delete(path, recursive: true);
                Logger.Log($"StaleInstallCleanup: removed {path}");
            }
            catch (Exception ex)
            {
                Logger.Log($"StaleInstallCleanup: {path} still not removable (will retry later): {ex.Message}");
                stillPending.Add(dirName);
            }
        }
        if (stillPending.Count != state.PendingDeletion.Count)
        {
            state.PendingDeletion = stillPending;
            InstallStateStore.Save(state);
        }
    }
}
