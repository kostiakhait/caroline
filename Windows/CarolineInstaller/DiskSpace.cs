namespace CarolineInstaller;

/// <summary>
/// Per explicit instruction (2026-09-18): the installer must actually check
/// free disk space rather than let a full drive surface as a confusing,
/// late failure -- an out-of-space error mid-download/mid-extraction
/// previously showed up as a bare IOException wrapped in whichever step
/// happened to be running (Download/Extraction/ModelsInstall), giving no
/// hint that "free up space" was the actual fix. This makes that failure
/// mode an explicit, early, actionable check instead.
/// </summary>
internal static class DiskSpace
{
    public static long GetAvailableFreeBytes(string path)
    {
        var root = Path.GetPathRoot(Path.GetFullPath(path));
        if (string.IsNullOrEmpty(root))
        {
            // Can't determine a drive for this path -- fail open (return a
            // huge number) rather than block install on something we can't
            // actually verify; every real Windows path has a drive root, so
            // this is a defensive fallback, not an expected case.
            Logger.Log($"DiskSpace: could not resolve a drive root for '{path}' -- skipping the check for it");
            return long.MaxValue;
        }
        return new DriveInfo(root).AvailableFreeSpace;
    }

    public static string FormatGb(long bytes) => $"{bytes / 1024.0 / 1024.0 / 1024.0:F1} GB";

    /// <summary>
    /// Throws InstallerException(ErrorCodes.DiskSpaceCheck, ...) if the
    /// drive containing `path` has less than `requiredBytes` free.
    /// `context` is a short human phrase completing "Not enough free disk
    /// space to {context}."
    /// </summary>
    public static void RequireFreeSpace(string path, long requiredBytes, string context)
    {
        var available = GetAvailableFreeBytes(path);
        Logger.Log($"DiskSpace: {context} -- available={FormatGb(available)}, required={FormatGb(requiredBytes)}");
        if (available < requiredBytes)
        {
            throw new InstallerException(ErrorCodes.DiskSpaceCheck,
                $"Not enough free disk space to {context}. {FormatGb(available)} available on this drive, at least "
                + $"{FormatGb(requiredBytes)} needed. Free up some space and run Setup again.");
        }
    }
}
