// STT/TTS via Camerlengo API v2 (ai:tts / ai:stt) -- see d:/REPO/reforce/API/Api2AICommands.py.
// v2 has a separate command processor/registry/auth model from the legacy
// ".command" protocol other tools in this repo still use: the discriminator
// is the JSON body having an undotted "command" key instead of ".command"
// (see reforce's Camerlengo.py do_POST), and v2 keys are scope-based, issued
// via Api2Auth.issue_key -- this key was minted specifically for Caroline
// with ("ai:tts", None)/("ai:stt", None) scope, it will NOT work against the
// legacy API and the old legacy key will NOT work here.
//
// Proxied through the backend rather than called directly from the WebView2
// page: keeps the API key out of client-side JS, and avoids any CORS
// uncertainty calling a remote origin from a file:// page.
import { fetchWithRetry } from "./httpRetry.js";
import { localTtsUrl } from "./localTtsServer.js";

const API_URL = "https://www.squirrelwisdom.com/";
const API_KEY = "QvR-sujLOgpKWZ-yhSOK5ZNgEe4sgF0EUU7GexQqr4M";

interface ApiEnvelope {
  ".status": "ok" | "error";
  [key: string]: unknown;
}

async function callApi(body: Record<string, unknown>, timeoutMs: number): Promise<ApiEnvelope> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchWithRetry(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: API_KEY, ...body }),
      signal: controller.signal,
    });
    return (await res.json()) as ApiEnvelope;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * `session` (a v2 session token, see subscriptionMode.ts's getV2Session())
 * is optional here for backward compat, but when provided lets Camerlengo
 * bill this call against that user's SquirrelWisdom wallet (see reforce's
 * Api2AICommands.py _bill_usage/_check_balance_or_error) -- omitting it
 * keeps the call scope-gated-only/unbilled, same as before this existed.
 */
export async function transcribeAudio(audioBase64: string, format: string, session?: string): Promise<string> {
  console.error(`[caroline] [voice] transcribeAudio: format=${format} audioBytes=${audioBase64.length} session=${session ? "yes" : "no"}`);
  const data = await callApi({ command: "ai:stt", audio: audioBase64, format, ...(session ? { session } : {}) }, 60_000);
  if (data[".status"] !== "ok" || typeof data.result !== "string") {
    if ((data as any)[".errcode"] === "402") {
      throw new Error("insufficient_balance");
    }
    throw new Error(String((data as any)[".reason"] ?? "STT failed"));
  }
  console.error(`[caroline] [voice] transcribeAudio: ok, textLen=${data.result.length}`);
  return data.result;
}

/**
 * Picks a voice matching the persona's gender field -- free text (English or
 * Russian, since the Settings field is a plain input), so this matches on
 * substrings rather than an exact enum. Defaults to the female voice
 * (matching DEFAULT_PERSONA in persona.ts) for anything ambiguous.
 */
export function voiceForGender(gender: string): string {
  const g = gender.trim().toLowerCase();
  if (g.startsWith("male") || g.startsWith("муж")) return "Onyx";
  return "Nova";
}

