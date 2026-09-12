using System.Diagnostics;
using Microsoft.Win32;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Caroline's own window (MainWindow.xaml.cs) is a WPF host around a
/// WebView2 control -- without the WebView2 Runtime present on the
/// machine, Caroline.exe either fails to show a window at all or crashes
/// on first launch, and the INSTALLER itself never sees any of that: it
/// doesn't touch WebView2 anywhere today, so it reports "success" and
/// exits regardless. The vast majority of real Windows 10/11 machines
/// already have it (bundled with Windows 11, or via Windows Update on
/// Windows 10 -- see Microsoft's own WebView2 distribution docs), but a
/// locked-down corporate image, LTSC build, or Windows Server box can
/// genuinely lack it -- exactly the kind of silent, hard-to-diagnose-
/// remotely gap that plausibly explains "it didn't install" reports with
/// no further detail.
///
/// Detection matches Microsoft's own documented "Detect if a WebView2
/// Runtime is already installed" registry check (see
/// learn.microsoft.com/microsoft-edge/webview2/concepts/distribution) --
/// the WOW6432Node HKLM path (per-machine, matches a per-machine Edge/
/// WebView2 install) plus the HKCU path (per-user install, e.g. one
/// installed by a non-admin user or by a different per-user app).
/// Install uses the small (~2MB) Evergreen Bootstrapper rather than
/// bundling the ~250MB Fixed Version runtime -- it downloads and installs
/// whichever Evergreen Runtime matches the machine's own architecture.
/// Not sha256-pinned like our other mirrored dependencies: Microsoft
/// itself documents that the bootstrapper's own bits change over time
/// under this same permanent link, since it always fetches the latest
/// Runtime -- a pinned hash would just break every future install once
/// Microsoft rotates it.
/// </summary>
internal static class WebView2Installer
{
    private const string BootstrapperUrl = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";
    private const string ClientId = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";

    public static bool IsInstalled()
    {
        return HasVersionAt(RegistryHive.LocalMachine, RegistryView.Registry64, $@"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{ClientId}")
            || HasVersionAt(RegistryHive.CurrentUser, RegistryView.Registry64, $@"Software\Microsoft\EdgeUpdate\Clients\{ClientId}");
    }

    private static bool HasVersionAt(RegistryHive hive, RegistryView view, string subKeyPath)
    {
        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, view);
            using var key = baseKey.OpenSubKey(subKeyPath);
            var version = key?.GetValue("pv") as string;
            return !string.IsNullOrEmpty(version) && version != "0.0.0.0";
        }
        catch (Exception ex)
        {
            Logger.Log($"WebView2Installer: registry check failed for {hive}\\{subKeyPath} (treating as not installed): {ex.Message}");
            return false;
        }
    }

    public static async Task InstallAsync(Downloader downloader, Action<string> onStatus, CancellationToken ct)
    {
        if (IsInstalled())
        {
            Logger.Log("WebView2Installer: WebView2 Runtime already present, skipping");
            return;
        }

        onStatus("Setting up Microsoft Edge WebView2 (required to show Caroline's window)…");
        var bootstrapperPath = Path.Combine(AppPaths.Root, "MicrosoftEdgeWebview2Setup.exe");
        await downloader.DownloadAsync(BootstrapperUrl, bootstrapperPath, expectedSha256Hex: null, _ => { }, ct);

        Logger.Log($"WebView2Installer: running bootstrapper silently: {bootstrapperPath}");
        using var process = Process.Start(new ProcessStartInfo(bootstrapperPath, "/silent /install")
        {
            UseShellExecute = false,
        });
        if (process is null)
        {
            throw new InvalidOperationException("Couldn't start the WebView2 Runtime bootstrapper (Process.Start returned null).");
        }
        await process.WaitForExitAsync(ct);
        Logger.Log($"WebView2Installer: bootstrapper exited with code {process.ExitCode}");

        try { File.Delete(bootstrapperPath); }
        catch (Exception ex) { Logger.Log($"WebView2Installer: couldn't delete bootstrapper (ignored, harmless leftover): {ex.Message}"); }

        // The bootstrapper's own exit code is the most direct signal (0 =
        // success); re-checking the registry catches the rare case where
        // it exits 0 but the Evergreen Updater hasn't finished writing the
        // key yet (fine either way, since a false negative here just means
        // one more install attempt, not a hard failure).
        if (process.ExitCode != 0 && !IsInstalled())
        {
            throw new InvalidOperationException(
                $"WebView2 Runtime install failed (bootstrapper exit code {process.ExitCode}). "
                + "Caroline needs it to display its own window.");
        }
        Logger.Log("WebView2Installer: WebView2 Runtime install verified");
    }
}
