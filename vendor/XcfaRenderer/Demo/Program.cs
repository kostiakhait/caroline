using System.Diagnostics;
using XcfaRenderer;

if (args.Length > 0 && args[0] == "--mfcc-probe") { MfccProbe.Run(); return; }
if (args.Length > 0 && args[0] == "--feat-dist")
{
    using var probe0 = XcfaFile.Open(args[1]);
    var counts = new Dictionary<int, int>();
    foreach (var seg in probe0.Catalog.Segments)
    {
        var validCount = seg.AudioFeatures.Count(f => f.Any(v => v != 0f));
        if (!counts.TryAdd(validCount, 1)) counts[validCount]++;
    }
    foreach (var kv in counts.OrderBy(k => k.Key))
        Console.WriteLine($"[feat-dist] validFramesPerSeg={kv.Key} count={kv.Value}");

    // Show which specific frame indices are valid for a few segments with n=10.
    foreach (var seg in probe0.Catalog.Segments.Where(s => s.FrameCount == 10).Take(5))
    {
        var validIdx = string.Join(",", Enumerable.Range(0, seg.AudioFeatures.Length).Where(i => seg.AudioFeatures[i].Any(v => v != 0f)));
        Console.WriteLine($"[feat-dist] seg id={seg.Id} n={seg.FrameCount} validIdx=[{validIdx}]");
    }
    return;
}

var xcfaPath = args.Length > 0 ? args[0] : @"d:\REPO\silmarillion\Caroline\art\models\CarolineB.xcfa";
var audioPath = args.Length > 1 ? args[1] : @"C:\Users\khait\AppData\Local\Temp\claude\d--REPO-silmarillion\c249cdcd-0dcd-4a23-8540-1b320b520408\scratchpad\test.mp3";
var outputPath = args.Length > 2 ? args[2] : @"C:\Users\khait\AppData\Local\Temp\claude\d--REPO-silmarillion\c249cdcd-0dcd-4a23-8540-1b320b520408\scratchpad\caroline_test.webm";

Console.WriteLine($"Model : {xcfaPath}");
Console.WriteLine($"Audio : {audioPath}");
Console.WriteLine($"Output: {outputPath}");
Console.WriteLine();

var options = new RenderOptions
{
    Fps = 25,
    Height = 360,
    RemoveBg = true,      // transparent background -- needs baked alpha masks (Catalog.HasBg)
    MicroMovement = true,
    MinSilenceSeconds = 0.5,
    SilenceExitSeconds = 0.2,
};

using (var probe = XcfaFile.Open(xcfaPath))
{
    Console.WriteLine($"[probe] catalog.Fps={probe.Catalog.Fps} segments={probe.Catalog.Segments.Length} silenceSegs={probe.Catalog.Segments.Count(s => s.IsSilence)}");
    // Dump a handful of stored per-frame audio_features vectors and mean_features, to sanity-check scale.
    var seg0 = probe.Catalog.Segments[0];
    Console.WriteLine($"[probe] seg0 id={seg0.Id} n={seg0.FrameCount} meanFeatures=[{string.Join(",", seg0.MeanFeatures.Select(v => v.ToString("F3")))}]");
    for (var i = 0; i < Math.Min(3, seg0.AudioFeatures.Length); i++)
        Console.WriteLine($"[probe] seg0.audioFeatures[{i}]=[{string.Join(",", seg0.AudioFeatures[i].Select(v => v.ToString("F3")))}]");
}

var prepSw = Stopwatch.StartNew();
using var model = Renderer.Prepare(xcfaPath, outputPath, options);
prepSw.Stop();
Console.WriteLine($"Preparation (one-time, reusable): {prepSw.Elapsed.TotalSeconds:F2} s");

var renderSw = Stopwatch.StartNew();
model.Render(audioPath, outputPath, options);
renderSw.Stop();
Console.WriteLine($"Render (this specific clip):      {renderSw.Elapsed.TotalSeconds:F2} s");

var sizeMb = new FileInfo(outputPath).Length / 1_048_576.0;
Console.WriteLine();
Console.WriteLine($"Done -> {outputPath} ({sizeMb:F2} MB)");
