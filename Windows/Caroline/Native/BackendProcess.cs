using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;

namespace Caroline.Native;

/// <summary>
/// Spawns and supervises the Python backend sidecar (Caroline/backend-py,
/// shipped as a "backend-py" folder next to this exe -- see the migration
/// plan at C:\Users\khait\.claude\plans\foamy-sniffing-pixel.md). Mirrors
/// the shape of Ratatosk's SncManager: a child process this window owns the
/// whole lifetime of, started on load and killed on exit.
///
/// Cutover from the Node backend (2026-09-09): this class no longer launches
/// Node at all (the old ResolveNodeExe/dist/server.js path is gone) -- the
/// Node "backend" folder still ships in the packaged output for now (see the
/// Makefile) purely as a git-revert-this-file rollback, not a live fallback.
/// </summary>
public sealed class BackendProcess : IDisposable
{
    // Moved off 8765 (2026-09-13, per explicit instruction) to 48765 -- a
    // dedicated, unlikely-to-collide port, rather than the low/common 8765
    // several other unrelated apps also default to.
    public const int Port = 48765;

    private readonly string _backendDir;
    private Process? _process;
    private bool _intentionalStop;

    public event Action<string>? OutputLine;
    /// <summary>Fired when the backend process exits on its own (crash, uncaught exception killing the whole
    /// python process) -- never fired for a deliberate Dispose()/shutdown. See MainWindow for the restart policy.</summary>
    public event Action? Crashed;

    public BackendProcess()
    {
        _backendDir = Path.Combine(AppContext.BaseDirectory, "backend-py");
    }

