using XcfaRenderer;

namespace XcfaRenderer.Tests;

// AudioFeatures.LoadPcm shells out to a real ffmpeg binary, so it's not covered
// here (no deterministic, dependency-free way to exercise it in a unit test) --
// these tests exercise Compute() directly against synthetic PCM instead.
public class AudioFeaturesTests
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
    public void ProducesOneFullFeatureVectorPerRequestedFrame()
    {
        var audio = SineWave(440, 1.0);
        const int fps = 5;
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / fps);

        var (features, energies) = AudioFeatures.Compute(audio, nFrames: 5, fps: fps, windowSamples: windowSamples);

        Assert.Equal(5, features.Length);
        Assert.Equal(5, energies.Length);
        Assert.All(features, f => Assert.Equal(16, f.Length));
    }

    [Fact]
    public void ToneHasHigherEnergyThanSilence()
    {
        var tone = SineWave(440, 1.0);
        var silence = new float[AudioFeatures.SampleRate]; // all-zero PCM
        const int fps = 5;
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / fps);

        var (_, toneEnergies) = AudioFeatures.Compute(tone, nFrames: 5, fps: fps, windowSamples: windowSamples);
        var (_, silenceEnergies) = AudioFeatures.Compute(silence, nFrames: 5, fps: fps, windowSamples: windowSamples);

        Assert.All(toneEnergies, e => Assert.True(e > 0.1f));
        Assert.All(silenceEnergies, e => Assert.Equal(0f, e));
    }

    [Fact]
    public void FeatureValuesAreFiniteEvenForSilence()
    {
        var silence = new float[AudioFeatures.SampleRate];
        const int fps = 5;
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / fps);

        var (features, _) = AudioFeatures.Compute(silence, nFrames: 5, fps: fps, windowSamples: windowSamples);

        foreach (var vec in features)
            foreach (var v in vec)
                Assert.False(float.IsNaN(v) || float.IsInfinity(v));
    }

    [Fact]
    public void HigherPitchToneHasHigherSpectralCentroidThanLowerPitchTone()
    {
        const int fps = 5;
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / fps);
        var lowTone = SineWave(200, 1.0);
        var highTone = SineWave(3000, 1.0);

        var (lowFeatures, _) = AudioFeatures.Compute(lowTone, nFrames: 5, fps: fps, windowSamples: windowSamples);
        var (highFeatures, _) = AudioFeatures.Compute(highTone, nFrames: 5, fps: fps, windowSamples: windowSamples);

        // Index 14 is spectral centroid (see AudioFeatures.Compute's 16-dim layout: 13 MFCC, energy, centroid, bandwidth).
        Assert.True(highFeatures[^1][14] > lowFeatures[^1][14]);
    }

    [Fact]
    public void EmaSmoothingPullsFirstFrameFullyToRawFeature()
    {
        var tone = SineWave(440, 1.0);
        const int fps = 5;
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / fps);

        var (features, energies) = AudioFeatures.Compute(tone, nFrames: 5, fps: fps, windowSamples: windowSamples);

        // The very first frame has no smoothing history, so its energy dim must equal the raw computed energy exactly.
        Assert.Equal(energies[0], features[0][13]);
    }
}
