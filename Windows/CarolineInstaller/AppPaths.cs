namespace CarolineInstaller;

/// <summary>
/// Install layout, all per-user under %LocalAppData%\Caroline -- no admin
/// rights needed, nothing touches Program Files or the machine-wide PATH.
///
///   Caroline\
///     app\            the app itself (Caroline.exe, backend\, wwwroot\) --
///                      this is what gets overwritten on update
///     workspace\       created independently by the backend at runtime
///                      (chat history, .mcp.json, persona.json, ...) --
///                      the installer never touches this
///     runtime\node\    isolated Node.js, used only by Caroline's own
///                      backend -- never added to PATH, never conflicts
///                      with a system Node.js install
///     runtime\python\  isolated embeddable Python, same idea
///     runtime\git\     isolated PortableGit -- gives Claude Code's Bash
///                      tool a bash.exe on machines without Git for
///                      Windows already installed
///     Caroline-download.zip   transient, deleted after a successful extract
/// </summary>
internal static class AppPaths
{
    /// <summary>
    /// Every runtime dependency (Node, Python, Git Bash) is mirrored here
    /// rather than fetched from its own upstream project at
    /// install time -- confirmed live this was a real bus-factor risk: any
    /// one of those projects renaming a release asset, restructuring their
    /// download layout, or shutting down entirely would silently break
    /// every future Caroline install with zero warning. Update the mirrored
    /// copy (and the pinned sha256 in that dependency's own Installer.cs)
    /// deliberately when bumping a pinned version -- see each Installer's
    /// own Version constant.
    /// </summary>
    public const string DependencyMirrorBaseUrl = "https://downloader.multi-portal.org/apps/caroline/deps";


    public static string Root { get; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Caroline");

    // Backed by state.json's ActiveAppDir (see InstallState.cs), not a fixed literal --
    // per explicit instruction (2026-09-06): each install/update now lands in its own
    // never-before-referenced app-* directory (see Program.cs's ExtractWithRetryAsync)
    // instead of deleting and re-extracting into one fixed "app" folder, since a
    // lingering handle on that fixed directory (from a just-killed process, an AV scan,
    // etc.) could -- confirmed live, repeatedly -- outlast even a generous retry budget.
    // Reads fresh every call (state.json is tiny) so it always reflects whichever
    // install is currently active, including one just switched to earlier in this same
    // run. Defaults to "app" when state.json doesn't exist yet, matching every install
    // from before this scheme existed.
    public static string AppDir => Path.Combine(Root, InstallStateStore.Load().ActiveAppDir);
    public static string ClientExe => Path.Combine(AppDir, "Caroline.exe");
    public static string BackendDir => Path.Combine(AppDir, "backend");
    public static string DownloadZipPath => Path.Combine(Root, "Caroline-download.zip");

    /// <summary>
    /// Visual Mode's talking-head models (see Caroline's backend visualMode.ts /
    /// BackendProcess.cs's CAROLINE_MODELS_DIR env var) -- a sibling of AppDir, not
    /// inside it: AppDir gets fully deleted and recreated on every app update
    /// (see Program.cs's extraction step), and these are tens of GB each, so they
    /// must survive that. See ModelsInstaller.cs for the download step itself.
    /// </summary>
    public static string ModelsDir => Path.Combine(Root, "art", "models");

    public static string RuntimeDir => Path.Combine(Root, "runtime");
    public static string NodeDir => Path.Combine(RuntimeDir, "node");
    public static string NodeExe => Path.Combine(NodeDir, "node.exe");
    public static string PythonDir => Path.Combine(RuntimeDir, "python");
    public static string PythonExe => Path.Combine(PythonDir, "python.exe");
    public static string GitDir => Path.Combine(RuntimeDir, "git");
    public static string GitBashExe => Path.Combine(GitDir, "bin", "bash.exe");
    /// <summary>Static, single-file ffmpeg.exe -- Visual Mode's XcfaRenderer (audio decode +
    /// video encode) and the voice pipeline both shell out to it. Bundled here (see
    /// FfmpegInstaller.cs) instead of relying on a bare "ffmpeg" PATH lookup, which silently
    /// breaks Visual Mode entirely on a machine that doesn't happen to already have ffmpeg
    /// installed -- confirmed as a real gap (2026-09-03), not just a theoretical one.</summary>
    public static string FfmpegDir => Path.Combine(RuntimeDir, "ffmpeg");
    public static string FfmpegExe => Path.Combine(FfmpegDir, "ffmpeg.exe");

    public static void EnsureRootExists() => Directory.CreateDirectory(Root);
}
