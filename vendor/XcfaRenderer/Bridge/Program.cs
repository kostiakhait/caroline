using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using XcfaRenderer;

// Headless bridge to XcfaRenderer for a shell that is not the WPF app (the Linux shell, a
// test harness). The WPF shell calls the library in-process (VisualModeManager.cs); this is
// the same two calls -- Renderer.Prepare once, PreparedModel.Render per clip -- behind a pipe.
//
// Protocol: one JSON object per line.
//   stdin : {"cmd":"configure","modelPath":"...","ffmpeg":"ffmpeg","height":480}
//           {"cmd":"render","id":"tts-1","audio":"/tmp/a.mp3","out":"/tmp/a.webm"}
//           {"cmd":"prewarm","audio":["/a.mp3","/b.mp3"]}   (renders into the cache when nothing else waits)
//           {"cmd":"cancel","id":"tts-1"}   (a render not started yet is dropped; one running finishes)
//           {"cmd":"quit"}
//   stdout: {"event":"ready","prepMs":1234,"rest":"/path/rest.png"}   (rest: the model's rest pose, a PNG with alpha --
//           what stands in the window between two clips, mouth closed; the WPF shell shows the same picture)
//           {"event":"rendered","id":"tts-1","ok":true,"renderMs":2800,"durationS":3.4,"cached":false,"out":"..."}
//           {"event":"rendered","id":"tts-1","ok":false,"error":"..."}
//           {"event":"cancelled","id":"tts-1"}
//           {"event":"error","message":"..."}
// Clips render one at a time, in the order they arrive, as in VisualModeManager. ffmpeg's
// stderr goes to this process's stderr. The bridge exits when stdin closes or the parent dies.

// Rendered clips are cached on disk by the hash of the audio (with the model and the height), so a
// clip that comes again -- a filler remark, see backend-py/app/filler_phrases.py -- is not rendered
// again: the cached file is copied and the answer says `cached: true` with a renderMs near zero.
// The cache is $XCFA_CLIP_CACHE, else an "xcfa-clip-cache" folder in the temp directory.

var stdout = Console.Out;
var outGate = new object();
void Send(object payload)
{
    lock (outGate) { stdout.WriteLine(JsonSerializer.Serialize(payload)); stdout.Flush(); }
}

Encoder.LogSink = text => Console.Error.WriteLine($"[ffmpeg] {text}");
var parentPid = ParentPid();
if (parentPid > 0) ProcessWatchdog.WatchParent(parentPid, onParentGone: () => Environment.Exit(0));

Renderer.PreparedModel? prepared = null;
string ffmpeg = "ffmpeg";
string modelKey = "";
int outHeight = 480;
var clipCache = Environment.GetEnvironmentVariable("XCFA_CLIP_CACHE") ?? Path.Combine(Path.GetTempPath(), "xcfa-clip-cache");
Directory.CreateDirectory(clipCache);
string CacheKey(string audioPath)
{
    using var sha = System.Security.Cryptography.SHA1.Create();
    var audio = File.ReadAllBytes(audioPath);
    sha.TransformBlock(audio, 0, audio.Length, null, 0);
    var tag = System.Text.Encoding.UTF8.GetBytes($"{modelKey}|{outHeight}");
    sha.TransformFinalBlock(tag, 0, tag.Length);
    return Convert.ToHexString(sha.Hash!).ToLowerInvariant();
}
// Two queues, one worker: a clip somebody is waiting for always goes before a prewarm job.
var queue = new System.Collections.Concurrent.ConcurrentQueue<(string Id, string Audio, string Out)>();
var warmQueue = new System.Collections.Concurrent.ConcurrentQueue<string>();
var work = new SemaphoreSlim(0);
var closing = false;
var warmed = 0;
var cancelled = new ConcurrentDictionary<string, bool>();