    public bool Start()
    {
        var sw = System.Diagnostics.Stopwatch.StartNew();
        OutputLine?.Invoke($"[BackendProcess] Start() entered (thread={Environment.CurrentManagedThreadId})");
        var entry = Path.Combine(_backendDir, "run_server.py");
        if (!File.Exists(entry))
        {
            OutputLine?.Invoke($"[BackendProcess] not found: {entry}");
            return false;
        }

        var pythonwExe = ResolvePythonwExe();
        OutputLine?.Invoke($"[BackendProcess] resolved pythonw exe: {pythonwExe} (exists={File.Exists(pythonwExe)})");

        var psi = new ProcessStartInfo
        {
            FileName = pythonwExe,
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
        // AppPaths.PythonExe) lives at Root\runtime\python\python.exe, i.e. this IS the
        // interpreter this process itself now runs under via pythonwExe below (a
        // sibling in the same directory). Still passed through explicitly: the backend's
        // own local_tts_launcher.py reads this exact env var to spawn the separate local
        // edge-tts server subprocess, matching the original's own convention rather than
        // hardcoding sys.executable there.
        psi.Environment["CAROLINE_PYTHON_PATH"] = Path.Combine(AppContext.BaseDirectory, "..", "runtime", "python", "python.exe");

        // Per explicit instruction (2026-09-13): Port above is the ONE place
        // the port number is defined -- every other C# call site already
        // reads BackendProcess.Port rather than a literal (see App.xaml.cs/
        // MainWindow.xaml.cs). The Python side previously had to duplicate
        // the same literal by hand as its own os.environ default (main.py's
        // PORT) with nothing keeping the two in sync -- passing it explicitly
        // here closes that gap; main.py's own literal now only matters as a
        // fallback for someone running backend-py directly, outside this launcher.
        psi.Environment["CAROLINE_PORT"] = Port.ToString();

        OutputLine?.Invoke($"[BackendProcess] calling Process.Start() (elapsed so far: {sw.Elapsed.TotalSeconds:F1}s)...");
        try
        {
            _process = Process.Start(psi);
        }
        catch (Exception ex)
        {
            OutputLine?.Invoke($"[BackendProcess] Process.Start() threw after {sw.Elapsed.TotalSeconds:F1}s (is the isolated Python runtime installed?): {ex}");
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
    /// CarolineInstaller provisions an isolated embeddable Python at
    /// %LocalAppData%\Caroline\runtime\python\ (a sibling of this exe's own
    /// "app" install folder) precisely so Caroline never depends on -- or
    /// fights with -- a Python the machine happens to already have. Prefer
    /// pythonw.exe there (a sibling of the installer's own python.exe, same
    /// embeddable distribution ships both); fall back to "pythonw" on PATH
    /// for dev/debug runs where the installer was never involved (e.g.
    /// `dotnet build` straight from the source tree).
    ///
    /// pythonw.exe specifically, not python.exe: it's compiled as a
    /// GUI-subsystem executable and never allocates a console window under
    /// any circumstances -- a stronger, simpler guarantee than relying on
    /// CreateNoWindow/UseShellExecute alone. Confirmed live: launching via
    /// pythonw.exe with RedirectStandardOutput/RedirectStandardError still
    /// captures stdout/stderr exactly like python.exe does -- no loss of
    /// the logging pipe into OutputLine above.
    /// </summary>
    private static string ResolvePythonwExe()
    {
        var isolated = Path.Combine(AppContext.BaseDirectory, "..", "runtime", "python", "pythonw.exe");
        return File.Exists(isolated) ? Path.GetFullPath(isolated) : "pythonw";
    }

    /// <summary>
    /// Bug fix (2026-09-11), per a real live incident: this used to do the Kill()+Dispose()
    /// work inline and could hang INDEFINITELY -- confirmed live, 40+ minutes, freezing the
    /// entire WPF window (not just the backend) because MainWindow.RestartBackend called this
    /// synchronously on the UI thread. Two known System.Diagnostics.Process gotchas can cause
    /// this: (a) a surviving grandchild process that inherited the redirected stdout/stderr
    /// pipe handle keeps that pipe's write end open, so the async output reader never sees
    /// EOF and Process.Dispose() waits on it forever; (b) calling Dispose()/Kill() reentrantly
    /// from within the SAME Process's own Exited callback (which is exactly what happened here
    /// -- Crashed fired from Process.Exited, MainWindow used to marshal onto the UI thread with
    /// a BLOCKING Dispatcher.Invoke, and RestartBackend's Dispose() call landed back on that
    /// same Process object while its Exited machinery was still "in progress") deadlocks on
    /// Process's internal wait-handle unregistration. (b) is fixed at the call site too
    /// (MainWindow now uses Dispatcher.BeginInvoke for Crashed/Frozen, so RestartBackend never
    /// runs nested inside Process's own callback) -- the bound below stays regardless, as a
    /// backstop: Dispose() must never again be able to hang its caller, whatever future
    /// Process-internal edge case might cause it.
    /// </summary>
    public void Dispose()
    {
        OutputLine?.Invoke($"[BackendProcess] Dispose() entered (thread={Environment.CurrentManagedThreadId}, _process={(_process == null ? "null" : $"pid={_process.Id}")})");
        if (_process == null)
        {
            OutputLine?.Invoke("[BackendProcess] Dispose(): _process was already null, nothing to do");
            return;
        }
        _intentionalStop = true;
        var process = _process;
        _process = null; // detach immediately -- Start() can be called again right after this
                          // method returns, even if the background cleanup below is still stuck.
        var sw = System.Diagnostics.Stopwatch.StartNew();
        var cleanupTask = Task.Run(() =>
        {
            try
            {
                var hasExited = process.HasExited;
                OutputLine?.Invoke($"[BackendProcess] Dispose(): HasExited={hasExited}");
                if (!hasExited)
                {
                    OutputLine?.Invoke("[BackendProcess] Dispose(): calling Kill(entireProcessTree:true)...");
                    process.Kill(entireProcessTree: true);
                    OutputLine?.Invoke($"[BackendProcess] Dispose(): Kill() returned after {sw.Elapsed.TotalSeconds:F1}s");
                }
            }
            catch (Exception ex)
            {
                OutputLine?.Invoke($"[BackendProcess] Dispose(): Kill() threw after {sw.Elapsed.TotalSeconds:F1}s (process may have already exited): {ex}");
            }
            // Stop the async redirected-output reads explicitly before Dispose() -- if a
            // surviving grandchild is holding the pipe open, this at least gives the BCL a
            // clean "we're done listening" signal instead of relying on EOF alone. Harmless
            // (and expected to throw/no-op) if reading was never active or already stopped.
            try { process.CancelOutputRead(); } catch { /* not reading, or already stopped */ }
            try { process.CancelErrorRead(); } catch { /* not reading, or already stopped */ }
            process.Dispose();
        });
        if (!cleanupTask.Wait(TimeSpan.FromSeconds(5)))
        {
            OutputLine?.Invoke(
                $"[BackendProcess] Dispose(): background cleanup did not finish within 5.0s -- abandoning it " +
                "and returning anyway (the Process object leaks; that's a much smaller problem than blocking " +
                "the caller forever, which is what happened live before this fix)."
            );
            return;
        }
        OutputLine?.Invoke($"[BackendProcess] Dispose() done, total elapsed {sw.Elapsed.TotalSeconds:F1}s");
    }
}
