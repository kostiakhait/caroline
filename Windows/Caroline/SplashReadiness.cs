using System;
using System.Collections.Generic;
using System.Text.Json;

namespace Caroline;

internal static class SplashReadiness
{
    internal static bool IsReady(string statusBody, IReadOnlyList<string> expectedTabIds)
    {
        if (expectedTabIds.Count == 0) return false;
        using var doc = JsonDocument.Parse(statusBody);
        var root = doc.RootElement;
        if (!root.TryGetProperty("ok", out var ok) || ok.ValueKind != JsonValueKind.True ||
            !root.TryGetProperty("tabs", out var tabs) || tabs.ValueKind != JsonValueKind.Array)
        {
            return false;
        }

        var remaining = new HashSet<string>(expectedTabIds, StringComparer.Ordinal);
        foreach (var tab in tabs.EnumerateArray())
        {
            if (!tab.TryGetProperty("tabId", out var tabId) || tabId.ValueKind != JsonValueKind.String ||
                !remaining.Contains(tabId.GetString()!))
            {
                continue;
            }
            if (!tab.TryGetProperty("hasSeenInit", out var initialized) || initialized.ValueKind != JsonValueKind.True ||
                (tab.TryGetProperty("ended", out var ended) && ended.ValueKind == JsonValueKind.True) ||
                !tab.TryGetProperty("forcedCompactionPending", out var compacting) || compacting.ValueKind != JsonValueKind.False)
            {
                return false;
            }
            remaining.Remove(tabId.GetString()!);
        }
        return remaining.Count == 0;
    }
}
