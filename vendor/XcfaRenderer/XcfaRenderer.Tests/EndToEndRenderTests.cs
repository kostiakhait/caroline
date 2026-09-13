using XcfaRenderer;

namespace XcfaRenderer.Tests;

/// <summary>
/// Exercises the full Renderer pipeline (audio load -> feature extraction ->
/// segment library -> frame selection -> composite -> ffmpeg encode) against
/// a real, already-compacted .xcfa model from Caroline/art/models. Skips
/// itself (rather than failing) when neither a model file nor ffmpeg is
/// available, since those are host-machine assets this repo doesn't ship in
/// source control / CI.
/// </summary>
public sealed class EndToEndRenderTests : IDisposable
{
    private static readonly string[] CandidateModels =
    {
        // Bug fix (2026-09-12): confirmed live -- none of these ever existed on
        // this machine (a stale path from a different checkout layout), so this
        // test has been silently skipping instead of actually exercising the
        // pipeline. The real models CarolineInstaller provisions live as a
        // sibling of the app's own install dir -- see BackendProcess.cs's
        // CAROLINE_MODELS_DIR comment for why (tens of GB each, must survive
        // every app-zip update). Kept the old paths too, harmless if unused.
        @"C:\Users\khait\AppData\Local\Caroline\art\models\CarolineA.xcfa",
        @"C:\Users\khait\AppData\Local\Caroline\art\models\CarolineB.xcfa",
        @"C:\Users\khait\AppData\Local\Caroline\art\models\PeterA.xcfa",
        @"C:\Users\khait\AppData\Local\Caroline\art\models\PeterB.xcfa",
        @"d:\REPO\silmarillion\Caroline\art\models\CarolineA.xcfa",
        @"d:\REPO\silmarillion\Caroline\art\models\CarolineB.xcfa",
        @"d:\REPO\silmarillion\Caroline\art\models\PeterA.xcfa",
        @"d:\REPO\silmarillion\Caroline\art\models\PeterB.xcfa",
    };

    private readonly List<string> _tempFiles = new();

    private static string? FindModel() => CandidateModels.FirstOrDefault(File.Exists);

    private string WriteSineWav(double freqHz, double seconds, int sampleRate = 16000)
    {
        var path = Path.Combine(Path.GetTempPath(), $"xcfa_e2e_{Guid.NewGuid():N}.wav");
        _tempFiles.Add(path);

        var n = (int)(sampleRate * seconds);
        var data = new short[n];
        for (var i = 0; i < n; i++)
            data[i] = (short)(8000 * Math.Sin(2 * Math.PI * freqHz * i / sampleRate));

        using var fs = new FileStream(path, FileMode.Create);
        using var bw = new BinaryWriter(fs);
        var dataBytes = data.Length * 2;
        bw.Write("RIFF"u8.ToArray());
        bw.Write(36 + dataBytes);
        bw.Write("WAVE"u8.ToArray());
        bw.Write("fmt "u8.ToArray());
        bw.Write(16);
        bw.Write((short)1);          // PCM
        bw.Write((short)1);          // mono
        bw.Write(sampleRate);
        bw.Write(sampleRate * 2);    // byte rate
        bw.Write((short)2);          // block align
        bw.Write((short)16);         // bits per sample
        bw.Write("data"u8.ToArray());
        bw.Write(dataBytes);
        foreach (var s in data) bw.Write(s);

        return path;
    }

    [SkippableFact]
    public void RendersAShortClipToANonEmptyVideoFile()
    {
        var modelPath = FindModel();
        Skip.If(modelPath is null, "No real .xcfa model found under Caroline/art/models on this machine.");

        var audioPath = WriteSineWav(220, 1.5);
        var outputPath = Path.Combine(Path.GetTempPath(), $"xcfa_e2e_{Guid.NewGuid():N}.mp4");
        _tempFiles.Add(outputPath);

        Renderer.Render(modelPath!, audioPath, outputPath, new RenderOptions
        {
            Fps = 15,
            Height = 240, // keep the test fast -- resolution doesn't affect the pipeline logic being tested
        });

        Assert.True(File.Exists(outputPath));
        Assert.True(new FileInfo(outputPath).Length > 0);
    }

    public void Dispose()
    {
        foreach (var f in _tempFiles)
        {
            try { File.Delete(f); } catch { /* best effort */ }
        }
    }
}
