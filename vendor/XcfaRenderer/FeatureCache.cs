using System.Security.Cryptography;
using System.Text;

namespace XcfaRenderer;

/// <summary>
/// On-disk cache of computed audio feature streams, keyed by a hash of the
/// raw PCM plus the extraction parameters -- direct port of the caching
/// half of _start_audio_feat_stream (_feat_cache_dir/_evict_feat_cache +
/// the cache-hit/cache-save blocks inline in that function). Skips
/// recomputing MFCC/centroid/bandwidth entirely when the same audio clip is
/// rendered again against the same fps/model.
///
/// The Python original's cache directory is Config.STORAGE/temp/
/// audio_feat_cache -- Config.STORAGE is reforce's own settings store, not
/// something a standalone library can reach into, so this uses the OS temp
/// directory instead (see AudioFeatures.DefaultFeatureCacheDir). The on-disk
/// FORMAT is also necessarily different (a self-contained binary layout
/// here vs. numpy's .npz there) since nothing outside this library ever
/// needs to read these files -- it's an independent cache store serving the
/// same purpose, not a shared one.
/// </summary>
internal static class FeatureCache
{
    private const uint Magic = 0x58434641; // "XCFA" as uint32 LE-ish tag, just a sentinel

    /// <summary>Direct port of _start_audio_feat_stream's cache key: sha1(audio bytes) then sha1.update(params string).</summary>
    public static string ComputeKey(float[] audioRaw, int nFrames, int fps, int windowSamples, int audioSr, float sampleFps, int leadMs)
    {
        using var sha1 = IncrementalHash.CreateHash(HashAlgorithmName.SHA1);
        var audioBytes = new byte[audioRaw.Length * sizeof(float)];
        Buffer.BlockCopy(audioRaw, 0, audioBytes, 0, audioBytes.Length);
        sha1.AppendData(audioBytes);
        sha1.AppendData(Encoding.UTF8.GetBytes($"{nFrames}:{fps}:{windowSamples}:{audioSr}:{sampleFps}:{leadMs}"));
        return Convert.ToHexString(sha1.GetHashAndReset()).ToLowerInvariant();
    }

    public static bool TryLoad(string cacheDir, string key, int nFrames, out float[][]? feats, out float[]? energies)
    {
        feats = null;
        energies = null;
        var path = Path.Combine(cacheDir, key + ".xcfafeat");
        if (!File.Exists(path)) return false;

        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read);
            using var br = new BinaryReader(fs);
            if (br.ReadUInt32() != Magic) return false;
            var storedN = br.ReadInt32();
            if (storedN != nFrames) return false;

            var loadedFeats = new float[nFrames][];
            for (var i = 0; i < nFrames; i++)
            {
                var row = new float[16];
                for (var d = 0; d < 16; d++) row[d] = br.ReadSingle();
                loadedFeats[i] = row;
            }
            var loadedEnergies = new float[nFrames];
            for (var i = 0; i < nFrames; i++) loadedEnergies[i] = br.ReadSingle();

            feats = loadedFeats;
            energies = loadedEnergies;
            return true;
        }
        catch
        {
            return false; // corrupt/partial cache file -- recompute instead of failing the render
        }
    }

    public static void Save(string cacheDir, string key, float[][] feats, float[] energies)
    {
        Directory.CreateDirectory(cacheDir);
        var finalPath = Path.Combine(cacheDir, key + ".xcfafeat");
        var tmpPath = finalPath + ".tmp";

        using (var fs = new FileStream(tmpPath, FileMode.Create, FileAccess.Write))
        using (var bw = new BinaryWriter(fs))
        {
            bw.Write(Magic);
            bw.Write(feats.Length);
            foreach (var row in feats)
                for (var d = 0; d < 16; d++) bw.Write(row[d]);
            foreach (var e in energies) bw.Write(e);
        }
        File.Move(tmpPath, finalPath, overwrite: true);

        Evict(cacheDir);
    }

    /// <summary>Deletes oldest cache files beyond maxFiles. Direct port of _evict_feat_cache.</summary>
    public static void Evict(string cacheDir, int maxFiles = 200)
    {
        try
        {
            var files = new DirectoryInfo(cacheDir).GetFiles("*.xcfafeat");
            if (files.Length <= maxFiles) return;
            foreach (var f in files.OrderBy(f => f.LastWriteTimeUtc).Take(files.Length - maxFiles))
            {
                try { f.Delete(); } catch { /* best effort */ }
            }
        }
        catch { /* best effort -- cache eviction failure must never break a render */ }
    }
}
