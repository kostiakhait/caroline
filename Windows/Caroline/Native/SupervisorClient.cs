using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Caroline.Native;

/// <summary>
/// Thin client for backend-py/supervisor.py, a standalone Python process
/// this class spawns once and which then owns the REAL backend
/// (run_server.py)'s whole lifecycle -- spawn, health polling (whole-process
/// AND per-tab), rate-limited restart. Per explicit instruction (2026-09-27):
/// this replaces BackendProcess.cs (which used to spawn run_server.py
/// directly) and BackendHealthWatchdog.cs (external health polling) --
/// MainWindow no longer decides any of that itself, it just launches the
/// supervisor and polls its own small HTTP status for one thing only: has
/// it given up auto-restarting (needs a human)? See supervisor.py's own
/// module docstring for the full reasoning (decoupling process supervision
/// from this WPF-specific, Windows-only shell, so a future headless
/// launcher can drive the exact same HTTP surface).
///
/// The backend's OWN app port (BackendProcess.Port, 48765 -- chat.js's
/// WebSocket, App.xaml.cs's splash-dismiss poll, MainWindow's own /api/
/// control calls) is UNCHANGED and untouched by this class; only the
/// process-supervision responsibility moved.
/// </summary>
public sealed class SupervisorClient : IDisposable
{
    // Sibling of BackendProcess.Port (48765), AppControlHost.Port (8768, this
    // process's own control surface), and Caroline.NativeHost's own
    // AppBrowserHost.Port (8767, a separate process since 2026-10-03) --
    // this project's own port registry.
    public const int Port = 48766;

