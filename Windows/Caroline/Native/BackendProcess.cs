using System.Diagnostics;
using System.IO;

namespace Caroline.Native;

/// <summary>
/// Spawns and supervises the Node backend sidecar (Caroline/backend, shipped
/// as a "backend" folder next to this exe -- see Caroline/README.md). Mirrors
/// the shape of Ratatosk's SncManager: a child process this window owns the
/// whole lifetime of, started on load and killed on exit.
/// </summary>
public sealed class BackendProcess : IDisposable
{
    public const int Port = 8765;

    private readonly string _backendDir;
    private Process? _process;
    private bool _intentionalStop;

    public event Action<string>? OutputLine;
    /// <summary>Fired when the backend process exits on its own (crash, uncaught exception killing the whole
    /// node process) -- never fired for a deliberate Dispose()/shutdown. See MainWindow for the restart policy.</summary>
    public event Action? Crashed;

    public BackendProcess()
    {
        _backendDir = Path.Combine(AppContext.BaseDirectory, "backend");
    }

    public bool Start()
    {
        var sw = System.Diagnostics.Stopwatch.StartNew();
        OutputLine?.Invoke($"[BackendProcess] Start() entered (thread={Environment.CurrentManagedThreadId})");
        var entry = Path.Combine(_backendDir, "dist", "server.js");
        if (!File.Exists(entry))
        {
            OutputLine?.Invoke($"[BackendProcess] not found: {entry}");
            return false;
        }

        var nodeExe = ResolveNodeExe();
        OutputLine?.Invoke($"[BackendProcess] resolved node exe: {nodeExe} (exists={File.Exists(nodeExe)})");

        var psi = new ProcessStartInfo
        {
            FileName = nodeExe,
            Arguments = $"\"{entry}\"",
            WorkingDirectory = _backendDir,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        // Visual Mode's talking-head models (see visualMode.ts's resolveVisualModel) --
        // a SIBLING of AppDir (CarolineInstaller.AppPaths' layout: Root\app\ + Root\art\models\),
        // never inside it, because AppDir gets fully deleted and recreated on every
        // app-zip update (see Program.cs's extraction step) and these are tens of GB
        // each -- re-downloading them on every routine update would be unacceptable.
        // Always set, even if the directory doesn't exist yet (dev tree without models
        // installed, or a build predating this feature) -- visualMode.ts's own fallback
        // to a dev-relative guess only kicks in when this path doesn't actually resolve.
        psi.Environment["CAROLINE_MODELS_DIR"] = Path.Combine(AppContext.BaseDirectory, "..", "art", "models");

        // Same sibling-of-AppDir reasoning as CAROLINE_MODELS_DIR just above -- the bundled
        // ffmpeg.exe lives at Root\runtime\ffmpeg\ffmpeg.exe (CarolineInstaller.AppPaths.FfmpegExe),
        // a fixed location regardless of which app-<hash> directory is currently active (see
        // CarolineInstaller.Program's per-install-directory extraction). Read by the backend's
        // own voice pipeline AND inherited automatically by every shared MCP server it spawns as
        // a plain child process (see sharedMcpServers.ts -- no env override there), so this one
        // line covers all of them, not just the backend itself.
        psi.Environment["CAROLINE_FFMPEG_PATH"] = Path.Combine(AppContext.BaseDirectory, "..", "runtime", "ffmpeg", "ffmpeg.exe");

        // Same reasoning again -- the embeddable Python runtime (CarolineInstaller.
        // AppPaths.PythonExe) lives at Root\runtime\python\python.exe, backs the local
        // edge-tts server (see localTtsServer.ts) this process spawns as its own child.
        psi.Environment["CAROLINE_PYTHON_PATH"] = Path.Combine(AppContext.BaseDirectory, "..", "runtime", "python", "python.exe");

        OutputLine?.Invoke($"[BackendProcess] calling Process.Start() (elapsed so far: {sw.Elapsed.TotalSeconds:F1}s)...");
        try
        {
            _process = Process.Start(psi);
        }
        catch (Exception ex)
        {
            OutputLine?.Invoke($"[BackendProcess] Process.Start() threw after {sw.Elapsed.TotalSeconds:F1}s (is Node.js installed?): {ex}");
            return false;
        }
        OutputLine?.Invoke($"[BackendProcess] Process.Start() returned after {sw.Elapsed.TotalSeconds:F1}s, pid={_process?.Id.ToString() ?? "null"}");

        if (_process == null) return false;

        _intentionalStop = false;
        _process.EnableRaisingEvents = true;
        _process.Exited += (_, _) =>
        {
            if (_intentionalStop) return; // Dispose()'s own Kill() -- not a crash
            OutputLine?.Invoke($"[BackendProcess] backend process exited unexpectedly (code {SafeExitCode()})");
            Crashed?.Invoke();
        };
        _process.OutputDataReceived += (_, e) => { if (e.Data != null) OutputLine?.Invoke(e.Data); };
        _process.ErrorDataReceived += (_, e) => { if (e.Data != null) OutputLine?.Invoke(e.Data); };
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();
        OutputLine?.Invoke($"[BackendProcess] Start() returning true, total elapsed {sw.Elapsed.TotalSeconds:F1}s");
        return true;
    }

    private int? SafeExitCode()
    {
        try { return _process?.ExitCode; }
        catch (Exception ex)
        {
            OutputLine?.Invoke($"[BackendProcess] SafeExitCode: reading ExitCode threw (reporting unknown): {ex.Message}");
            return null;
        }
    }

    /// <summary>
    /// CarolineInstaller provisions an isolated Node.js at
    /// %LocalAppData%\Caroline\runtime\node\node.exe (a sibling of this
    /// exe's own "app" install folder) precisely so Caroline never depends
    /// on -- or fights with -- a Node.js the machine happens to already
    /// have. Prefer that; fall back to "node" on PATH for dev/debug runs
    /// where the installer was never involved (e.g. `dotnet build` straight
    /// from the source tree).
    /// </summary>
    private static string ResolveNodeExe()
    {
        var isolated = Path.Combine(AppContext.BaseDirectory, "..", "runtime", "node", "node.exe");
        return File.Exists(isolated) ? Path.GetFullPath(isolated) : "node";
    }

    public void Dispose()
    {
        OutputLine?.Invoke($"[BackendProcess] Dispose() entered (thread={Environment.CurrentManagedThreadId}, _process={(_process == null ? "null" : $"pid={_process.Id}")})");
        if (_process == null)
        {
            OutputLine?.Invoke("[BackendProcess] Dispose(): _process was already null, nothing to do");
            return;
        }
        _intentionalStop = true;
        var sw = System.Diagnostics.Stopwatch.StartNew();
        try
        {
            var hasExited = _process.HasExited;
            OutputLine?.Invoke($"[BackendProcess] Dispose(): HasExited={hasExited}");
            if (!hasExited)
            {
                OutputLine?.Invoke("[BackendProcess] Dispose(): calling Kill(entireProcessTree:true)...");
                _process.Kill(entireProcessTree: true);
                OutputLine?.Invoke($"[BackendProcess] Dispose(): Kill() returned after {sw.Elapsed.TotalSeconds:F1}s");
            }
        }
        catch (Exception ex)
        {
            OutputLine?.Invoke($"[BackendProcess] Dispose(): Kill() threw after {sw.Elapsed.TotalSeconds:F1}s (process may have already exited): {ex}");
        }
        _process.Dispose();
        _process = null;
        OutputLine?.Invoke($"[BackendProcess] Dispose() done, total elapsed {sw.Elapsed.TotalSeconds:F1}s");
    }
}
