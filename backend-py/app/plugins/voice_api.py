"""Ports backend/src/voice.ts -- TTS/STT/language-detection/cheap-image-
description via Camerlengo, plus the local edge-tts fallback path for TTS.
Used by app/main.py's tts/stt control_request ops and by
app_browser_plugin.py's app_browser_describe.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from app.logging_setup import log_event
from app.plugins.sw_api import CAROLINE_SW_KEY, _post_json

LOCAL_TTS_PORT = 9414


def local_tts_url() -> str:
    return f"http://127.0.0.1:{LOCAL_TTS_PORT}/tts"


class VoiceApiError(Exception):
    pass


async def transcribe_audio(audio_base64: str, fmt: str, session: str | None = None) -> str:
    body: dict[str, Any] = {"command": "ai:stt", "key": CAROLINE_SW_KEY, "audio": audio_base64, "format": fmt}
    if session:
        body["session"] = session
    data = await _post_json(body)
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        if data.get(".errcode") == "402":
            raise VoiceApiError("insufficient_balance")
        raise VoiceApiError(str(data.get(".reason") or "STT failed"))
    return data["result"]


def voice_for_gender(gender: str) -> str:
    """Picks a voice matching the persona's gender field -- free text
    (English or Russian), so this matches on substrings rather than an
    exact enum. Defaults to the female voice for anything ambiguous."""
    g = gender.strip().lower()
    if g.startswith("male") or g.startswith("муж"):
        return "Onyx"
    return "Nova"


# Edge-tts (Microsoft's per-locale Neural voices) needs a specific voice
# per language -- unlike Camerlengo's Nova/Onyx, which are multilingual on
# their own and need no such table.
_EDGE_TTS_VOICES = {
    "en": {"female": "en-US-AriaNeural", "male": "en-US-GuyNeural"},
    "ru": {"female": "ru-RU-SvetlanaNeural", "male": "ru-RU-DmitryNeural"},
}


def _edge_tts_voice_for(camero_voice: str, language: str | None) -> str:
    table = _EDGE_TTS_VOICES.get(language or "", _EDGE_TTS_VOICES["en"])
    return table["male"] if camero_voice == "Onyx" else table["female"]


async def detect_language(text: str) -> str | None:
    """Real LLM-backed language detection (Camerlengo's ai:detectLanguage)
    -- deliberately NOT a Cyrillic/Latin heuristic. Returns a lowercase
    ISO 639-1 code, or None if the API call fails or genuinely can't tell.
    NOTE: this is the OLD, single-shot detector -- only used here to pick
    an edge-tts VOICE (low-stakes). The separate, higher-stakes "detect
    the user's own language for system-prompt steering" call site is
    intentionally NOT this function -- see the dedicated
    resolve-based-language-detection redesign plan for that one."""
    try:
        data = await _post_json({"command": "ai:detectLanguage", "key": CAROLINE_SW_KEY, "text": text})
    except Exception:
        return None
    if data.get(".status") != "ok" or not isinstance(data.get("language"), str):
        return None
    iso = data["language"].strip().lower()
    return iso if iso and iso != "unknown" else None


async def resolve_user_language(recent_text: str, session: str | None = None) -> str | None:
    """Redesign (2026-09-09, see the resolve-based-language-detection plan):
    determines what language the USER (not Caroline) is writing in, via
    ai:resolve (the same general-purpose command clean_text_for_speech
    already uses) rather than ai:detectLanguage -- returns the language's
    own English name (e.g. "Russian", "Spanish"), not an ISO code, and is
    NOT collapsed to a ru/en binary. Deliberately no artificial local
    timeout here -- only ever called fire-and-forget from chat_session.py's
    refresh_language_in_background, never awaited synchronously in a
    user-facing path."""
    prompt = (
        "Determine what language the USER is writing in, based on their most recent messages below (ignore any "
        "assistant/system text mixed in -- focus only on the user's own words). Reply with ONLY the language's "
        'English name (e.g. "Russian", "Spanish", "English") and nothing else -- no punctuation, no explanation.\n\n'
        f"{recent_text}"
    )
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt}
    if session:
        body["session"] = session
    try:
        data = await _post_json(body)
    except Exception as exc:
        log_event("plugin:voice", "resolve_user_language_request_failed", error=str(exc))
        return None
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        log_event("plugin:voice", "resolve_user_language_bad_response", status=data.get(".status"), reason=data.get(".reason"))
        return None
    name = data["result"].strip()
    return name or None


async def translate_text(text: str, language: str, session: str | None = None) -> str | None:
    """Per explicit instruction (2026-09-13): the target language for a
    generated piece of text (progress narration, today) must come from
    Caroline's own DEDICATED, already-continuously-refreshed language
    detector (chat_session.py's current_language_name/
    refresh_language_in_background), never from asking the SAME
    creative-generation call to also correctly infer language from
    context -- confirmed live that the small model kept defaulting to
    English regardless of a "reply in whatever language the user's lines
    are written in" instruction, once the surrounding dialogue had enough
    English technical/tool-ish content mixed in to confuse it. Run
    UNCONDITIONALLY on the final text as a forced correction pass, not
    conditionally based on a guess about whether it's "already right".

    Confirmed live, twice: (1) CAROLINE_SW_KEY's ACL rejects the dedicated
    ai:translate command outright ("Key scope/resource ACL does not
    permit this call") -- same restriction resolve_user_language() above
    already works around by going through the general-purpose ai:resolve
    instead of the more specific ai:detectLanguage; same fix here. (2)
    Once routed through ai:resolve, the SAME small-model failure modes as
    generate_progress_comment() showed up here too (a JSON-wrapped refusal
    -- "already in English... no translation can be generated" -- for a
    perfectly ordinary translation request) -- same <tag> contract +
    extraction + garbage/refusal filter as narration, not a bespoke
    lighter check. Text already in the target language should come back
    close to unchanged. Returns None on any failure OR on a garbage/
    refusal-shaped response -- callers must fall back to the original
    text, never block on this."""
    prompt = (
        f"Translate the following text into {language}. If the text is already in that language, respond with "
        "it unchanged (or only lightly cleaned up) -- do not refuse or explain, translation into the SAME "
        "language it's already in is a normal, valid case, not an error.\n\n"
        f"Text to translate:\n---\n{text}\n---\n\n"
        "Output format, follow exactly -- a program parses this, not a person: write ONLY the translated text "
        "inside a <translation> tag, nothing else anywhere in your reply -- no JSON, no markdown, no code "
        "fences, no quotes around it, no explanation.\n"
        "Example, for an unrelated hypothetical translation into French -- copy the TAG, not the words: "
        "<translation>Il pleut à Paris aujourd'hui.</translation>"
    )
    # Bug fix (2026-09-13), confirmed live: model="SMALL" (openai/gpt-5-nano)
    # just echoed the source text back unchanged instead of translating it,
    # every time -- too weak for this specific task even with a clear tag
    # contract and a correct explicit prompt. Leaving `model` unset (the
    # account's own default/ALTERNATE tier) instead reliably produced a
    # real, correct translation on the same exact input. This call exists
    # specifically to GUARANTEE correctness (see this function's own
    # docstring) -- worth the extra cost over SMALL, unlike the narration
    # draft itself above, which stays SMALL on purpose.
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt}
    if session:
        body["session"] = session
    try:
        data = await _post_json(body)
    except Exception as exc:
        log_event("plugin:voice", "translate_text_request_failed", error=str(exc))
        return None
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        log_event("plugin:voice", "translate_text_bad_response", status=data.get(".status"), reason=data.get(".reason"))
        return None
    translated = _extract_tagged_text(data["result"], "translation")
    if not translated:
        log_event("plugin:voice", "translate_text_unextractable", raw=data["result"][:300])
        return None
    if _looks_like_narration_garbage(translated, language):
        log_event("plugin:voice", "translate_text_rejected_garbage", text=translated[:300])
        return None
    return translated


# Guards against the failure modes seen live rather than trusting any
# non-empty .result blindly (screenshots, tab 2):
#  - the SMALL model REFUSING the task ("I'm sorry, I can't help with
#    that.") and that refusal getting shown as a Caroline chat bubble;
#  - a raw API/catalog-lookup error leaking straight through (the "didn't
#    find a matching entry ... in the available catalog" incident);
#  - a reply in the wrong SCRIPT entirely (Chinese "正在打开文档。" for a
#    Russian conversation);
#  - raw JSON, or a reply far longer than "one short sentence".
# Any of these -> treat as no usable response, show nothing.
_NARRATION_GARBAGE_PATTERNS = [
    re.compile(r"\bi(?:'m| am)\s+sorry\b", re.IGNORECASE),
    re.compile(r"\bi\s+(?:can(?:'t|not)|won'?t|am unable to|cannot)\s+(?:help|assist|do that|provide|comply)", re.IGNORECASE),
    re.compile(r"\bas an? (?:ai|language model)\b", re.IGNORECASE),
    re.compile(r"\bне могу (?:помочь|это сделать|выполнить)\b", re.IGNORECASE),
    re.compile(r"\bизвини(?:те)?[,.\s].{0,40}\bне могу\b", re.IGNORECASE),
    re.compile(r"\bкак (?:ИИ|языковая модель)\b", re.IGNORECASE),
    re.compile(r"\bmatching entry\b", re.IGNORECASE),
    re.compile(r"\bavailable catalog\b", re.IGNORECASE),
    re.compile(r"\berrcode\b", re.IGNORECASE),
    re.compile(r"^\s*[{\[]"),  # raw JSON/array leaking through
    # Bug fix (2026-09-13), confirmed live: a declarative "there's nothing
    # to say" meta-statement, distinct from the first-person refusals above
    # -- just as much a non-answer, still slipped through unfiltered.
    re.compile(r"\bno (?:specific )?(?:remark|response|sentence|instructions?)\b.{0,40}\b(?:found|derived|generated|drafted|identified|could be)\b", re.IGNORECASE),
    re.compile(r"\bcould not be (?:drafted|generated|derived|produced)\b", re.IGNORECASE),
    # Bug fix (2026-09-14), confirmed live (caroline.log, 21:45:37): a THIRD
    # variant of the same underlying problem -- instead of a refusal or
    # meta-commentary about being an AI, the small model sometimes writes a
    # dry third-person RECAP of the conversation ("The user asks Caroline to
    # ...  The Caroline response confirms that ...") instead of a reactive
    # remark in her own voice. Perfectly well-formed prose, not a refusal,
    # under the length cap -- none of the patterns above catch it, and it
    # went out as a real chat bubble. A genuine in-character remark never
    # refers to "the user" or "Caroline"/"the AI assistant" as a third party
    # describing what happened; this is the same tell _looks_like_narration_
    # garbage already uses for refusals, generalized to this shape too.
    re.compile(r"^\s*the user (?:asks?|asked|wants?|requests?|is asking)\b", re.IGNORECASE),
    re.compile(r"\bthe (?:caroline|ai assistant('s)?) response\b", re.IGNORECASE),
    re.compile(r"\bthe ai assistant\b", re.IGNORECASE),
]
_NARRATION_MAX_CHARS = 400
# Han / Hiragana / Katakana / Hangul. Progress narration for this product
# is only ever asked for in a European language; a CJK reply is the SMALL
# model flailing, never legitimate here.
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def _looks_like_narration_garbage(text: str, language: str = "") -> bool:
    if len(text) > _NARRATION_MAX_CHARS:
        return True
    if _CJK_RE.search(text) and not re.search(r"chin|japan|korea|mandarin|中文", language, re.IGNORECASE):
        return True
    return any(p.search(text) for p in _NARRATION_GARBAGE_PATTERNS)


def _normalized_overlap(text: str, other: str) -> bool:
    """Whitespace-collapsed, case-folded containment check either
    direction -- a near-verbatim echo, not requiring an exact match
    (paraphrases that drop/add a clause on either side still count)."""
    norm_text = re.sub(r"\s+", " ", text).strip().lower()
    norm_other = re.sub(r"\s+", " ", other).strip().lower()
    if len(norm_text) < 15 or len(norm_other) < 15:
        return False
    return norm_text in norm_other or norm_other in norm_text


def _is_echo_of_dialogue(text: str, recent_dialogue: str) -> bool:
    """A genuine narration REACTS to the conversation -- it doesn't quote it
    back. Confirmed live (2026-09-13): even with the <narration>-tag
    contract in place, the SMALL model sometimes just repeats the user's
    own last line, or Caroline's own last line, near-verbatim instead of
    producing a new reactive remark -- the mechanical garbage filter above
    has no way to catch this (the text itself is perfectly well-formed)."""
    return any(_normalized_overlap(text, line.split(":", 1)[-1]) for line in recent_dialogue.splitlines())


# The prompt's own bad/good/tag-format example sentences (built into the
# prompt string below via these same constants) -- kept as named constants
# specifically so this check can never drift out of sync with what the
# prompt actually says. Bug fix (2026-09-13), confirmed live: giving the
# model ANY concrete example sentence to illustrate the <narration> tag
# format risks it being copied back verbatim (confirmed live twice: once
# with a bare "your one sentence goes here" placeholder, once with the
# real GOOD-example sentence reused for a completely unrelated
# conversation about mailboxes) when the model is unsure/lazy rather than
# genuinely reacting to the actual dialogue -- catch that the same way as
# a dialogue echo, not by trying to word the example so carefully it can
# never be copied (that arms race isn't worth it for a cosmetic feature).
_NARRATION_EXAMPLE_BAD = "I'll create a GitLab repo and send you the link."
_NARRATION_EXAMPLE_GOOD = "Setting up a fresh repo for this is usually the fiddly part."
_NARRATION_EXAMPLE_TAG = "Movers always lowball the box count, every single time."

# Bug fix (2026-09-14), per explicit instruction: pattern-matching specific
# third-person phrasings after the fact (_NARRATION_GARBAGE_PATTERNS'
# "the user asks"/"the AI assistant" entries, added 2026-09-13) is the
# wrong primary fix -- confirmed live (screenshots) that the model finds
# NEW third-person phrasings the patterns don't happen to cover. The real
# fix has to be instructing the model correctly in the first place; the
# patterns stay only as a defense-in-depth safety net, not the mechanism
# doing the actual work. A second first-person-vs-recap example pair,
# alongside the existing promise-vs-no-promise one above, specifically
# illustrating THIS failure shape (a third-person recap of the
# conversation instead of a first-person in-character remark).
_NARRATION_EXAMPLE_THIRDPERSON_BAD = "The user asks Caroline to regenerate the report, and the Caroline response confirms the edits were made."
_NARRATION_EXAMPLE_THIRDPERSON_GOOD = "Regenerated it and reopened it for you to check."


def _is_echo_of_prompt_example(text: str) -> bool:
    return any(
        _normalized_overlap(text, example)
        for example in (
            _NARRATION_EXAMPLE_BAD, _NARRATION_EXAMPLE_GOOD, _NARRATION_EXAMPLE_TAG,
            _NARRATION_EXAMPLE_THIRDPERSON_BAD, _NARRATION_EXAMPLE_THIRDPERSON_GOOD,
        )
    )


# Bug fix (2026-09-13, confirmed live via caroline.log): a bare "reply with
# ONLY X, no preamble" instruction buried at the end of a long paragraph of
# caveats wasn't a strong enough contract for the cheap SMALL model --
# confirmed live it regularly echoed back a paraphrase of its OWN
# instructions ("(If there are no \"User:\" lines above, the default
# response is in Russian.)"), or wrapped the real sentence in JSON
# (`{"assistant": "...", "note": "..."}`) or backticks/a "Result:" prefix.
# The old garbage filter only caught JSON that started at position 0
# (`^\s*[{\[]`) -- anything wrapped in backticks, a leading word, or valid-
# but-differently-shaped JSON sailed straight through to the user as a real
# chat bubble. generate_progress_comment() now asks for the answer inside
# an explicit <narration> tag (a stronger, more parseable contract for a
# small model than prose alone) and this function extracts ONLY that tag's
# content -- falling back to unwrapping a plain {"result": "..."}-shaped
# JSON object (for a response that ignored the tag instruction but still
# came back structured), and finally to the raw text as-is for a model
# that just answered in plain prose. Never guesses past a JSON shape it
# doesn't recognize -- returns "" rather than passing raw JSON through.
_JSON_TEXT_KEYS = ("narration", "translation", "translated_text", "result", "text", "message", "assistant", "response", "comment", "answer")


def _extract_tagged_text(raw: str, tag: str) -> str:
    """Generalized (2026-09-13) from what was narration-only: the SAME
    unreliable-small-model failure modes (JSON-wrapping, backtick/"Result:"
    prefixes, echoing its own instructions) confirmed live for
    translate_text() too, not just generate_progress_comment() -- one
    shared extractor, parameterized by which XML-ish tag the prompt asked
    for. Falls back to unwrapping a plain {"result": "..."}-shaped JSON
    object (a response that ignored the tag instruction but still came
    back structured), and finally to the raw text as-is for a model that
    just answered in plain prose. Never guesses past a JSON shape it
    doesn't recognize -- returns "" rather than passing raw JSON through."""
    tag_match = re.search(rf"<{tag}>(.*?)</{tag}>", raw, re.IGNORECASE | re.DOTALL)
    if tag_match:
        return tag_match.group(1).strip()
    stripped = raw.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except Exception:
            return ""
        if isinstance(parsed, dict):
            for key in _JSON_TEXT_KEYS:
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""
    return stripped


async def generate_progress_comment(recent_dialogue: str, language: str, session: str | None = None) -> str | None:
    """Per explicit instruction (2026-09-10): Caroline has no way to
    interrupt her own main session mid-turn just to narrate progress
    without genuinely disrupting whatever she's doing (the SDK only
    delivers a new injected message once the CURRENT turn -- the whole
    tool-call chain -- has fully finished; interrupt() is a real abort,
    not a "pause and continue" signal). This sidesteps that entirely: a
    SEPARATE, lightweight ai:resolve call (model SMALL, per explicit
    instruction -- this is filler narration, not worth full price)
    drafts a short in-character remark on her behalf, sent straight to
    the client as its own chat message.

    Reverted (2026-09-11) back to "SMALL" -- the temporary openai/gpt5-nano
    bypass (added when Camerlengo's SMALL category resolved to a 403ing
    model) is no longer needed: live-tested against SMALL directly (a
    narrator-shaped prompt, an arithmetic question, and a trick question)
    and got coherent, correctly-languaged, non-garbage answers, confirming
    the category now points at a working model again.

    The real session never sees or knows about this -- it's a cosmetic
    stand-in for "I'm still working on it", not something she said or
    will remember.

    Redesigned (2026-09-10) after live evidence (screenshots) that the
    original single-question + tool-names-as-"activity" version produced
    bland, repetitive, OPERATION-sounding remarks ("I'm checking the
    phone-related messages...", "let me pull up that operation...")
    instead of something addressed to the user that actually engages with
    what's being discussed. Now takes the real recent back-and-forth
    (chat_session.py's _gather_recent_dialogue_for_narration) instead of
    a single frozen question, so each call has fresh material to react to
    and the model can speak to the actual substance instead of describing
    internal mechanics. Returns None on any failure (network, bad
    response, SW unavailable, or a response that fails the garbage check
    below) -- always a silent skip, never surfaced as an error."""
    prompt = (
        "For the next few sentences, YOU ARE Caroline, an AI assistant, writing directly to the specific "
        "person she's mid-conversation with. Not narrating about her, not describing what she or the user "
        "did -- BE her, speaking in first person, the way she'd actually type a message: \"I\", never \"the "
        "user\"/\"Caroline\"/\"the AI assistant\" as a third party. She's been quietly working on the user's "
        "last message for over a minute now without saying anything back yet. Draft ONE short remark, AS "
        "her, to keep the conversation feeling alive. This is NOT a status update about internal work -- "
        "never say things like \"I'm checking/pulling up/sorting through/looking into/working on X\", never "
        "mention tools, files, operations, or how long anything is taking. Also never expose ANY internal "
        "machinery, even in passing -- no restarts, glitches, crashes, session/turn internals, backups, "
        "snapshots, dehydration/compaction, tool or MCP-server names, workspace file paths, or anything else "
        "about how she's built or how this conversation is being kept alive behind the scenes. The dialogue "
        "below may itself contain that kind of internal-mechanics language (her own past tool calls, backup-"
        "job chatter, a prior restart note) -- that's real material she generated, not something to react to "
        "or repeat; a genuine remark never mentions it either way. Instead, react like someone "
        "genuinely engaged with the actual topic would: add a real, specific thought connected to what's "
        "being discussed -- a relevant detail, a follow-up angle, a small observation -- not a generic "
        "placeholder that could fit any conversation, and NOT a recap or summary of the conversation so far "
        "(that's not a remark, that's a report -- never write it).\n"
        f"  Bad (third person, a recap instead of a remark): \"{_NARRATION_EXAMPLE_THIRDPERSON_BAD}\"\n"
        f"  Good (first person, an actual remark): \"{_NARRATION_EXAMPLE_THIRDPERSON_GOOD}\"\n\n"
        "You have no idea what the real assistant is actually doing right now, so NEVER commit to a new "
        "action on her behalf -- no \"I'll do X\", \"I will send/create/check Y\", no new promises or plans "
        "of any kind, however small. Only react to what's ALREADY in the conversation below -- an "
        "observation, a reaction, a connection to something already said -- never something forward-looking "
        "that could turn out to be false.\n"
        f"  Bad (a new promise): \"{_NARRATION_EXAMPLE_BAD}\"\n"
        f"  Good (same situation, no promise): \"{_NARRATION_EXAMPLE_GOOD}\"\n\n"
        f"Here is the real recent conversation between her and the user (oldest first):\n---\n{recent_dialogue}\n---\n\n"
        "Write in whichever language feels most natural to draft this in -- don't spend effort trying to "
        "match the user's own language yourself, a dedicated separate step translates your draft into "
        "exactly the right language afterward regardless of what you write it in here.\n\n"
        "Output format, follow exactly -- a program parses this, not a person: write your one sentence (two "
        "at most) inside a <narration> tag, with NOTHING else anywhere in your reply -- no JSON, no markdown, "
        "no code fences, no quotes around it, no explanation, and never repeat or paraphrase these "
        "instructions themselves. There is always SOMETHING to react to below -- even a single prior line "
        "is enough; never reply that you can't produce one.\n"
        "Example, for an unrelated hypothetical conversation about a house move -- copy the TAG, not the "
        f"words: <narration>{_NARRATION_EXAMPLE_TAG}</narration>"
    )
    # Bug fix (2026-09-11), per explicit instruction: reverted to "SMALL"
    # (the openai/gpt5-nano bypass above is no longer needed -- see this
    # function's own docstring).
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt, "model": "SMALL"}
    if session:
        body["session"] = session
    try:
        data = await _post_json(body)
    except Exception as exc:
        log_event("plugin:voice", "generate_progress_comment_request_failed", error=str(exc))
        return None
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        log_event("plugin:voice", "generate_progress_comment_bad_response", status=data.get(".status"), reason=data.get(".reason"))
        return None
    text = _extract_tagged_text(data["result"], "narration")
    if not text:
        log_event("plugin:voice", "generate_progress_comment_unextractable", raw=data["result"][:300])
        return None
    if _looks_like_narration_garbage(text, language):
        log_event("plugin:voice", "generate_progress_comment_rejected_garbage", text=text[:300])
        return None
    if _is_echo_of_dialogue(text, recent_dialogue):
        log_event("plugin:voice", "generate_progress_comment_rejected_echo", text=text[:300])
        return None
    if _is_echo_of_prompt_example(text):
        log_event("plugin:voice", "generate_progress_comment_rejected_example_echo", text=text[:300])
        return None
    # Per explicit instruction (2026-09-13): force the final text through
    # Camerlengo's own dedicated ai:translate command, targeting `language`
    # -- the authoritative, separately/continuously detected value (see
    # chat_session.py's current_language_name), not something this creative
    # generation call was ever asked to correctly infer on its own anymore
    # (see the prompt's own comment above). Unconditional, not "only if it
    # looks wrong" -- confirmed live this small model doesn't reliably self-
    # report a language mismatch, so a forced pass is the only guarantee.
    # Falls back to the untranslated text on any failure -- a narration
    # comment in the wrong language is still better than none at all.
    translated = await translate_text(text, language, session=session)
    final_text = translated or text
    if translated is None:
        log_event("plugin:voice", "generate_progress_comment_translate_failed_using_original", text=text[:300])
    log_event("plugin:voice", "generate_progress_comment_ok", dialogue_chars=len(recent_dialogue), text=final_text)
    return final_text


async def _synthesize_speech_locally(text: str, voice: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                res = await client.post(local_tts_url(), json={"text": text, "voice": voice})
                break
            except httpx.TransportError as exc:
                last_err = exc
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise last_err  # type: ignore[misc]
        if res.status_code >= 400:
            raise VoiceApiError(f"local TTS server returned {res.status_code}: {res.text[:300]}")
        return base64.b64encode(res.content).decode("ascii")


async def _synthesize_speech_via_camerlengo(text: str, voice: str, session: str | None = None) -> str:
    body: dict[str, Any] = {"command": "ai:tts", "key": CAROLINE_SW_KEY, "text": text, "voice": voice}
    if session:
        body["session"] = session
    data = await _post_json(body)
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        if data.get(".errcode") == "402":
            raise VoiceApiError("insufficient_balance")
        raise VoiceApiError(str(data.get(".reason") or "TTS failed"))
    return data["result"]  # base64 MP3


async def synthesize_speech(text: str, voice: str = "Nova", session: str | None = None) -> str:
    """Prefers the local edge-tts server over Camerlengo's ai:tts -- purely
    a latency optimization (no Camerlengo round trip), tried FIRST
    regardless of session, falling back to Camerlengo whenever the local
    attempt fails for any reason (never a hard new dependency)."""
    language = await detect_language(text)
    edge_voice = _edge_tts_voice_for(voice, language)
    try:
        audio = await _synthesize_speech_locally(text, edge_voice)
        log_event("plugin:voice", "tts_local_ok", edge_voice=edge_voice, text_len=len(text))
        return audio
    except Exception as exc:
        log_event("plugin:voice", "tts_local_failed_falling_back", edge_voice=edge_voice, error=str(exc))
        return await _synthesize_speech_via_camerlengo(text, voice, session)


# Skip the LLM cleanup call entirely when the text plainly has nothing for
# it to fix -- no markdown/HTML syntax, no digits.
_NEEDS_TTS_CLEANUP = re.compile(r"[*_`#|~]|<[a-z][^>]*>|\d", re.IGNORECASE)


async def clean_text_for_speech(text: str, session: str | None = None) -> str:
    """Strips markdown/HTML formatting and rewrites digits/numerals into
    their spoken, correctly-inflected form before text reaches TTS/Visual
    Mode."""
    if not _NEEDS_TTS_CLEANUP.search(text):
        return text
    prompt = (
        "Rewrite the following text so it is ready to be read aloud by a text-to-speech engine. "
        "Perform exactly these transformations, nothing else:\n"
        "- Strip ALL markdown formatting (**bold**, *italic*, `code`, # headers, - / * bullet "
        "lists, [links](url), | tables |, --- rules, > quotes) down to plain spoken prose -- "
        "keep the words, drop the syntax.\n"
        "- Strip ALL HTML tags the same way -- keep the text content, drop the markup.\n"
        "- Rewrite every digit/numeral (dates, quantities, times, ordinals, etc.) as the words "
        "a native speaker would actually SAY aloud, correctly inflected/declined for its exact "
        "grammatical role in the sentence (case, number, gender, as the language requires) -- "
        'not just the bare nominative/cardinal form. Example in Russian: "5 книг" -> "пяти книг", '
        '"с 5 книгами" -> "с пятью книгами", "2024 год" -> "две тысячи двадцать четвёртый год".\n'
        "- Keep the exact same language and meaning. Do not translate, summarize, add, or remove "
        "any information. Do not add commentary, explanations, or quotation marks around the result.\n"
        "- Return ONLY the rewritten text, nothing else.\n\n"
        f"Text:\n{text}"
    )
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt}
    if session:
        body["session"] = session
    try:
        data = await _post_json(body)
    except Exception:
        return text
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        return text
    return data["result"]


async def describe_image_cheap(base64_png: str) -> dict[str, Any]:
    """Cheap image understanding (Camerlengo's ai:describeImage, a small
    model) as a deliberately CHEAPER alternative to putting raw image
    bytes into Claude's own context."""
    data = await _post_json({"command": "ai:describeImage", "key": CAROLINE_SW_KEY, "content": base64_png, "type": "base64"})
    if data.get(".status") != "ok" or not isinstance(data.get("result"), dict):
        raise VoiceApiError(str(data.get(".reason") or "describeImage failed"))
    r = data["result"]
    return {"description": str(r.get("description") or ""), "objects": r.get("objects"), "palette": r.get("palette")}
