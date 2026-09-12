namespace CarolineInstaller;

/// <summary>
/// Per explicit instruction (2026-09-12): every setup failure, whatever
/// step it happens in, must show a MessageBox with an ERROR CODE plus a
/// human description -- not just a bare exception message. A support
/// conversation ("it failed") is useless without knowing WHICH step broke;
/// a code ("E07") is something a user can read off the dialog and report,
/// and something we can grep for immediately.
///
/// RunCriticalSectionAsync wraps each major step so any exception it
/// throws (network, disk, a third-party installer's own failure, a bug)
/// surfaces with that step's code -- see Program.cs's WithStepAsync. An
/// exception that reaches Program.Main's own top-level catch WITHOUT
/// already being an InstallerException (a bug somewhere not wrapped, or a
/// framework-level failure before any step even started) still gets a
/// code -- ErrorCodes.Unexpected -- so literally nothing shows a bare,
/// code-less message.
/// </summary>
internal sealed class InstallerException : Exception
{
    public string Code { get; }

    public InstallerException(string code, string message, Exception? inner = null) : base(message, inner) => Code = code;

    /// <summary>Formatted exactly as shown in the failure MessageBox and logged.</summary>
    public string Formatted => $"[{Code}] {Message}";
}

/// <summary>One stable, short code per setup step -- keep these stable across
/// versions once shipped (a user or support conversation may reference one
/// long after the build that produced it).</summary>
internal static class ErrorCodes
{
    public const string CleanupPreviousInstall = "E01";
    public const string NodeInstall = "E02";
    public const string PythonInstall = "E03";
    public const string GitBashInstall = "E04";
    public const string FfmpegInstall = "E05";
    public const string WebView2Install = "E06";
    public const string VersionCheck = "E07";
    public const string Download = "E08";
    public const string Extraction = "E09";
    public const string PlaywrightInstall = "E10";
    public const string ModelsInstall = "E11";
    public const string ShortcutOrAutostart = "E12";
    public const string Launch = "E13";
    public const string LaunchCrashed = "E14";
    /// <summary>Anything not wrapped by a specific step -- a bug, not a foreseen failure mode.</summary>
    public const string Unexpected = "E99";
}
