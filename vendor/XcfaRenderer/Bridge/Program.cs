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
//           {"cmd":"cancel","id":"tts-1"}   (a render not started yet is dropped; one running finishes)
//           {"cmd":"quit"}
//   stdout: {"event":"ready","prepMs":1234}
//           {"event":"rendered","id":"tts-1","ok":true,"renderMs":2800,"durationS":3.4,"out":"..."}
//           {"event":"rendered","id":"tts-1","ok":false,"error":"..."}
//           {"event":"cancelled","id":"tts-1"}
//           {"event":"error","message":"..."}
// Clips render one at a time, in the order they arrive, as in VisualModeManager. ffmpeg's
// stderr goes to this process's stderr. The bridge exits when stdin closes or the parent dies.

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
var queue = new BlockingCollection<(string Id, string Audio, string Out)>();
var cancelled = new ConcurrentDictionary<string, bool>();

var worker = new Thread(() =>
{
    foreach (var job in queue.GetConsumingEnumerable())
    {
        if (cancelled.TryRemove(job.Id, out _)) { Send(new { @event = "cancelled", id = job.Id }); continue; }
        var model = prepared;
        if (model == null) { Send(new { @event = "rendered", id = job.Id, ok = false, error = "model not warmed" }); continue; }
        try
        {
            var sw = Stopwatch.StartNew();
            var seconds = AudioFeatures.LoadPcm(job.Audio, ffmpeg).Length / (double)AudioFeatures.SampleRate;
            model.Render(job.Audio, job.Out, new RenderOptions { RemoveBg = true, Height = 480, Ffmpeg = ffmpeg });
            Send(new { @event = "rendered", id = job.Id, ok = true, renderMs = sw.ElapsedMilliseconds, durationS = seconds, @out = job.Out });
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
                var warm = Stopwatch.StartNew();
                prepared = Renderer.Prepare(modelPath, "preview.webm", new RenderOptions { RemoveBg = true, Height = height, Ffmpeg = ffmpeg });
                Send(new { @event = "ready", prepMs = warm.ElapsedMilliseconds });
                break;
            case "render":
                queue.Add((root.GetProperty("id").GetString()!, root.GetProperty("audio").GetString()!, root.GetProperty("out").GetString()!));
                break;
            case "cancel":
                cancelled[root.GetProperty("id").GetString()!] = true;
                break;
            case "quit":
                queue.CompleteAdding();
                worker.Join();
                return;
        }
    }
    catch (Exception ex)
    {
        Send(new { @event = "error", message = ex.Message });
    }
}
queue.CompleteAdding();
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
