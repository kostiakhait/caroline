using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Windows.Threading;
using Caroline.Native;

namespace Caroline.Services;

/// <summary>
/// The Windows end of the update (docs/UPDATE_EVERYWHERE_PLAN.md, "The rule"). The looking, the downloading and the checking of the signature and of the
/// sha256 are the backend's (backend-py/app/update_service.py, one code for every platform); this class only asks it what to do and does the one
/// thing only Windows can: start the installer and let Caroline exit.
///
///   action "install"  Caroline is inactive (or the owner said yes): the installer starts at once, invisibly (--silent-update) when nobody asked,
///                     with its own progress window when the owner chose "Update to ..." in the tray.
///   action "ask"      Caroline is in use: no dialog, nothing that breaks the work. The tray item "Update to {version}" and a line in the status bar of
///                     every tab; the owner's click confirms. When she becomes inactive the update installs by itself, answered or not.
///
/// This replaces the once-per-launch MessageBox "Restart now to update?" of the earlier updater (it interrupted the work, which the rule forbids).
/// </summary>
public sealed class UpdateChecker
{
    private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(30);

    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(15) };
    private readonly DispatcherTimer _timer;
    private bool _polling;
    private bool _launched;
    private string? _lastStatusLine;
    private string? _offeredVersion;

    /// <summary>A verified update is waiting and the owner is to decide: the tray shows "Update to {version}". Fired on every poll while it waits.</summary>
    public event Action<string>? UpdateAvailable;

    /// <summary>A line for the status bar of every tab (download progress, "ready"); fired when the text changes.</summary>
    public event Action<string>? StatusChanged;

    public UpdateChecker()
    {
        _timer = new DispatcherTimer { Interval = PollInterval };
        _timer.Tick += async (_, _) => await PollAsync();
    }

    public void Start()
    {
        Logger.Log($"[UpdateChecker] starting -- asking the backend every {PollInterval.TotalSeconds:F0}s (the backend looks for releases itself)");
        _timer.Start();
    }

    private static string Url(string path) => $"http://127.0.0.1:{BackendProcess.Port}{path}";

    private async Task PollAsync()
    {
        if (_polling || _launched) return;
        _polling = true;
        try
        {
            using var resp = await _http.GetAsync(Url("/api/update/status"));
            if (!resp.IsSuccessStatusCode) return;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            await ActAsync(doc.RootElement);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException)
        {
            // The backend is not up yet (or is restarting): nothing to do, the next tick asks again.
        }
        catch (Exception ex)
        {
            Logger.Log($"[UpdateChecker] poll failed: {ex}");
        }
        finally { _polling = false; }
    }

    private async Task ActAsync(JsonElement s)
    {
        var status = s.GetProperty("status").GetString();
        var version = s.TryGetProperty("version", out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";
        if (status == "downloading")
        {
            var pct = s.TryGetProperty("progress", out var p) && p.ValueKind == JsonValueKind.Number ? p.GetInt32() : 0;
            Say($"Caroline: Update ongoing... {pct}%");
            return;
        }
        if (status != "ready") return;

        var action = s.GetProperty("action").GetString();
        var path = s.TryGetProperty("path", out var pa) && pa.ValueKind == JsonValueKind.String ? pa.GetString() : null;
        var confirmed = s.TryGetProperty("confirmed", out var c) && c.ValueKind == JsonValueKind.True;
        if (action == "install" && path is not null && File.Exists(path))
        {
            await LaunchInstallerAsync(path, version, silent: !confirmed);
            return;
        }
        if (action == "ask")
        {
            UpdateAvailable?.Invoke(version);
            if (_offeredVersion != version)
            {
                _offeredVersion = version;
                Logger.Log($"[UpdateChecker] update {version} is ready, Caroline is in use -- waiting for the owner or for her to become inactive");
            }
            Say($"Caroline: Update {version} ready -- installs when you are done, or choose \"Update to {version}\" in the tray");
        }
    }

    /// <summary>The owner chose "Update to ..." in the tray: that is the yes. If the update is not downloaded yet it installs, with its progress window, as soon as it is.</summary>
    public async Task UpdateNowAsync()
    {
        Logger.Log("[UpdateChecker] UpdateNowAsync: the owner confirmed");
        try
        {
            using var resp = await _http.PostAsync(Url("/api/update/confirm"), new StringContent("{}", Encoding.UTF8, "application/json"));
            Logger.Log($"[UpdateChecker] confirm -> {(int)resp.StatusCode}");
        }
        catch (Exception ex)
        {
            Logger.Log($"[UpdateChecker] confirm failed (the next poll asks again): {ex.Message}");
        }
        await PollAsync();
    }

    private async Task LaunchInstallerAsync(string path, string version, bool silent)
    {
        _launched = true;
        _timer.Stop();
        Logger.Log($"[UpdateChecker] launching {path} for {version} ({(silent ? "silent, Caroline was inactive" : "visible, the owner confirmed")})");
        try
        {
            using var resp = await _http.PostAsync(Url("/api/update/installing"), new StringContent("{}", Encoding.UTF8, "application/json"));
        }
        catch (Exception ex)
        {
            Logger.Log($"[UpdateChecker] could not tell the backend the install started (not fatal): {ex.Message}");
        }
        Process.Start(new ProcessStartInfo(path, silent ? "--silent-update" : "") { UseShellExecute = true });
        App.RequestShutdown($"update: installer {path} launched");
    }

    private void Say(string line)
    {
        if (line == _lastStatusLine) return;
        _lastStatusLine = line;
        StatusChanged?.Invoke(line);
    }
}
