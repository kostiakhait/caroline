/**
 * Node's global fetch() (undici) pools keep-alive connections -- in a
 * long-running process (this backend can run for hours), a pooled
 * connection can go stale server-side and every subsequent request on it
 * fails with a generic network-level "fetch failed" error, indefinitely,
 * until the process restarts. Confirmed live: a fresh one-off script
 * hitting the exact same SquirrelWisdom endpoint at the same moment
 * worked immediately, while this long-lived backend kept failing on every
 * attempt -- the process itself wasn't broken, just its reused connection.
 * A retry opens a fresh connection and succeeds, which is what lets
 * login/office-editing/TTS/STT recover on their own instead of needing
 * the whole app restarted (see MCP/notes/src/api.ts's own copy of this,
 * used by the separately-spawned Notes MCP server process).
 *
 * Only retries a genuine fetch() throw (DNS/connection-level failure) --
 * an actual HTTP error response (4xx/5xx) is a real answer from the
 * server, not a stale-connection symptom, so callers see that immediately
 * via the normal `!res.ok` check, not retried here. Also does not retry an
 * AbortError: a caller-supplied `init.signal` firing is a deliberate
 * cancellation/timeout, not a stale-connection symptom either, and
 * retrying it would silently ignore the caller's own timeout budget
 * (see voice.ts's callApi, which wraps calls in its own AbortController).
 */
export async function fetchWithRetry(url: string, init?: RequestInit, retries = 2): Promise<Response> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return await fetch(url, init);
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") throw err;
      lastErr = err;
      console.error(`[caroline] [httpRetry] fetch attempt ${attempt + 1}/${retries + 1} to ${url} failed:`, err);
      if (attempt < retries) await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
    }
  }
  console.error(`[caroline] [httpRetry] all ${retries + 1} attempts to ${url} failed, giving up`);
  throw lastErr;
}
