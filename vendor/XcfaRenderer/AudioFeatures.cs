using System.Diagnostics;
using System.Numerics;
using System.Threading;

namespace XcfaRenderer;

/// <summary>
/// Runtime audio-feature extraction: decodes an input speech clip and produces
/// the same 16-dim per-output-frame feature vectors (13 MFCC + RMS energy +
/// spectral centroid + spectral bandwidth, EMA-smoothed) that a model's stored
/// segments were built from -- direct port of _render_worker.py's
/// _load_audio_ffmpeg + _precompute_audio_features (Compute, the batched
/// call) AND _start_audio_feat_stream/_fill_feat_stream_chunked/_FeatStream
/// (ComputeStreaming + the nested FeatureStream type, the chunked
/// background-thread variant production actually uses so rendering can
/// start writing frames before the whole clip's features are computed).
///
/// Reimplemented as self-contained DSP (own FFT/mel/DCT) rather than a
/// librosa-equivalent NuGet package, so the exact algorithm (Slaney mel
/// scale, orthonormal DCT-II, hann-windowed STFT with center=False) is
/// fully under our control and traceable line-for-line back to librosa's
/// own definitions, which is what the stored segment features were computed
/// with.
/// </summary>
public static class AudioFeatures
{
    public const int SampleRate = 16000;
    // Module-level default in _render_worker.py:54 (_FEATURE_LEAD_MS = 250) -- NOT the
    // 130 hardcoded inside _precompute_audio_features, which is dead code (zero call
    // sites besides its own def; the real default path, _start_audio_feat_stream, uses
    // the module-level 250 constant, overridable via main()'s lead_ms arg -- see
    // RenderOptions.LeadMs).
    private const int FeatureLeadMs = 250;
    private const int NMels = 128;
    private const int NMfcc = 13;
    private const float EmaOld = 0.45f;
    private const float EmaNew = 0.55f;

