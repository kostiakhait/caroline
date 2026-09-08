using XcfaRenderer;

internal static class MfccProbe
{
    public static void Run()
    {
        const int sr = 16000;
        var n = sr; // 1 second
        var audio = new float[n];
        for (var i = 0; i < n; i++)
            audio[i] = MathF.Sin(2f * MathF.PI * 220f * i / sr) * 0.5f;

        var (feats, energies) = AudioFeatures.Compute(audio, nFrames: 1, fps: 25, windowSamples: 640, leadMs: 0);
        Console.WriteLine("[mfcc-probe] feat[0]=[" + string.Join(",", feats[0].Select(v => v.ToString("F4"))) + "]");
        Console.WriteLine("[mfcc-probe] energy[0]=" + energies[0].ToString("F6"));
    }
}
