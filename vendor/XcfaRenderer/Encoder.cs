using System.Diagnostics;

namespace XcfaRenderer;

/// <summary>
/// Pipes raw video frames to an external ffmpeg process, which encodes and
/// muxes them against the source audio track -- direct port of
/// _render_worker.py's _FfmpegPipeWriter/_FfmpegAlphaPipeWriter +
/// _open_ffmpeg_pipe/_open_ffmpeg_pipe_alpha. The final encode is (and stays)
/// an external ffmpeg process, not a library call, matching the Python
/// original -- ffmpeg is already a hard dependency for PCM decode
/// (see AudioFeatures.LoadPcm).
///
/// NVENC hardware encoding is opportunistic, same as the Python original:
/// probed once per (process, ffmpeg path) via a real 1-frame encode (an
/// encoder-list check isn't enough -- h264_nvenc is listed even without a
/// CUDA-capable GPU/driver present), then used for non-WebM output if
/// available. Direct port of _nvenc_available, minus its cross-run
/// Config.installed persistence layer: that's reforce's own settings store
/// (a Camerlengo-process concept), not something a standalone library can
/// reuse -- an in-memory per-process cache (matching _nvenc_cache, the
/// in-memory half of the original) is what's ported here.
/// </summary>
public sealed class Encoder : IDisposable
{
    private static readonly Dictionary<string, bool> NvencCache = new();

    /// <summary>True if ffmpeg can actually encode h264_nvenc right now (real probe, not just an encoder-list check). Cached per ffmpeg path for this process's lifetime.</summary>
    public static bool NvencAvailable(string ffmpeg = "ffmpeg")
    {
        if (NvencCache.TryGetValue(ffmpeg, out var cached)) return cached;

        bool ok;
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = ffmpeg,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            var args = psi.ArgumentList;
            args.Add("-hide_banner"); args.Add("-loglevel"); args.Add("error");
            // 320x240 -- safely above NVENC's minimum frame-size requirement (16x16 caused false negatives).
            args.Add("-f"); args.Add("lavfi"); args.Add("-i"); args.Add("nullsrc=s=320x240:d=0.04");
            args.Add("-c:v"); args.Add("h264_nvenc"); args.Add("-frames:v"); args.Add("1");
            args.Add("-f"); args.Add("null"); args.Add("-");

            using var proc = Process.Start(psi);
            ok = proc is not null && proc.WaitForExit(10_000) && proc.ExitCode == 0;
            if (proc is not null && !proc.HasExited) { try { proc.Kill(); } catch { /* best effort */ } ok = false; }
        }
        catch
        {
            ok = false;
        }

