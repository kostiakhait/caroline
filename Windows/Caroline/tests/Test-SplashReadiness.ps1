$ErrorActionPreference = 'Stop'
$source = Get-Content (Join-Path $PSScriptRoot '../SplashReadiness.cs') -Raw
# Compile the production predicate in isolation from WPF. Its visibility is
# widened only inside this in-memory harness so PowerShell can call it.
$source = $source.Replace('internal static class SplashReadiness', 'public static class SplashReadiness')
$source = $source.Replace('internal static bool IsReady', 'public static bool IsReady')
Add-Type -TypeDefinition $source -Language CSharp

$expected = [string[]]@('1', '2')
$cases = @(
    @{ Name = 'no WebViews connected'; Body = '{"ok":true,"connected":false,"tabs":[]}'; Want = $false },
    @{ Name = 'second restored tab absent'; Body = '{"ok":true,"connected":true,"tabs":[{"tabId":"1","hasSeenInit":true,"forcedCompactionPending":false}]}'; Want = $false },
    @{ Name = 'second tab registered before SDK init'; Body = '{"ok":true,"tabs":[{"tabId":"1","hasSeenInit":true,"forcedCompactionPending":false},{"tabId":"2","hasSeenInit":false,"forcedCompactionPending":true}]}'; Want = $false },
    @{ Name = 'second tab compacting'; Body = '{"ok":true,"tabs":[{"tabId":"1","hasSeenInit":true,"forcedCompactionPending":false},{"tabId":"2","hasSeenInit":true,"forcedCompactionPending":true}]}'; Want = $false },
    @{ Name = 'both restored tabs ready'; Body = '{"ok":true,"tabs":[{"tabId":"1","hasSeenInit":true,"forcedCompactionPending":false},{"tabId":"2","hasSeenInit":true,"forcedCompactionPending":false}]}'; Want = $true },
    @{ Name = 'wrong tab ID at same count'; Body = '{"ok":true,"tabs":[{"tabId":"1","hasSeenInit":true,"forcedCompactionPending":false},{"tabId":"3","hasSeenInit":true,"forcedCompactionPending":false}]}'; Want = $false },
    @{ Name = 'disconnected tab still registered'; Body = '{"ok":true,"tabs":[{"tabId":"1","ended":false,"hasSeenInit":true,"forcedCompactionPending":false},{"tabId":"2","ended":true,"hasSeenInit":true,"forcedCompactionPending":false}]}'; Want = $false }
)
foreach ($case in $cases) {
    $actual = [Caroline.SplashReadiness]::IsReady($case.Body, $expected)
    if ($actual -ne $case.Want) { throw "$($case.Name): expected $($case.Want), got $actual" }
}
Write-Output "Splash readiness regressions passed: $($cases.Count) cases"
