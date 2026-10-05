using System;
using System.IO;

namespace Caroline.Native;

/// <summary>
/// Linux analog of the Windows App.xaml.cs's named Mutex -- a file lock
/// (flock, via FileStream's own FileShare.None exclusive open) under the
/// Linux workspace dir. Unlike a named kernel Mutex, an flock is
/// automatically released by the kernel if this process dies without
/// calling Dispose() (crash, SIGKILL) -- no stale-lock cleanup logic
/// needed, which a Windows named Mutex already gets for free too (abandoned
/// mutex detection), so behavior matches.
/// </summary>
public sealed class SingleInstanceGuard : IDisposable
{
    private readonly FileStream? _lockFile;

    public bool IsFirstInstance { get; }

    public SingleInstanceGuard()
    {
        var xdgDataHome = Environment.GetEnvironmentVariable("XDG_DATA_HOME");
        var dataHome = !string.IsNullOrEmpty(xdgDataHome)
            ? xdgDataHome
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".local", "share");
        var dir = Path.Combine(dataHome, "caroline");
        Directory.CreateDirectory(dir);
        var lockPath = Path.Combine(dir, "caroline.lock");

        try
        {
            // FileShare.None on an existing open handle is what actually
            // enforces exclusivity here (a second process's own attempt to
            // open the same path this way throws IOException) -- equivalent
            // in effect to flock(LOCK_EX | LOCK_NB), without needing a
            // P/Invoke to the real flock(2) syscall.
            _lockFile = new FileStream(lockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            IsFirstInstance = true;
        }
        catch (IOException)
        {
            IsFirstInstance = false;
        }
    }

    public void Dispose()
    {
        _lockFile?.Dispose();
    }
}