        NvencCache[ffmpeg] = ok;
        return ok;
    }

    /// <summary>
    /// Optional hook a host app (Caroline's VisualModeManager) can set once to receive this
    /// encoder's captured ffmpeg stderr after every Close(), success or failure -- this
    /// library has no logger of its own (standalone, no dependency on the host app), and
    /// with ffmpeg now launched with CreateNoWindow=true (see Open()), its diagnostic
    /// output would otherwise be completely invisible instead of just an unwanted console
    /// window. Per explicit instruction: log everything relevant to animation rendering,
    /// not only failures.
    /// </summary>
    public static Action<string>? LogSink;

    private readonly Process _proc;
    private readonly bool _alpha;
    private readonly System.Text.StringBuilder _stderr = new();
    private bool _closed;

    // Producer/consumer split between frame computation (Renderer.cs's caller, on
    // its own thread) and the actual write to ffmpeg's stdin pipe (this background
    // task). Confirmed live (2026-09-03) as the real reason a single reply's render
    // took 30-40s on a fast machine: WriteFrame used to write straight to the pipe
    // SYNCHRONOUSLY on the caller's own thread, so whenever ffmpeg's own encode
    // throughput lagged even slightly, the NEXT frame's pixel math (warp/composite/
    // unsharp -- real CPU work) sat idle waiting for a pipe write to unblock, instead
    // of running concurrently with ffmpeg's encoding. Bounded to a modest number of
    // frames so a genuinely stuck/dead ffmpeg still applies backpressure (WriteFrame
    // blocks) rather than buffering unboundedly in memory.
    private readonly System.Collections.Concurrent.BlockingCollection<byte[]> _writeQueue = new(boundedCapacity: 16);
    private readonly Task _writerTask;
    private volatile bool _writerFailed;

    private Encoder(Process proc, bool alpha)
    {
        _proc = proc;
        _alpha = alpha;
        proc.ErrorDataReceived += (_, e) => { if (e.Data != null) lock (_stderr) _stderr.AppendLine(e.Data); };
        proc.BeginErrorReadLine();
        _writerTask = Task.Run(WriterLoop);
    }

    private void WriterLoop()
    {
        foreach (var pixels in _writeQueue.GetConsumingEnumerable())
        {
            try
            {
                _proc.StandardInput.BaseStream.Write(pixels, 0, pixels.Length);
            }
            catch (IOException)
            {
                // Pipe broke (ffmpeg exited/crashed) -- stop trying to write more;
                // Close() below will still observe the real exit code and stderr.
                _writerFailed = true;
                break;
            }
        }
    }

    /// <summary>
    /// Opens an ffmpeg pipe. Output codec is chosen by outputPath's extension:
    /// ".webm" -> VP9 (+ alpha channel if useAlpha), everything else -> H.264/AAC (no alpha).
    /// </summary>
    public static Encoder Open(int width, int height, int fps, string audioPath, string outputPath,
        bool useAlpha = false, string ffmpeg = "ffmpeg", string? videoBitrate = null)
    {
        var isWebm = outputPath.EndsWith(".webm", StringComparison.OrdinalIgnoreCase);
        var alpha = useAlpha && isWebm;

        var dir = Path.GetDirectoryName(Path.GetFullPath(outputPath));
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);

        var psi = new ProcessStartInfo
        {
            FileName = ffmpeg,
            RedirectStandardInput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        var args = psi.ArgumentList;
        args.Add("-y");
        args.Add("-f"); args.Add("rawvideo");
        args.Add("-pix_fmt"); args.Add(alpha ? "bgra" : "bgr24");
        args.Add("-s"); args.Add($"{width}x{height}");
        args.Add("-r"); args.Add(fps.ToString());
        args.Add("-i"); args.Add("pipe:0");
        args.Add("-i"); args.Add(audioPath);

        if (isWebm)
        {
            args.Add("-c:v"); args.Add("libvpx-vp9");
            args.Add("-deadline"); args.Add("realtime");
            args.Add("-cpu-used"); args.Add("8");
            if (videoBitrate is not null) { args.Add("-b:v"); args.Add(videoBitrate); }
            else { args.Add("-crf"); args.Add("30"); args.Add("-b:v"); args.Add("0"); }
            if (alpha)
            {
                args.Add("-pix_fmt"); args.Add("yuva420p");
            }
            else
            {
                args.Add("-pix_fmt"); args.Add("yuv420p");
            }
            args.Add("-c:a"); args.Add("libopus");
            args.Add("-b:a"); args.Add("128k");
        }
        else
        {
            if (NvencAvailable(ffmpeg))
            {
                args.Add("-c:v"); args.Add("h264_nvenc");
                args.Add("-preset"); args.Add("p4");
                args.Add("-rc"); args.Add("vbr");
                if (videoBitrate is not null)
                {
                    args.Add("-b:v"); args.Add(videoBitrate);
                    args.Add("-maxrate"); args.Add(videoBitrate);
                }
                else
                {
                    args.Add("-cq"); args.Add("22");
                }
            }
            else
            {
                args.Add("-c:v"); args.Add("libx264");
                if (videoBitrate is not null) { args.Add("-b:v"); args.Add(videoBitrate); }
                else { args.Add("-crf"); args.Add("22"); }
                args.Add("-preset"); args.Add("fast");
            }
            args.Add("-pix_fmt"); args.Add("yuv420p");
            args.Add("-c:a"); args.Add("aac");
            args.Add("-b:a"); args.Add("128k");
            args.Add("-movflags"); args.Add("+faststart");
        }
        args.Add("-shortest");
        args.Add(outputPath);

        var proc = Process.Start(psi) ?? throw new InvalidOperationException("Failed to start ffmpeg.");
        ProcessWatchdog.Register(proc);
        return new Encoder(proc, alpha);
    }

    /// <summary>Writes one frame: BGR24 (width*height*3 bytes) normally, or BGRA (width*height*4) when opened with useAlpha.
    /// Enqueues onto the background writer (see WriterLoop) instead of writing to the pipe
    /// directly -- returns as soon as there's queue room, not once ffmpeg has actually
    /// consumed the frame, so the caller's own pixel computation for the NEXT frame can run
    /// concurrently with this one being encoded.</summary>
    public void WriteFrame(byte[] pixels)
    {
        if (_closed || _writerFailed) return;
        try
        {
            _writeQueue.Add(pixels);
        }
        catch (InvalidOperationException)
        {
            // CompleteAdding() already called (Close() is racing with a caller still
            // writing) -- nothing left to do, this frame is simply dropped.
        }
    }

    /// <summary>Closes the pipe and waits for ffmpeg to finish muxing. Throws if ffmpeg exited non-zero.</summary>
    public void Close()
    {
        if (_closed) { _proc.WaitForExit(); return; }
        _closed = true;
        _writeQueue.CompleteAdding();
        _writerTask.Wait(); // drains whatever's still queued before we close stdin
        try { _proc.StandardInput.BaseStream.Close(); } catch { /* already gone */ }
        _proc.WaitForExit();
        ProcessWatchdog.Unregister(_proc);
        string stderrText;
        lock (_stderr) stderrText = _stderr.ToString();
        LogSink?.Invoke($"ffmpeg encode exited with code {_proc.ExitCode}. stderr:\n{stderrText}");
        if (_proc.ExitCode != 0)
            throw new InvalidOperationException($"ffmpeg encoding failed (exit {_proc.ExitCode}). stderr:\n{stderrText}");
    }

    public void Dispose()
    {
        if (!_closed) Close();
        _proc.Dispose();
        _writeQueue.Dispose();
    }
}