// Mirrors reforce's own rewriteAbbreviationsForSpeech (SHRDialogEngine/Adapters/
// BotAdapter.py) regex pre-filter: skip the LLM call entirely when the text plainly
// has nothing for it to fix -- no markdown/HTML syntax, no digits -- so a typical
// short, already-plain-prose reply doesn't pay an extra LLM round-trip on every
// single TTS call.
const NEEDS_TTS_CLEANUP = /[*_`#|~]|<[a-z][^>]*>|\d/i;

/**
 * Strips markdown/HTML formatting and rewrites digits/numerals into their spoken,
 * correctly-inflected form before text reaches TTS/Visual Mode -- per explicit
 * instruction (2026-09-03), following the same pattern reforce's own audiobook
 * pipeline already uses for TTS-friendliness (BotAdapter.py's _llm_simplify, used
 * as a synthesis-retry pass) and abbreviation pronunciation (rewriteAbbreviationsForSpeech,
 * with its careful grammatical-case-agreement rules): a single, narrowly-scoped LLM
 * rewrite via the same ai:resolve command those use (see reforce's Api2AICommands.py),
 * not a hand-rolled regex/markdown parser -- numeral case agreement (Russian "5 книг" ->
 * "пяти книг" vs "с пятью книгами" depending on context) and mixed markdown/HTML syntax
 * aren't reliably fixable with substitution rules alone.
 *
 * Best-effort: any failure (network, malformed response) falls back to the ORIGINAL
 * text unchanged rather than blocking playback -- same reasoning as every other
 * best-effort LLM-assisted rewrite in this codebase (detectLanguage, describeImageCheap).
 */
export async function cleanTextForSpeech(text: string, session?: string): Promise<string> {
  if (!NEEDS_TTS_CLEANUP.test(text)) return text;
  try {
    const prompt =
      "Rewrite the following text so it is ready to be read aloud by a text-to-speech engine. " +
      "Perform exactly these transformations, nothing else:\n" +
      "- Strip ALL markdown formatting (**bold**, *italic*, `code`, # headers, - / * bullet " +
      "lists, [links](url), | tables |, --- rules, > quotes) down to plain spoken prose -- " +
      "keep the words, drop the syntax.\n" +
      "- Strip ALL HTML tags the same way -- keep the text content, drop the markup.\n" +
      "- Rewrite every digit/numeral (dates, quantities, times, ordinals, etc.) as the words " +
      "a native speaker would actually SAY aloud, correctly inflected/declined for its exact " +
      "grammatical role in the sentence (case, number, gender, as the language requires) -- " +
      'not just the bare nominative/cardinal form. Example in Russian: "5 книг" -> "пяти книг", ' +
      '"с 5 книгами" -> "с пятью книгами", "2024 год" -> "две тысячи двадцать четвёртый год".\n' +
      "- Keep the exact same language and meaning. Do not translate, summarize, add, or remove " +
      "any information. Do not add commentary, explanations, or quotation marks around the result.\n" +
      "- Return ONLY the rewritten text, nothing else.\n\n" +
      `Text:\n${text}`;
    const data = await callApi({ command: "ai:resolve", question: prompt, ...(session ? { session } : {}) }, 30_000);
    if (data[".status"] !== "ok" || typeof data.result !== "string") {
      console.error(`[caroline] [voice] cleanTextForSpeech: ai:resolve failed, using original text: ${(data as any)[".reason"] ?? JSON.stringify(data)}`);
      return text;
    }
    const cleaned = data.result.trim();
    return cleaned || text;
  } catch (err) {
    console.error("[caroline] [voice] cleanTextForSpeech: threw, using original text:", err);
    return text;
  }
}

/**
 * Consults the "LARGE" model category (see reforce's AI.py resolveModelCategory/
 * initModelCategories -- GPT-5-class, auto-selected at Camerlengo startup, not a
 * pinned id) for advice on wording a response to a legal/commercial/social question --
 * per explicit instruction (2026-09-04): a GPT-5-class model is measurably better at
 * this careful, nuanced non-technical phrasing than Claude Code is, the same way
 * Claude Code is the better one at actual code. This is ADVICE for the caller to weigh
 * and incorporate into their own final answer, not a replacement response -- see
 * consultTools.ts's own tool description for exactly when this is meant to be used.
 * `session` is passed through the same way every other ai:* call here does (ai:resolve
 * itself has no billing hook today, so this is forward-compatible rather than currently
 * load-bearing) -- see Api2AICommands.py's cmdV2Resolve.
 */
export async function consultLargeModel(question: string, session?: string): Promise<string> {
  console.error(`[caroline] [voice] consultLargeModel: questionLen=${question.length}`);
  const data = await callApi({ command: "ai:resolve", question, model: "LARGE", ...(session ? { session } : {}) }, 60_000);
  if (data[".status"] !== "ok" || typeof data.result !== "string") {
    throw new Error(String((data as any)[".reason"] ?? "consult_large_model (ai:resolve) failed"));
  }
  console.error(`[caroline] [voice] consultLargeModel: ok, resultLen=${data.result.length}`);
  return data.result;
}

// Edge-tts (Microsoft's per-locale Neural voices) needs a specific voice per
// language -- unlike Camerlengo's Nova/Onyx, which are multilingual on their own
// and need no such table. Covers the two languages Caroline actually operates in
// (see resolveGreetingLanguage et al.); anything else falls back to the English
// pair rather than failing outright. cameroVoice is always exactly "Nova" or
// "Onyx" (see voiceForGender's own two possible returns), reversed here into
// female/male rather than threading a second gender-only parameter through every
// caller for a single internal lookup.
const EDGE_TTS_VOICES: Record<string, { female: string; male: string }> = {
  en: { female: "en-US-AriaNeural", male: "en-US-GuyNeural" },
  ru: { female: "ru-RU-SvetlanaNeural", male: "ru-RU-DmitryNeural" },
};

function edgeTtsVoiceFor(cameroVoice: string, language: string | null): string {
  const table = EDGE_TTS_VOICES[language ?? ""] ?? EDGE_TTS_VOICES.en;
  return cameroVoice === "Onyx" ? table.male : table.female;
}

async function synthesizeSpeechLocally(text: string, voice: string): Promise<string> {
  const res = await fetch(localTtsUrl(), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, voice }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error(`local TTS server returned ${res.status}: ${(await res.text()).slice(0, 300)}`);
  return Buffer.from(await res.arrayBuffer()).toString("base64");
}

async function synthesizeSpeechViaCamerlengo(text: string, voice: string, session?: string): Promise<string> {
  console.error(`[caroline] [voice] synthesizeSpeechViaCamerlengo: voice=${voice} textLen=${text.length} session=${session ? "yes" : "no"}`);
  const data = await callApi({ command: "ai:tts", text, voice, ...(session ? { session } : {}) }, 60_000);
  if (data[".status"] !== "ok" || typeof data.result !== "string") {
    if ((data as any)[".errcode"] === "402") {
      throw new Error("insufficient_balance");
    }
    throw new Error(String((data as any)[".reason"] ?? "TTS failed"));
  }
  console.error(`[caroline] [voice] synthesizeSpeechViaCamerlengo: ok, audioBytes=${data.result.length}`);
  return data.result; // base64 MP3
}

/**
 * Text-to-speech, preferring the local edge-tts server (see localTtsServer.ts) over
 * Camerlengo's ai:tts. Per explicit instruction (2026-09-07): this is purely a
 * latency optimization (no Camerlengo round trip, no per-call Python startup since
 * the local server is already running) -- not a cost one, so it's tried FIRST
 * regardless of whether a SquirrelWisdom session is available, and falls back to
 * the exact same Camerlengo call as before whenever the local attempt fails for any
 * reason (an install predating this feature / a Python or edge-tts setup failure /
 * a transient error), so this is never a hard new dependency.
 */
export async function synthesizeSpeech(text: string, voice = "Nova", session?: string): Promise<string> {
  const language = await detectLanguage(text);
  const edgeVoice = edgeTtsVoiceFor(voice, language);
  try {
    const result = await synthesizeSpeechLocally(text, edgeVoice);
    console.error(`[caroline] [voice] synthesizeSpeech: local edge-tts ok, voice=${edgeVoice} audioBytes=${result.length}`);
    return result;
  } catch (err) {
    console.error(`[caroline] [voice] synthesizeSpeech: local edge-tts failed, falling back to Camerlengo: ${(err as Error).message}`);
    return synthesizeSpeechViaCamerlengo(text, voice, session);
  }
}

/**
 * Real LLM-backed language detection (Camerlengo's ai:detectLanguage, see
 * reforce's API/Api2AICommands.py cmdV2DetectLanguage / AI.py's
 * detectLanguage) -- deliberately NOT a Cyrillic/Latin heuristic: per
 * explicit instruction, language detection must go through the real API
 * everywhere in this codebase, not a regex guess. Returns a lowercase ISO
 * 639-1 code (e.g. "ru", "en"), or null if the API call itself fails or
 * genuinely can't tell (both treated identically by callers -- see
 * server.ts's resolveGreetingLanguage, which falls back to English for
 * either case, per explicit instruction).
 */
export async function detectLanguage(text: string): Promise<string | null> {
  try {
    const data = await callApi({ command: "ai:detectLanguage", text }, 15_000);
    if (data[".status"] !== "ok" || typeof data.language !== "string") return null;
    const iso = data.language.trim().toLowerCase();
    return iso && iso !== "unknown" ? iso : null;
  } catch (err) {
    console.error("[caroline] [voice] detectLanguage failed:", err);
    return null;
  }
}

export interface CheapImageDescription {
  description: string;
  objects?: unknown;
  palette?: unknown;
}

/**
 * Cheap image understanding (Camerlengo's ai:describeImage -- reforce's
 * AI.py describeImage(), a small model, gpt-4o-mini by default) as a
 * deliberately CHEAPER alternative to putting the raw image bytes into
 * Claude's own context (an image content block costs real vision tokens on
 * every single call). Per explicit instruction: prefer this whenever exact
 * pixel/element coordinates aren't needed -- only escalate to a real
 * screenshot content block (app_browser_screenshot et al.) when this isn't
 * enough (need to actually see/click something precisely). Currently has no
 * billing hook on the reforce side at all (confirmed by reading
 * Api2AICommands.py directly) -- unmetered regardless of subscription
 * state, so this is strictly a token-cost optimization, not something
 * gated behind an active paid balance.
 */
export async function describeImageCheap(base64Png: string): Promise<CheapImageDescription> {
  const data = await callApi({ command: "ai:describeImage", content: base64Png, type: "base64" }, 30_000);
  if (data[".status"] !== "ok" || typeof data.result !== "object" || data.result === null) {
    throw new Error(String((data as any)[".reason"] ?? "describeImage failed"));
  }
  const r = data.result as any;
  return { description: String(r.description ?? ""), objects: r.objects, palette: r.palette };
}
