using XcfaRenderer;

namespace XcfaRenderer.Tests;

public class AudioFeatureCachingTests : IDisposable
{
    private readonly string _cacheDir = Path.Combine(Path.GetTempPath(), $"xcfa_feat_cache_test_{Guid.NewGuid():N}");

    private static float[] SineWave(double freqHz, double seconds)
    {
        var n = (int)(AudioFeatures.SampleRate * seconds);
        var samples = new float[n];
        for (var i = 0; i < n; i++)
            samples[i] = 0.5f * MathF.Sin(2f * MathF.PI * (float)freqHz * i / AudioFeatures.SampleRate);
        return samples;
    }

    [Fact]
    public void SecondComputeWithSameAudioServesFromCacheSynchronously()
    {
        var audio = SineWave(440, 1.0);
        const int fps = 5;
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / fps);

        var (stream1, energies1) = AudioFeatures.ComputeStreaming(audio, nFrames: 5, fps: fps, windowSamples: windowSamples,
            sampleFps: fps, cacheDir: _cacheDir);
        var firstRow = stream1[4]; // force the background task to finish and save to cache
        // Give the background save a brief moment (Set/MarkReady happen before the save write) --
        // poll for the cache file rather than a fixed sleep.
        var cacheFile = Directory.Exists(_cacheDir) ? Directory.GetFiles(_cacheDir, "*.xcfafeat") : Array.Empty<string>();
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (cacheFile.Length == 0 && DateTime.UtcNow < deadline)
        {
            Thread.Sleep(20);
            cacheFile = Directory.Exists(_cacheDir) ? Directory.GetFiles(_cacheDir, "*.xcfafeat") : Array.Empty<string>();
        }
        Assert.NotEmpty(cacheFile);

        var (stream2, energies2) = AudioFeatures.ComputeStreaming(audio, nFrames: 5, fps: fps, windowSamples: windowSamples,
            sampleFps: fps, cacheDir: _cacheDir);

        Assert.Equal(energies1, energies2);
        for (var i = 0; i < 5; i++)
            Assert.Equal(stream1[i], stream2[i]);
    }

    [Fact]
    public void DifferentAudioProducesDifferentCacheKey()
    {
        var a = SineWave(300, 0.5);
        var b = SineWave(300, 0.5);
        for (var i = 0; i < b.Length; i++) b[i] *= 0.5f; // different amplitude -> different bytes

        var keyA = FeatureCache.ComputeKey(a, 3, 5, 3200, AudioFeatures.SampleRate, 5, 250);
        var keyB = FeatureCache.ComputeKey(b, 3, 5, 3200, AudioFeatures.SampleRate, 5, 250);

        Assert.NotEqual(keyA, keyB);
    }

    public void Dispose()
    {
        try { if (Directory.Exists(_cacheDir)) Directory.Delete(_cacheDir, recursive: true); } catch { /* best effort */ }
    }
}
