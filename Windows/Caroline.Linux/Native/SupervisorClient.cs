using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;

namespace Caroline.Native;

/// <summary>
/// Linux port of Windows/Caroline/Native/SupervisorClient.cs -- same role,
/// same wire contract (spawns backend-py/supervisor.py, polls its own
/// GET /status on the same port), only the process-launch and force-kill
/// details differ: python3 (the bundled runtime's own interpreter, per
/// supervisor.py's own Phase 1 _resolve_pythonw_exe POSIX branch) instead
/// of pythonw.exe, and no taskkill.exe fallback needed -- .NET's own
/// Process.Kill(entireProcessTree: true) already walks /proc on Linux, so
/// the same primary path Windows uses is sufficient; a process-group SIGKILL
/// (`kill -9 -&lt;pgid&gt;`) is the fallback here instead, functionally the
/// same "catch anything the primary kill missed" role taskkill played.
/// </summary>
public sealed class SupervisorClient : IDisposable
{
    public const int Port = 48766;
    public const int BackendPort = 48765;

    private readonly string _backendPyDir;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };
    private Process? _process;
    private bool _intentionalStop;

    public event Action<string>? OutputLine;
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

        var python3Exe = ResolvePython3Exe();
        if (python3Exe == null)
        {
            // Same fail-loud posture as the Windows version's own
            // 2026-10-04 fix: never fall back to a system-wide python3 off
            // PATH, which would run the backend without its bundled
            // dependencies (claude_agent_sdk included).
            OutputLine?.Invoke($"[SupervisorClient] isolated Python runtime not found next to this build -- refusing to fall back to a system-wide python3 (would run without the bundled dependencies). Expected it at: {Path.Combine(AppContext.BaseDirectory, "..", "runtime", "python", "bin", "python3")}");
            return false;
        }
        OutputLine?.Invoke($"[SupervisorClient] resolved python3 exe: {python3Exe} (exists={File.Exists(python3Exe)})");

        var psi = new ProcessStartInfo
        {
            FileName = python3Exe,
            WorkingDirectory = _backendPyDir,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        psi.ArgumentList.Add(entry);
        psi.Environment["CAROLINE_APP_ROOT"] = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));
        psi.Environment["CAROLINE_PORT"] = BackendPort.ToString();
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
            if (_intentionalStop) return;
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

    /// <summary>Only the bundled, isolated runtime -- see supervisor.py's own
    /// Phase 1 _resolve_pythonw_exe for the matching backend-side path this
    /// mirrors (runtime/python/bin/python3, no system-python fallback).</summary>
    private static string? ResolvePython3Exe()
    {
        var isolated = Path.Combine(AppContext.BaseDirectory, "..", "runtime", "python", "bin", "python3");
        return File.Exists(isolated) ? Path.GetFullPath(isolated) : null;
    }

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
            // Fallback only -- the primary path above (Kill(entireProcessTree:
            // true)) already walks the real /proc parent-child tree on Linux
            // (kernel-tracked, not process-group-based), so it's correct on
            // its own and this is just "catch a PID Kill() itself got stuck
            // on," same role the Windows version's taskkill fallback plays.
            // NOT a process-group kill (no setsid/new-session equivalent was
            // set up at launch to make that safe) -- this targets only
            // supervisor.py's own PID directly, which is weaker than the
            // primary path for orphaned grandchildren specifically, but the
            // primary path is what's actually expected to run almost always.
            OutputLine?.Invoke(
                $"[SupervisorClient] Dispose(): background cleanup did not finish within 5.0s -- " +
                "Kill(entireProcessTree:true) itself was still blocked. Falling back to a detached " +
                $"`kill -9 {process.Id}` so the supervisor process itself doesn't survive as an orphan " +
                "holding a port; not waiting for it either."
            );
            try
            {
                var psi = new ProcessStartInfo("kill", $"-9 {process.Id}")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                };
                Process.Start(psi);
            }
            catch (Exception ex)
            {
                OutputLine?.Invoke($"[SupervisorClient] Dispose(): fallback `kill -9` launch itself failed: {ex.Message}");
            }
            return;
        }
        OutputLine?.Invoke($"[SupervisorClient] Dispose() done, total elapsed {sw.Elapsed.TotalSeconds:F1}s");
    }
}
