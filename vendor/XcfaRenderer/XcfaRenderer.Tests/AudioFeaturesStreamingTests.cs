using XcfaRenderer;

namespace XcfaRenderer.Tests;

public class AudioFeaturesStreamingTests
{
    private static float[] SineWave(double freqHz, double seconds, float amplitude = 0.5f)
    {
        var n = (int)(AudioFeatures.SampleRate * seconds);
        var samples = new float[n];
        for (var i = 0; i < n; i++)
            samples[i] = amplitude * MathF.Sin(2f * MathF.PI * (float)freqHz * i / AudioFeatures.SampleRate);
        return samples;
    }

    [Fact]
    public void StreamingMatchesBatchWithCachingDisabled()
    {
        var audio = SineWave(440, 2.0);
        const int fps = 5;
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / fps);

        var (batchFeats, batchEnergies) = AudioFeatures.Compute(audio, nFrames: 10, fps: fps, windowSamples: windowSamples, leadMs: 250);
        var (stream, streamEnergies) = AudioFeatures.ComputeStreaming(audio, nFrames: 10, fps: fps, windowSamples: windowSamples,
            leadMs: 250, sampleFps: fps, cacheDir: null);

        Assert.Equal(batchEnergies, streamEnergies);
        for (var i = 0; i < 10; i++)
        {
            var row = stream[i]; // blocks until ready
            for (var d = 0; d < 16; d++)
                Assert.Equal(batchFeats[i][d], row[d], precision: 3);
        }
    }

    [Fact]
    public void IndexerClampsOutOfRangeIndices()
    {
        var audio = SineWave(300, 1.0);
        const int fps = 5;
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / fps);
        var (stream, _) = AudioFeatures.ComputeStreaming(audio, nFrames: 5, fps: fps, windowSamples: windowSamples, cacheDir: null);

        var last = stream[4];
        var clamped = stream[100]; // should clamp to the last valid index, not throw
        Assert.Equal(last, clamped);
    }
}