    private readonly string _backendPyDir;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };
    private Process? _process;
    private bool _intentionalStop;

    public event Action<string>? OutputLine;
    /// <summary>Fired when the SUPERVISOR process itself exits unexpectedly
    /// (not the backend it manages -- supervisor.py's own autonomous poll
    /// loop handles backend crashes/freezes without any help from here).
    /// If this fires, NOTHING is managing the backend anymore -- more
    /// serious than a plain backend crash, see MainWindow's handler.</summary>
    public event Action? Crashed;

    public SupervisorClient()
    {
        _backendPyDir = Path.Combine(AppContext.BaseDirectory, "backend-py");
    }

    public bool Start()
    {
        var sw = Stopwatch.StartNew();
        OutputLine?.Invoke($"[SupervisorClient] Start() entered (thread={Environment.CurrentManagedThreadId})");
        var entry = Path.Combine(_backendPyDir, "supervisor.py");
        if (!File.Exists(entry))
        {
            OutputLine?.Invoke($"[SupervisorClient] not found: {entry}");
            return false;
        }

        var pythonwExe = ResolvePythonwExe();
        OutputLine?.Invoke($"[SupervisorClient] resolved pythonw exe: {pythonwExe} (exists={File.Exists(pythonwExe)})");

        var psi = new ProcessStartInfo
        {
            FileName = pythonwExe,
            Arguments = $"\"{entry}\"",
            WorkingDirectory = _backendPyDir,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        // supervisor.py computes every sibling path (art/models, art/
        // whisper-model, runtime/ffmpeg, runtime/codex, runtime/python) from
        // its OWN file location by default (self-sufficient -- see its own
        // docstring) -- this override exists only so a dev tree where
        // supervisor.py isn't at its normal installed depth still resolves
        // correctly, same safety-net role BackendProcess.cs's own env vars
        // used to play for run_server.py directly.
        psi.Environment["CAROLINE_APP_ROOT"] = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));
        psi.Environment["CAROLINE_PORT"] = BackendProcess.Port.ToString();
        psi.Environment["CAROLINE_SUPERVISOR_PORT"] = Port.ToString();

        OutputLine?.Invoke($"[SupervisorClient] calling Process.Start() (elapsed so far: {sw.Elapsed.TotalSeconds:F1}s)...");
        try
        {
            _process = Process.Start(psi);
        }
        catch (Exception ex)
        {
            OutputLine?.Invoke($"[SupervisorClient] Process.Start() threw after {sw.Elapsed.TotalSeconds:F1}s (is the isolated Python runtime installed?): {ex}");
            return false;
        }
        OutputLine?.Invoke($"[SupervisorClient] Process.Start() returned after {sw.Elapsed.TotalSeconds:F1}s, pid={_process?.Id.ToString() ?? "null"}");

        if (_process == null) return false;

        _intentionalStop = false;
        _process.EnableRaisingEvents = true;
        _process.Exited += (_, _) =>
        {
            if (_intentionalStop) return; // Dispose()'s own Kill() -- not a crash
            OutputLine?.Invoke($"[SupervisorClient] supervisor process exited unexpectedly (code {SafeExitCode()})");
            Crashed?.Invoke();
        };
        _process.OutputDataReceived += (_, e) => { if (e.Data != null) OutputLine?.Invoke(e.Data); };
        _process.ErrorDataReceived += (_, e) => { if (e.Data != null) OutputLine?.Invoke(e.Data); };
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();
        OutputLine?.Invoke($"[SupervisorClient] Start() returning true, total elapsed {sw.Elapsed.TotalSeconds:F1}s");
        return true;
    }

    private int? SafeExitCode()
    {
        try { return _process?.ExitCode; }
        catch (Exception ex)
        {
            OutputLine?.Invoke($"[SupervisorClient] SafeExitCode: reading ExitCode threw (reporting unknown): {ex.Message}");
            return null;
        }
    }

    /// <summary>Same reasoning as BackendProcess.cs's own ResolvePythonwExe
    /// (this class deliberately duplicates rather than shares it -- see
    /// BackendProcess.cs's own doc comment for why it's now just a Port
    /// constant, not a class other code should still depend on).</summary>
    private static string ResolvePythonwExe()
    {
        var isolated = Path.Combine(AppContext.BaseDirectory, "..", "runtime", "python", "pythonw.exe");
        return File.Exists(isolated) ? Path.GetFullPath(isolated) : "pythonw";
    }

    /// <summary>Polls supervisor.py's own GET /status. Returns null (and
    /// logs) on any failure -- callers treat that the same as "nothing new
    /// to report" rather than a crash, since a momentary connection hiccup
    /// to a process this same class just spawned is not, on its own,
    /// evidence of anything wrong.</summary>
    public async Task<JsonElement?> GetStatusAsync()
    {
        try
        {
            using var resp = await _http.GetAsync($"http://127.0.0.1:{Port}/status");
            if (!resp.IsSuccessStatusCode) return null;
            var body = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(body);
            return doc.RootElement.Clone();
        }
        catch (Exception ex)
        {
            OutputLine?.Invoke($"[SupervisorClient] GetStatusAsync failed: {ex.Message}");
            return null;
        }
    }

    /// <summary>Same 5s-timeout-then-detached-taskkill-fallback shape as
    /// BackendProcess.cs's own Dispose() -- see its doc comment for the two
    /// real incidents (a surviving grandchild holding the output pipe open;
    /// reentrant Dispose() from within Process.Exited) this guards against.
    /// Killing this process's own tree also takes down the backend it
    /// spawned as a child, so nothing extra is needed here for that.</summary>
    public void Dispose()
    {
        OutputLine?.Invoke($"[SupervisorClient] Dispose() entered (thread={Environment.CurrentManagedThreadId}, _process={(_process == null ? "null" : $"pid={_process.Id}")})");
        _http.Dispose();
        if (_process == null)
        {
            OutputLine?.Invoke("[SupervisorClient] Dispose(): _process was already null, nothing to do");
            return;
        }
        _intentionalStop = true;
        var process = _process;
        _process = null;
        var sw = Stopwatch.StartNew();
        var cleanupTask = Task.Run(() =>
        {
            try
            {
                var hasExited = process.HasExited;
                OutputLine?.Invoke($"[SupervisorClient] Dispose(): HasExited={hasExited}");
                if (!hasExited)
                {
                    OutputLine?.Invoke("[SupervisorClient] Dispose(): calling Kill(entireProcessTree:true)...");
                    process.Kill(entireProcessTree: true);
                    OutputLine?.Invoke($"[SupervisorClient] Dispose(): Kill() returned after {sw.Elapsed.TotalSeconds:F1}s");
                }
            }
            catch (Exception ex)
            {
                OutputLine?.Invoke($"[SupervisorClient] Dispose(): Kill() threw after {sw.Elapsed.TotalSeconds:F1}s (process may have already exited): {ex}");
            }
            try { process.CancelOutputRead(); } catch { /* not reading, or already stopped */ }
            try { process.CancelErrorRead(); } catch { /* not reading, or already stopped */ }
            process.Dispose();
        });
        if (!cleanupTask.Wait(TimeSpan.FromSeconds(5)))
        {
            OutputLine?.Invoke(
                $"[SupervisorClient] Dispose(): background cleanup did not finish within 5.0s -- " +
                "Kill(entireProcessTree:true) itself was still blocked. Falling back to a detached " +
                $"`taskkill /F /T /PID {process.Id}` so neither the supervisor nor the backend it owns " +
                "survives as an orphan holding a port; not waiting for it either."
            );
            try
            {
                Process.Start(new ProcessStartInfo("taskkill.exe", $"/F /T /PID {process.Id}")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                });
            }
            catch (Exception ex)
            {
                OutputLine?.Invoke($"[SupervisorClient] Dispose(): fallback taskkill launch itself failed: {ex.Message}");
            }
            return;
        }
        OutputLine?.Invoke($"[SupervisorClient] Dispose() done, total elapsed {sw.Elapsed.TotalSeconds:F1}s");
    }
}
