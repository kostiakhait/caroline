using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using Caroline.Models;

namespace Caroline.Services;

public class SettingsService
{
    private static readonly string SettingsDir =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Caroline");

    private static readonly string SettingsPath = Path.Combine(SettingsDir, "settings.json");

    // Bug fix (2026-09-13, per explicit report from a real user's logs):
    // AppSettings.WindowLeft/Top/Width/Height deliberately default to
    // double.NaN as a "never customized yet" sentinel (see that model's own
    // comment) -- but System.Text.Json THROWS on NaN/Infinity by default
    // instead of writing them, so Save() below threw on every single call
    // until the user moved/resized the window at least once. That exception
    // propagated up into the app's global DispatcherUnhandledException
    // handler and was silently swallowed there, so OpenTabIds/TabNames
    // (persisted via this same Save() call) never reached disk either --
    // confirmed live: a user's open tabs and tab renames were never
    // surviving a restart, with no visible error. AllowNamedFloatingPointLiterals
    // makes System.Text.Json read/write NaN/Infinity as the literal JSON
    // strings "NaN"/"Infinity"/"-Infinity" instead of throwing, on both
    // sides so a file written by one version round-trips through the other.
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        NumberHandling = JsonNumberHandling.AllowNamedFloatingPointLiterals,
    };

    public AppSettings Load()
    {
        try
        {
            if (File.Exists(SettingsPath))
            {
                var json = File.ReadAllText(SettingsPath);
                var settings = JsonSerializer.Deserialize<AppSettings>(json, JsonOptions);
                if (settings != null) return settings;
            }
        }
        catch (IOException ex)
        {
            Logger.Log($"[SettingsService] Load: read of {SettingsPath} failed, falling back to defaults: {ex.Message}");
        }
        catch (JsonException ex)
        {
            Logger.Log($"[SettingsService] Load: {SettingsPath} is corrupt, falling back to defaults: {ex.Message}");
        }

        return new AppSettings();
    }

    public void Save(AppSettings settings)
    {
        Directory.CreateDirectory(SettingsDir);
        var json = JsonSerializer.Serialize(settings, JsonOptions);
        File.WriteAllText(SettingsPath, json);
    }
}
