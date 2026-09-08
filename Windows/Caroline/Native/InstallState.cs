using System.IO;
using System.Text.Json;
using Caroline.Services;

namespace Caroline.Native;

/// <summary>
/// Read/write mirror of CarolineInstaller's own InstallState.cs -- same JSON shape, same
/// file (%LocalAppData%\Caroline\state.json) -- kept as a small duplicate here rather than
/// a shared assembly between the two separately-published projects (same pattern as e.g.
/// Autostart.cs's own AppBrowserHostPort literal, not shared code). Caroline itself only
/// ever reads InstalledSha256 (see UpdateChecker) and reads+retries PendingDeletion (see
/// StaleInstallCleanup) -- it never writes ActiveAppDir; only the installer decides that.
/// </summary>
internal sealed class InstallState
{
    public string? InstalledSha256 { get; set; }
    public string ActiveAppDir { get; set; } = "app";
    public List<string> PendingDeletion { get; set; } = new();
}

internal static class InstallStateStore
{
    public static string Root { get; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Caroline");

    private static string StatePath => Path.Combine(Root, "state.json");

    public static InstallState Load()
    {
        if (!File.Exists(StatePath)) return new InstallState();
        try
        {
            return JsonSerializer.Deserialize<InstallState>(File.ReadAllText(StatePath)) ?? new InstallState();
        }
        catch (Exception ex)
        {
            Logger.Log($"InstallStateStore: {StatePath} unreadable/corrupt (treating as defaults): {ex.Message}");
            return new InstallState();
        }
    }

    public static void Save(InstallState state)
    {
        try
        {
            var json = JsonSerializer.Serialize(state, new JsonSerializerOptions { WriteIndented = true });
            var tmpPath = StatePath + ".tmp-" + Guid.NewGuid().ToString("N");
            File.WriteAllText(tmpPath, json);
            File.Move(tmpPath, StatePath, overwrite: true);
        }
        catch (Exception ex)
        {
            Logger.Log($"InstallStateStore: failed to save {StatePath} (ignored): {ex.Message}");
        }
    }
}