var worker = new Thread(() =>
{
    while (true)
    {
        work.Wait();
        if (!queue.TryDequeue(out var job))
        {
            if (warmQueue.TryDequeue(out var warmAudio) && prepared != null)
            {
                try
                {
                    var key = Path.Combine(clipCache, CacheKey(warmAudio) + ".webm");
                    if (!File.Exists(key))
                    {
                        var tmpOut = Path.Combine(Path.GetTempPath(), $"warm-{Guid.NewGuid():N}.webm");
                        prepared.Render(warmAudio, tmpOut, new RenderOptions { RemoveBg = true, Height = outHeight, Ffmpeg = ffmpeg });
                        File.Move(tmpOut, key, overwrite: true);
                    }
                    if (Interlocked.Increment(ref warmed) % 16 == 0 || warmQueue.IsEmpty) Send(new { @event = "prewarmed", count = warmed, left = warmQueue.Count });
                }
                catch (Exception ex) { Send(new { @event = "error", message = "prewarm: " + ex.Message }); }
                continue;
            }
            if (closing && queue.IsEmpty && warmQueue.IsEmpty) break;
            continue;
        }
        if (cancelled.TryRemove(job.Id, out _)) { Send(new { @event = "cancelled", id = job.Id }); continue; }
        var model = prepared;
        if (model == null) { Send(new { @event = "rendered", id = job.Id, ok = false, error = "model not warmed" }); continue; }
        try
        {
            var sw = Stopwatch.StartNew();
            var seconds = AudioFeatures.LoadPcm(job.Audio, ffmpeg).Length / (double)AudioFeatures.SampleRate;
            var cached = Path.Combine(clipCache, CacheKey(job.Audio) + Path.GetExtension(job.Out));
            var fromCache = File.Exists(cached);
            if (fromCache) File.Copy(cached, job.Out, overwrite: true);
            else
            {
                model.Render(job.Audio, job.Out, new RenderOptions { RemoveBg = true, Height = outHeight, Ffmpeg = ffmpeg });
                var partial = cached + ".tmp";
                File.Copy(job.Out, partial, overwrite: true);
                File.Move(partial, cached, overwrite: true);
            }
            Send(new { @event = "rendered", id = job.Id, ok = true, renderMs = sw.ElapsedMilliseconds, durationS = seconds, cached = fromCache, @out = job.Out });
        }
        catch (Exception ex)
        {
            Send(new { @event = "rendered", id = job.Id, ok = false, error = ex.Message });
        }
    }
}) { IsBackground = false, Name = "xcfa-bridge-render" };
worker.Start();

string? line;
while ((line = Console.In.ReadLine()) != null)
{
    if (string.IsNullOrWhiteSpace(line)) continue;
    try
    {
        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;
        switch (root.GetProperty("cmd").GetString())
        {
            case "configure":
                ffmpeg = root.TryGetProperty("ffmpeg", out var f) ? f.GetString() ?? "ffmpeg" : "ffmpeg";
                var modelPath = root.GetProperty("modelPath").GetString()!;
                var height = root.TryGetProperty("height", out var h) ? h.GetInt32() : 480;
                outHeight = height;
                modelKey = modelPath + "|" + new FileInfo(modelPath).Length;
                var warm = Stopwatch.StartNew();
                prepared = Renderer.Prepare(modelPath, "preview.webm", new RenderOptions { RemoveBg = true, Height = height, Ffmpeg = ffmpeg });
                var restPath = Path.Combine(clipCache, "rest-" + Convert.ToHexString(System.Security.Cryptography.SHA1.HashData(System.Text.Encoding.UTF8.GetBytes(modelKey + "|" + height))).ToLowerInvariant() + ".png");
                if (!File.Exists(restPath)) File.WriteAllBytes(restPath, prepared.GetStaticFramePng());
                Send(new { @event = "ready", prepMs = warm.ElapsedMilliseconds, rest = restPath });
                break;
            case "render":
                queue.Enqueue((root.GetProperty("id").GetString()!, root.GetProperty("audio").GetString()!, root.GetProperty("out").GetString()!));
                work.Release();
                break;
            case "prewarm":
                foreach (var a in root.GetProperty("audio").EnumerateArray()) { warmQueue.Enqueue(a.GetString()!); work.Release(); }
                break;
            case "cancel":
                cancelled[root.GetProperty("id").GetString()!] = true;
                break;
            case "quit":
                closing = true; work.Release();
                worker.Join();
                return;
        }
    }
    catch (Exception ex)
    {
        Send(new { @event = "error", message = ex.Message });
    }
}
closing = true; work.Release();
worker.Join();

static int ParentPid()
{
    // The shell that started this process; 0 (no watch) when it cannot be told.
    try
    {
        var stat = File.ReadAllText($"/proc/{Environment.ProcessId}/stat");
        var fields = stat[(stat.LastIndexOf(')') + 2)..].Split(' ');
        return int.Parse(fields[1]);
    }
    catch { return 0; }
}
