using System.IO;
using System.Text.Json;
using Caroline.Models;

namespace Caroline.Services;

public class SettingsService
{
    private static readonly string SettingsDir =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Caroline");

    private static readonly string SettingsPath = Path.Combine(SettingsDir, "settings.json");

    public AppSettings Load()
    {
        try
        {
            if (File.Exists(SettingsPath))
            {
                var json = File.ReadAllText(SettingsPath);
                var settings = JsonSerializer.Deserialize<AppSettings>(json);
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
        var json = JsonSerializer.Serialize(settings, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(SettingsPath, json);
    }
}