    /// <summary>Decodes any audio file to mono float32 PCM at <see cref="SampleRate"/> via ffmpeg.</summary>
    public static float[] LoadPcm(string audioPath, string ffmpeg = "ffmpeg")
    {
        var psi = new ProcessStartInfo
        {
            FileName = ffmpeg,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        psi.ArgumentList.Add("-y");
        psi.ArgumentList.Add("-i");
        psi.ArgumentList.Add(audioPath);
        psi.ArgumentList.Add("-ac");
        psi.ArgumentList.Add("1");
        psi.ArgumentList.Add("-ar");
        psi.ArgumentList.Add(SampleRate.ToString());
        psi.ArgumentList.Add("-f");
        psi.ArgumentList.Add("f32le");
        psi.ArgumentList.Add("pipe:1");

        using var proc = Process.Start(psi) ?? throw new InvalidOperationException("Failed to start ffmpeg.");
        var stderrTask = proc.StandardError.ReadToEndAsync();
        using var stdout = new MemoryStream();
        proc.StandardOutput.BaseStream.CopyTo(stdout);
        proc.WaitForExit();
        var stderrText = stderrTask.GetAwaiter().GetResult();
        Encoder.LogSink?.Invoke($"ffmpeg PCM decode (\"{audioPath}\") exited with code {proc.ExitCode}. stderr:\n{stderrText}");

        var bytes = stdout.ToArray();
        if (bytes.Length == 0)
            throw new InvalidOperationException($"ffmpeg produced no audio output for \"{audioPath}\". stderr:\n{stderrText}");

        var floats = new float[bytes.Length / sizeof(float)];
        Buffer.BlockCopy(bytes, 0, floats, 0, floats.Length * sizeof(float));
        return floats;
    }

    /// <summary>
    /// Computes nFrames EMA-smoothed 16-dim feature vectors (one per output
    /// video frame at the given fps) plus their un-smoothed RMS energies,
    /// from raw mono PCM at <see cref="SampleRate"/>. windowSamples should be
    /// max(256, SampleRate / catalogFps), matching XcfaCatalog.Fps.
    /// </summary>
    public static (float[][] Features, float[] Energies) Compute(float[] audioRaw, int nFrames, int fps, int windowSamples, int leadMs = FeatureLeadMs)
    {
        var leadSamples = SampleRate * leadMs / 1000;
        var energies = ComputeEnergies(audioRaw, nFrames, fps, windowSamples, leadSamples);

        var nSig = audioRaw.Length;
        var shifted = leadSamples < nSig ? audioRaw[leadSamples..] : audioRaw;

        var hopLength = Math.Max(64, SampleRate / fps);
        var nFft = ComputeNFft(windowSamples);

        var spectralFrames = StftMagnitudes(shifted, nFft, hopLength);
        var melFilters = BuildMelFilterbank(SampleRate, nFft, NMels);

        var t = Math.Max(1, spectralFrames.Count);
        var mfcc = new float[t][];
        var centroid = new float[t];
        var bandwidth = new float[t];
        var freqs = FftBinFrequencies(SampleRate, nFft);

        for (var i = 0; i < t; i++)
        {
            var mag = i < spectralFrames.Count ? spectralFrames[i] : new float[nFft / 2 + 1];
            mfcc[i] = ComputeMfcc(mag, melFilters);
            (centroid[i], bandwidth[i]) = ComputeCentroidBandwidth(mag, freqs);
        }

        var features = new float[nFrames][];
        float[]? sf = null;
        for (var i = 0; i < nFrames; i++)
        {
            var ti = Math.Min(i, t - 1);
            var feat = new float[16];
            Array.Copy(mfcc[ti], feat, NMfcc);
            feat[13] = energies[i];
            feat[14] = centroid[ti];
            feat[15] = bandwidth[ti];

            if (sf is null)
            {
                sf = (float[])feat.Clone();
            }
            else
            {
                var next = new float[16];
                for (var d = 0; d < 16; d++)
                    next[d] = EmaOld * sf[d] + EmaNew * feat[d];
                sf = next;
            }
            features[i] = (float[])sf.Clone();
        }

        return (features, energies);
    }

    /// <summary>Default on-disk feature-cache directory (see FeatureCache.cs) -- pass null to RenderOptions/ComputeStreaming's cacheDir to disable caching entirely.</summary>
    public static readonly string DefaultFeatureCacheDir = Path.Combine(Path.GetTempPath(), "xcfa_feat_cache");

    /// <summary>
    /// Streaming/chunked variant: computes energies instantly (matches
    /// _FeatStream's own doc: "energies is a plain list (computed instantly,
    /// no blocking)"), then either serves a disk-cache hit synchronously
    /// (see FeatureCache.cs) or kicks off a background task that fills the
    /// returned FeatureStream chunk-by-chunk (5 seconds of output frames at
    /// a time, by default), each chunk running its own batched STFT over
    /// just that slice of audio (plus n_fft of context) rather than the
    /// whole clip at once, saving to the cache once done. A caller reading
    /// FeatureStream[i] blocks only until frame i's chunk has been filled --
    /// direct port of _start_audio_feat_stream + _fill_feat_stream_chunked +
    /// _FeatStream (including its disk-cache hit/save paths).
    /// </summary>
    public static (FeatureStream Stream, float[] Energies) ComputeStreaming(
        float[] audioRaw, int nFrames, int fps, int windowSamples, int leadMs = FeatureLeadMs,
        int chunkFrames = -1, float sampleFps = 5f, string? cacheDir = null)
    {
        if (chunkFrames <= 0) chunkFrames = Math.Max(1, fps * 5); // "process 5 seconds at a time"
        // cacheDir left null disables caching entirely -- callers wanting the default location pass AudioFeatures.DefaultFeatureCacheDir explicitly (see RenderOptions.FeatureCacheDir).

        var leadSamples = SampleRate * leadMs / 1000;
        var energies = ComputeEnergies(audioRaw, nFrames, fps, windowSamples, leadSamples);

        var cacheKey = cacheDir is not null
            ? FeatureCache.ComputeKey(audioRaw, nFrames, fps, windowSamples, SampleRate, sampleFps, leadMs)
            : null;
        if (cacheKey is not null && FeatureCache.TryLoad(cacheDir!, cacheKey, nFrames, out var cachedFeats, out var cachedEnergies))
        {
            var hitStream = new FeatureStream(nFrames);
            for (var i = 0; i < nFrames; i++) hitStream.Set(i, cachedFeats![i]);
            hitStream.MarkReady(nFrames);
            return (hitStream, cachedEnergies!); // cache-hit energies -- bit-identical to the ones just recomputed above, kept for symmetry with the Python original's cache-hit path
        }

        var nSig = audioRaw.Length;
        var shifted = leadSamples < nSig ? audioRaw[leadSamples..] : audioRaw;
        var hopLength = Math.Max(64, SampleRate / fps);
        var nFft = ComputeNFft(windowSamples);
        var melFilters = BuildMelFilterbank(SampleRate, nFft, NMels);
        var freqs = FftBinFrequencies(SampleRate, nFft);

        var stream = new FeatureStream(nFrames);

        Task.Run(() =>
        {
            try
            {
                float[]? ema = null;
                for (var chunkStart = 0; chunkStart < nFrames; chunkStart += chunkFrames)
                {
                    var chunkEnd = Math.Min(nFrames, chunkStart + chunkFrames);

                    var sStart = chunkStart * hopLength;
                    var sEnd = Math.Min(shifted.Length, chunkEnd * hopLength + nFft);
                    var chunkAudio = shifted[sStart..sEnd];
                    if (chunkAudio.Length < nFft)
                    {
                        var padded = new float[nFft];
                        Array.Copy(chunkAudio, padded, chunkAudio.Length);
                        chunkAudio = padded;
                    }

                    var spectralFrames = StftMagnitudes(chunkAudio, nFft, hopLength);
                    var t = Math.Max(1, spectralFrames.Count);

                    for (var i = 0; i < chunkEnd - chunkStart; i++)
                    {
                        var ti = Math.Min(i, t - 1);
                        var mag = ti < spectralFrames.Count ? spectralFrames[ti] : new float[nFft / 2 + 1];
                        var mfcc = ComputeMfcc(mag, melFilters);
                        var (cent, bw) = ComputeCentroidBandwidth(mag, freqs);

                        var feat = new float[16];
                        Array.Copy(mfcc, feat, NMfcc);
                        var frameIdx = chunkStart + i;
                        feat[13] = energies[frameIdx];
                        feat[14] = cent;
                        feat[15] = bw;

                        ema = ema is null ? (float[])feat.Clone() : EmaStep(ema, feat);
                        stream.Set(frameIdx, (float[])ema.Clone());
                    }

                    stream.MarkReady(chunkEnd);
                }

                if (cacheKey is not null)
                {
                    try { FeatureCache.Save(cacheDir!, cacheKey, stream.SnapshotAll(), energies); }
                    catch { /* cache save failure must never fail the render */ }
                }
            }
            catch (Exception ex)
            {
                stream.Fail(ex);
            }
        });

        return (stream, energies);
    }

    private static float[] EmaStep(float[] prev, float[] feat)
    {
        var next = new float[16];
        for (var d = 0; d < 16; d++)
            next[d] = EmaOld * prev[d] + EmaNew * feat[d];
        return next;
    }

    /// <summary>
    /// List-like container for feature vectors filled chunk-by-chunk by a
    /// background task; indexing blocks until the requested frame's chunk
    /// has been written, then returns immediately. Direct port of
    /// _render_worker.py's _FeatStream.
    /// </summary>
    public sealed class FeatureStream
    {
        private readonly float[][] _data;
        private readonly object _gate = new();
        private int _ready;
        private Exception? _error;

        internal FeatureStream(int nFrames) => _data = new float[nFrames][];

        public int Length => _data.Length;

        internal void Set(int index, float[] value) => _data[index] = value;

        /// <summary>All rows, as-is -- only safe to call once every index has been Set (e.g. right after the fill loop, before saving to cache).</summary>
        internal float[][] SnapshotAll() => _data!;

        internal void MarkReady(int upTo)
        {
            lock (_gate) { _ready = upTo; Monitor.PulseAll(_gate); }
        }

        internal void Fail(Exception exc)
        {
            lock (_gate) { _error = exc; Monitor.PulseAll(_gate); }
        }

        private void Wait(int need)
        {
            lock (_gate)
            {
                while (_ready < need && _error is null)
                    Monitor.Wait(_gate, 50);
            }
            if (_error is not null)
                throw new InvalidOperationException("Audio feature extraction failed.", _error);
        }

        /// <summary>Blocks until frame index i's feature vector has been computed, then returns it.</summary>
        public float[] this[int i]
        {
            get
            {
                var idx = Math.Clamp(i, 0, _data.Length - 1);
                Wait(idx + 1);
                return _data[idx];
            }
        }
    }

    // ---- RMS energy: per-frame window centered at (frame/fps*sr + lead), matching
    // _precompute_audio_features's vectorised cumsum approach (done as a direct
    // loop here -- output frame counts are small enough that this is not a
    // meaningful cost, and it avoids reimplementing numpy's cumsum machinery). ----
    private static float[] ComputeEnergies(float[] audioRaw, int nFrames, int fps, int windowSamples, int leadSamples)
    {
        var half = windowSamples / 2;
        var nSig = audioRaw.Length;

        var sqCum = new double[nSig + 1];
        for (var i = 0; i < nSig; i++)
            sqCum[i + 1] = sqCum[i] + (double)audioRaw[i] * audioRaw[i];

        var energies = new float[nFrames];
        for (var f = 0; f < nFrames; f++)
        {
            var centre = (long)((double)f / fps * SampleRate) + leadSamples;
            var start = (int)Math.Clamp(centre - half, 0, nSig);
            var end = (int)Math.Min(start + windowSamples, nSig);
            var winLen = Math.Max(end - start, 1);
            var winSum = sqCum[end] - sqCum[start];
            energies[f] = (float)Math.Sqrt(winSum / winLen);
        }
        return energies;
    }

    // n_fft = min(2048, 1 << max(7, (window_samples - 1).bit_length() - 1))
    private static int ComputeNFft(int windowSamples)
    {
        var v = windowSamples - 1;
        var bitLength = v <= 0 ? 0 : (int)Math.Floor(Math.Log2(v)) + 1;
        var shift = Math.Max(7, bitLength - 1);
        return Math.Min(2048, 1 << shift);
    }

    // ---- STFT magnitude spectrogram: hann window, center=False (no padding, frames
    // start every hopLength samples), matching librosa.feature.mfcc/spectral_centroid/
    // spectral_bandwidth's shared default STFT settings. ----
    private static List<float[]> StftMagnitudes(float[] signal, int nFft, int hopLength)
    {
        var result = new List<float[]>();
        if (signal.Length < nFft) return result;

        var window = HannWindow(nFft);
        var nBins = nFft / 2 + 1;

        for (var start = 0; start + nFft <= signal.Length; start += hopLength)
        {
            var buf = new Complex[nFft];
            for (var i = 0; i < nFft; i++)
                buf[i] = signal[start + i] * window[i];
            Fft.Forward(buf);

            var mag = new float[nBins];
            for (var k = 0; k < nBins; k++)
                mag[k] = (float)buf[k].Magnitude;
            result.Add(mag);
        }
        return result;
    }

    private static float[] HannWindow(int n)
    {
        var w = new float[n];
        for (var i = 0; i < n; i++)
            w[i] = 0.5f - 0.5f * MathF.Cos(2f * MathF.PI * i / (n - 1));
        return w;
    }

    private static float[] FftBinFrequencies(int sr, int nFft)
    {
        var nBins = nFft / 2 + 1;
        var freqs = new float[nBins];
        for (var k = 0; k < nBins; k++)
            freqs[k] = (float)k * sr / nFft;
        return freqs;
    }

    private static (float Centroid, float Bandwidth) ComputeCentroidBandwidth(float[] mag, float[] freqs)
    {
        double sumMag = 0, sumFreqMag = 0;
        for (var k = 0; k < mag.Length; k++)
        {
            sumMag += mag[k];
            sumFreqMag += freqs[k] * mag[k];
        }
        if (sumMag < 1e-12) return (0f, 0f);

        var centroid = sumFreqMag / sumMag;
        double sumDevSq = 0;
        for (var k = 0; k < mag.Length; k++)
        {
            var dev = freqs[k] - centroid;
            sumDevSq += mag[k] * dev * dev;
        }
        var bandwidth = Math.Sqrt(sumDevSq / sumMag);
        return ((float)centroid, (float)bandwidth);
    }

    // ---- Mel filterbank (Slaney scale + Slaney area normalisation), matching
    // librosa.filters.mel(sr, n_fft, n_mels, fmin=0, fmax=sr/2, htk=False). ----
    private static float[][] BuildMelFilterbank(int sr, int nFft, int nMels)
    {
        var nBins = nFft / 2 + 1;
        var fMin = 0.0;
        var fMax = sr / 2.0;

        var melMin = HzToMel(fMin);
        var melMax = HzToMel(fMax);
        var melPoints = new double[nMels + 2];
        for (var i = 0; i < nMels + 2; i++)
            melPoints[i] = melMin + (melMax - melMin) * i / (nMels + 1);

        var hzPoints = melPoints.Select(MelToHz).ToArray();
        var binFreqs = FftBinFrequencies(sr, nFft).Select(f => (double)f).ToArray();

        var filters = new float[nMels][];
        for (var m = 0; m < nMels; m++)
        {
            var fLeft = hzPoints[m];
            var fCentre = hzPoints[m + 1];
            var fRight = hzPoints[m + 2];
            var filter = new float[nBins];
            for (var k = 0; k < nBins; k++)
            {
                var f = binFreqs[k];
                double w;
                if (f < fLeft || f > fRight) w = 0.0;
                else if (f <= fCentre) w = (f - fLeft) / Math.Max(fCentre - fLeft, 1e-12);
                else w = (fRight - f) / Math.Max(fRight - fCentre, 1e-12);
                filter[k] = (float)w;
            }
            // Slaney area normalisation: enorm = 2 / (hz[m+2] - hz[m])
            var enorm = 2.0 / Math.Max(fRight - fLeft, 1e-12);
            for (var k = 0; k < nBins; k++)
                filter[k] = (float)(filter[k] * enorm);
            filters[m] = filter;
        }
        return filters;
    }

    private static double HzToMel(double f)
    {
        const double fSp = 200.0 / 3.0;
        const double minLogHz = 1000.0;
        var minLogMel = minLogHz / fSp;
        var logstep = Math.Log(6.4) / 27.0;
        return f < minLogHz ? f / fSp : minLogMel + Math.Log(f / minLogHz) / logstep;
    }

    private static double MelToHz(double mel)
    {
        const double fSp = 200.0 / 3.0;
        const double minLogHz = 1000.0;
        var minLogMel = minLogHz / fSp;
        var logstep = Math.Log(6.4) / 27.0;
        return mel < minLogMel ? mel * fSp : minLogHz * Math.Exp(logstep * (mel - minLogMel));
    }

    // ---- MFCC: power-mel-spectrogram -> log (dB) -> orthonormal DCT-II, first 13 coeffs.
    // Matches librosa.feature.mfcc's default chain (power=2.0, power_to_db, dct type=2 norm='ortho'). ----
    private static float[] ComputeMfcc(float[] mag, float[][] melFilters)
    {
        var nMels = melFilters.Length;
        var melPower = new double[nMels];
        for (var m = 0; m < nMels; m++)
        {
            double sum = 0;
            var filt = melFilters[m];
            for (var k = 0; k < mag.Length; k++)
                sum += (double)mag[k] * mag[k] * filt[k];
            melPower[m] = sum;
        }

        // power_to_db: 10*log10(max(power, amin)), then reference to max, floored at -80 dB
        // (librosa defaults: ref=1.0, amin=1e-10, top_db=80.0).
        const double amin = 1e-10;
        const double topDb = 80.0;
        var db = new double[nMels];
        var maxDb = double.NegativeInfinity;
        for (var m = 0; m < nMels; m++)
        {
            db[m] = 10.0 * Math.Log10(Math.Max(amin, melPower[m]));
            if (db[m] > maxDb) maxDb = db[m];
        }
        for (var m = 0; m < nMels; m++)
            db[m] = Math.Max(db[m], maxDb - topDb);

        return DctOrthonormal(db, NMfcc);
    }

    private static float[] DctOrthonormal(double[] x, int nOut)
    {
        var n = x.Length;
        var result = new float[nOut];
        for (var k = 0; k < nOut; k++)
        {
            double sum = 0;
            for (var i = 0; i < n; i++)
                sum += x[i] * Math.Cos(Math.PI * (2 * i + 1) * k / (2.0 * n));
            var y = 2.0 * sum;
            y *= k == 0 ? Math.Sqrt(1.0 / (4.0 * n)) : Math.Sqrt(1.0 / (2.0 * n));
            result[k] = (float)y;
        }
        return result;
    }
}

/// <summary>Minimal iterative radix-2 Cooley-Tukey FFT (in place). Input length must be a power of two.</summary>
internal static class Fft
{
    public static void Forward(Complex[] buf)
    {
        var n = buf.Length;
        if (n <= 1) return;
        if ((n & (n - 1)) != 0)
            throw new ArgumentException("FFT length must be a power of two.");

        // Bit-reversal permutation.
        for (int i = 1, j = 0; i < n; i++)
        {
            var bit = n >> 1;
            for (; (j & bit) != 0; bit >>= 1)
                j ^= bit;
            j ^= bit;
            if (i < j) (buf[i], buf[j]) = (buf[j], buf[i]);
        }

        for (var len = 2; len <= n; len <<= 1)
        {
            var ang = -2 * Math.PI / len;
            var wLen = new Complex(Math.Cos(ang), Math.Sin(ang));
            for (var i = 0; i < n; i += len)
            {
                var w = Complex.One;
                for (var k = 0; k < len / 2; k++)
                {
                    var u = buf[i + k];
                    var v = buf[i + k + len / 2] * w;
                    buf[i + k] = u + v;
                    buf[i + k + len / 2] = u - v;
                    w *= wLen;
                }
            }
        }
    }
}
