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


# Bug fix (2026-09-25), confirmed live: a bare-name reply is only a prompt
# ASK, not a guarantee -- caught two real corruptions this let through
# unfiltered: one tab's persisted language was the model's whole chain of
# reasoning ("To determine the language, let's analyze the given user
# message: \"...\"\n\nThe language is English"), and a SEPARATE tab flipped
# to a flatly wrong "English" with no way to tell whether that was a genuine
# (if mistaken) one-word verdict or the same kind of leak by coincidence.
# Both values get fed straight into live prompts as {language} (see
# chat_session.py's STARTUP_GREETING_NUDGE_TEMPLATE/translate_text), so
# saving anything but a short bare name poisons real conversation turns, not
# just this cache. Per explicit instruction (2026-09-25): the prompt above now
# demands exactly ONE capitalized English word ("Russian", "English",
# "Spanish", ...), matching the same single-token discipline detect_language()
# above already gets for free from Camerlengo's own ai:detectLanguage (a
# dedicated, narrowly-scoped command) -- ai:resolve is the general-purpose one
# and has no such built-in contract, so it has to be enforced here instead.
# A prompt instruction is still only an ASK, not a guarantee -- reject
# anything that isn't literally that one word instead of trusting compliance.
_LANGUAGE_NAME_WORD = re.compile(r"^[A-Z][A-Za-z'-]{1,29}$")


def _looks_like_a_language_name(name: str) -> bool:
    return bool(_LANGUAGE_NAME_WORD.match(name))


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
        "Below are the user's most recent messages (ignore any assistant/system text mixed in -- focus only on "
        "the user's own words). These messages can be a mix of languages, including short fragments or stray "
        "boilerplate in a different language than the user actually speaks -- determine which language is "
        "PREDOMINANTLY used, i.e. which language makes up the MAJORITY of this text overall, not just whichever "
        "language the last fragment happens to be in. Your ENTIRE reply must be a SINGLE WORD: that predominant "
        "language's own English name, capitalized, e.g. \"Russian\", \"English\", \"Spanish\", \"German\", "
        "\"French\", \"Ukrainian\" -- nothing before or after it: no punctuation, no quotes, no sentence, no "
        "explanation, no reasoning. Not a full name like \"Brazilian Portuguese\" -- the single base word "
        '("Portuguese") is enough.\n\n'
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
    if not name:
        return None
    if not _looks_like_a_language_name(name):
        log_event("plugin:voice", "resolve_user_language_implausible_result", raw_len=len(name), raw_preview=name[:120])
        return None
    return name


def _gender_agreement_clause(gender: str | None) -> str:
    """Same grammatical-gender-agreement rule persona.py's own system-prompt
    block states for the main session (persona_system_prompt_append) --
    duplicated here (not imported/shared) because this module's callers are
    a completely separate, small-model side channel (narration drafting,
    forced translation) that never sees that system prompt at all. Without
    this, a translated/drafted first-person line has no idea which
    grammatical gender to use and defaults inconsistently -- confirmed live
    (2026-09-26): the narrator kept coming out masculine in Russian despite
    Caroline's persona being female. Returns "" (no clause at all) when
    gender is unknown, rather than guessing."""
    if not gender:
        return ""
    gender_lower = gender.lower()
    if "female" in gender_lower:
        examples = '"поняла" not "понял", "сказала" not "сказал", "сделала" not "сделал"'
    elif "male" in gender_lower:
        examples = '"понял" not "поняла", "сказал" not "сказала", "сделал" not "сделала"'
    else:
        return ""
    return (
        f" The speaker is {gender} -- in every language where verbs/adjectives inflect for the speaker's "
        f"grammatical gender (e.g. Russian past tense), use that gender's forms consistently, e.g. {examples} "
        "-- never mix or default to the other gender."
    )


async def translate_text(text: str, language: str, session: str | None = None, timeout: float = 30.0, gender: str | None = None) -> str | None:
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
    text, never block on this.

    Widened (2026-09-15), per explicit instruction: no longer narration-
    only. Confirmed live -- the real Claude model itself (not narration)
    can drop a terse English status line into an otherwise-Russian
    conversation (mid-tool-call-chain text like "Now executing deletes and
    marks in batches."), and language_hint_instruction's system-prompt
    nudge is advisory, not a hard guarantee. chat_session.py now runs
    EVERY real visible assistant text block through this too, not just
    generate_progress_comment's filler. Two consequences of that widening,
    both handled below: real replies can be far longer than a narration
    aside, so the garbage filter's length cap is now sized off the INPUT
    text's own length rather than the fixed narration-sized constant; and
    a real reply can contain code/paths/technical content that must not be
    translated along with the prose, so the prompt now says so explicitly.

    `timeout` (2026-09-22): forwarded to _post_json's own per-attempt
    budget, default unchanged (30s) for the two real-visible-reply call
    sites (chat_session.py) -- narration passes a much shorter value, see
    generate_progress_comment's own doc comment for why."""
    prompt = (
        f"Translate the following text into {language}." + _gender_agreement_clause(gender) + " If the text is already in that language, respond with "
        "it unchanged (or only lightly cleaned up) -- do not refuse or explain, translation into the SAME "
        "language it's already in is a normal, valid case, not an error. Leave code blocks/inline code, file "
        "paths, URLs, and other literal technical identifiers exactly as they are -- translate only the "
        "surrounding natural-language prose, and preserve the original formatting/markdown structure.\n\n"
        f"Text to translate:\n---\n{text}\n---\n\n"
        "Output format, follow exactly -- a program parses this, not a person: write ONLY the translated text "
        "inside a <translation> tag, nothing else anywhere in your reply -- no JSON, no markdown wrapper, no "
        "code fences around the WHOLE answer, no quotes around it, no explanation.\n"
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
        data = await _post_json(body, timeout=timeout)
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
    # Bug fix (2026-09-15): _NARRATION_MAX_CHARS (400) is sized for
    # narration's own short asides -- a real reply being translated can
    # legitimately be much longer, so cap this at a generous multiple of
    # the ORIGINAL text's own length instead (Russian in particular tends
    # to run noticeably longer than English for the same content) rather
    # than the fixed narration-sized constant, which would reject every
    # long-but-correct translation as "garbage" by length alone.
    max_chars = max(_NARRATION_MAX_CHARS, len(text) * 3)
    if _looks_like_narration_garbage(translated, language, max_chars=max_chars):
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
    # Bug fix (2026-09-16), confirmed live ("что это за херня насыпалась?"):
    # the original version of this pattern required "no (specific )remark/
    # response/..." with NOTHING else in between -- real observed text had
    # an extra adjective in the way ("no specific SHORT remark", "no VALID
    # response") that broke the match, and a wider set of trailing verbs
    # than the original list covered ("allowed", "possible", "present").
    # Loosened both sides rather than chasing each new exact phrasing.
    re.compile(r"\bno\b.{0,25}\b(?:remark|response|sentence|instructions?)\b.{0,40}\b(?:found|derived|generated|drafted|identified|could be|allowed|possible|present)\b", re.IGNORECASE),
    re.compile(r"\bcould not be (?:drafted|generated|derived|produced)\b", re.IGNORECASE),
    # Bug fix (2026-09-16), confirmed live: the model treated its OWN
    # prompt as content to describe/summarize instead of an instruction to
    # follow ("The page provides extensive instructions and a sample
    # conversation, but no specific short remark is present...") -- a
    # wholly new failure shape, well-formed prose, no refusal wording, that
    # slipped past every pattern above undetected (generate_progress_
    # comment_ok). Not a refusal, not a recap of the real conversation --
    # a meta-description of the PROMPT itself, which a genuine remark can
    # never be.
    re.compile(r"\bthe (?:page|prompt|instructions?) (?:provides?|contains?|includes?)\b", re.IGNORECASE),
    re.compile(r"\bthe assistant will (?:then )?draft\b", re.IGNORECASE),
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
    # Bug fix (2026-09-16), confirmed live ("критический дефект" -- третье
    # лицо): the patterns above only catch "the user asks..."/"the
    # Caroline response..." shapes -- two genuine live examples slipped
    # past both: "Caroline wants to know the exact date and time of the
    # concert..." and "...Caroline is handling the escalation" -- her own
    # NAME used as a plain third-person subject with all kinds of verb
    # forms (wants/is handling/raising/...), no "the ... response" wrapper
    # to match against. Trying to enumerate every verb form is the same
    # losing game flagged elsewhere in this file -- simpler and more
    # robust: she has NO legitimate reason to write her own name at all in
    # a genuine first-person remark (that's what "I" is for), so any
    # occurrence of the literal word is itself the tell, regardless of
    # what comes after it.
    re.compile(r"\bCaroline\b", re.IGNORECASE),
    # Bug fix (2026-09-16), confirmed live AGAIN, same day as the "the page
    # provides extensive instructions" incident fixed above, with entirely
    # different wording this time ("Write a short remark (max two words)
    # reacting to the given conversation, using the <narration> tag,
    # without any JSON, markdown, or preamble.") -- the model isn't quoting
    # its instructions verbatim (which _is_echo_of_prompt_example could
    # catch), it's PARAPHRASING them in its own words. Chasing each new
    # exact phrasing one at a time is a losing game (already flagged as
    # such elsewhere in this file) -- this instead matches on the
    # characteristic META-VOCABULARY of the prompt's own instructions
    # (words like "narration tag", "markdown", "preamble" have no reason to
    # ever appear in a genuine first-person remark about the user's actual
    # conversation, whatever topic that is) rather than any exact sentence
    # shape. Broader and more durable than another one-off pattern.
    re.compile(r"<narration>", re.IGNORECASE),
    re.compile(r"\bnarration tag\b", re.IGNORECASE),
    re.compile(r"\b(?:no|without any) (?:json|markdown|preamble)\b", re.IGNORECASE),
    re.compile(r"\bshort remark\b", re.IGNORECASE),
    re.compile(r"\breacting to (?:the|this|a) (?:given |real )?conversation\b", re.IGNORECASE),
    re.compile(r"\b(?:max|maximum) (?:one|two|three|1|2|3) (?:words?|sentences?)\b", re.IGNORECASE),
]
_NARRATION_MAX_CHARS = 400
# Han / Hiragana / Katakana / Hangul. Progress narration for this product
# is only ever asked for in a European language; a CJK reply is the SMALL
# model flailing, never legitimate here.
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def _looks_like_narration_garbage(text: str, language: str = "", max_chars: int = _NARRATION_MAX_CHARS) -> bool:
    if len(text) > max_chars:
        return True
    if _CJK_RE.search(text) and not re.search(r"chin|japan|korea|mandarin|中文", language, re.IGNORECASE):
        return True
    return any(p.search(text) for p in _NARRATION_GARBAGE_PATTERNS)


# Bug fix (2026-09-16), confirmed live: an echo of an ENGLISH prompt
# example slipped past this exact check because a couple of its Latin
# letters came back as visually-identical CYRILLIC homoglyphs instead
# ("Мovers..." -- Cyrillic М, U+041C -- not Latin M, U+004D) -- a plain
# case-folded string comparison sees those as different characters and the
# containment check silently fails, even though a person reading it can't
# tell the difference at all. Translates the handful of Cyrillic letters
# that are visually indistinguishable from a Latin one to their Latin
# equivalent before comparing -- one-directional (Cyrillic->Latin) is
# enough since the echoed example text is always the English original.
_CYRILLIC_LATIN_CONFUSABLES = str.maketrans(
    # lowercase only -- .lower() below always runs first, so an uppercase
    # Cyrillic confusable is already folded to its lowercase Cyrillic form
    # by the time this table is applied. н -> h (not n) -- that's the
    # actual visual lookalike (Cyrillic н, not Cyrillic п).
    "аекмнорстух", "aekmhopctyx",
)


def _normalized_overlap(text: str, other: str) -> bool:
    """Whitespace-collapsed, case-folded containment check either
    direction -- a near-verbatim echo, not requiring an exact match
    (paraphrases that drop/add a clause on either side still count)."""
    norm_text = re.sub(r"\s+", " ", text).strip().lower().translate(_CYRILLIC_LATIN_CONFUSABLES)
    norm_other = re.sub(r"\s+", " ", other).strip().lower().translate(_CYRILLIC_LATIN_CONFUSABLES)
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

# Bug fix (2026-09-16), per explicit instruction ("что это за херня
# насыпалась?" -- a real live incident): the 2026-09-15 restructuring above
# fixed the ORIGINAL "lost in the middle" failure (tag contract buried at
# the end) by moving the contract first, but made the overall prompt
# LONGER and structurally more complex (contract + persona paragraph + TWO
# separate bad/good example pairs + dialogue + reminder + a third,
# unrelated example scenario for the tag format) -- confirmed live the
# cheap SMALL model this runs against can't reliably track that much
# structure: given a completely ordinary, unpoisoned dialogue, it replied
# "The page provides extensive instructions and a sample conversation, but
# no specific short remark is present" -- i.e. it started treating the
# WHOLE PROMPT as content to summarize instead of an instruction to follow.
# _is_echo_of_prompt_example's own comment already diagnosed the right
# fix for this general class of problem before ("give a small model less
# to track, don't just reorder it correctly") -- applied properly this
# time: ONE combined example pair (covering both third-person-recap AND
# new-promise in the same bad line) replaces the two separate pairs above,
# and the same GOOD line doubles as the tag-format example at the end
# instead of introducing a THIRD, unrelated scenario. The two pairs above
# are kept defined (not deleted) only because _is_echo_of_prompt_example
# still checks the model's reply against them defensively -- they're no
# longer in the prompt text itself.
_NARRATION_COMBINED_BAD = "The user asks Caroline to check the invoice, and Caroline says she'll email accounting once she confirms the numbers."
_NARRATION_COMBINED_GOOD = "Invoice math like this always takes longer than it looks like it should."


def _is_echo_of_prompt_example(text: str) -> bool:
    return any(
        _normalized_overlap(text, example)
        for example in (
            _NARRATION_EXAMPLE_BAD, _NARRATION_EXAMPLE_GOOD, _NARRATION_EXAMPLE_TAG,
            _NARRATION_EXAMPLE_THIRDPERSON_BAD, _NARRATION_EXAMPLE_THIRDPERSON_GOOD,
            _NARRATION_COMBINED_BAD, _NARRATION_COMBINED_GOOD,
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
_JSON_TEXT_KEYS = ("narration", "translation", "translated_text", "result", "text", "message", "assistant", "response", "comment", "answer", "reply")


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
        # Bug fix (2026-09-16), confirmed live: the model can reasonably
        # pick almost any key name for its one JSON field ("reply",
        # "example", ... -- "reply" itself only surfaced during THIS
        # incident's own testing, was never on the allowlist before), and
        # can nest it arbitrarily (confirmed live the same day:
        # `{"responses": [{"tag": "narration", "text": "..."}]}` -- a list
        # of dicts, not a flat object). Chasing every possible shape one at
        # a time is a losing battle -- recursively collect every
        # sentence-length string anywhere in the parsed structure
        # (excluding short label-like strings such as a bare "narration"
        # tag name) and use it if there's EXACTLY one candidate. More than
        # one is genuinely ambiguous -- stay conservative and give up
        # rather than guess wrong.
        candidates: list[str] = []

        def _collect(node: Any) -> None:
            if isinstance(node, str):
                if len(node.strip()) >= 15:
                    candidates.append(node.strip())
            elif isinstance(node, dict):
                for v in node.values():
                    _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        _collect(parsed)
        if len(candidates) == 1:
            return candidates[0]
        return ""
    return stripped


async def generate_progress_comment(
    recent_dialogue: str, language: str, session: str | None = None, timeout: float = 30.0, gender: str | None = None,
) -> str | None:
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

    `timeout` (2026-09-22), per explicit instruction ("Нарратор должен
    срабатывать КАЖДУЮ МИНУТУ"): forwarded to both the draft _post_json
    call below AND the translate_text() pass -- confirmed live this whole
    generate+translate chain took 71s end to end once, on top of
    chat_session.py's own PROGRESS_NARRATION_INTERVAL_MS=60s wait, because
    both legs used the default 30s-per-attempt/3-attempt budget every
    OTHER (real, user-facing) SW API call gets. Narration is cosmetic
    filler under a 60s promise, not worth that patience -- callers doing
    the actual 60s-cadence narration loop should pass a much shorter
    value (chat_session.py's own NARRATION_NETWORK_TIMEOUT_S) so a
    slow/hung attempt fails fast and the OUTER retry loop
    (NARRATION_GENERATION_RETRY_ATTEMPTS) gets a real chance to try again
    within the same minute, instead of one slow attempt eating most of
    it. Default (30.0) keeps every other caller's behavior unchanged.

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
        # Bug fix (2026-09-16), per explicit instruction ("что это за херня
        # насыпалась?" -- see _NARRATION_COMBINED_BAD/GOOD's own comment for
        # the full incident): the 2026-09-15 version below (put the format
        # contract first, "sandwich" it with a reminder at the end) was the
        # right general idea but too much prompt overall for the cheap
        # SMALL model this runs against -- confirmed live it started
        # treating the WHOLE PROMPT as a "page" to summarize instead of an
        # instruction to follow, given a completely ordinary, unpoisoned
        # dialogue. Cut to ONE combined example (covering both failure
        # shapes -- third-person recap AND a new promise -- in one bad
        # line) instead of two separate example pairs, and the closing
        # reminder reuses that SAME good line as the tag-format example
        # instead of introducing a third, unrelated scenario. Shorter
        # prompt, same coverage.
        "CRITICAL: a program parses your reply, not a person. Reply with ONLY a <narration> tag around ONE "
        "short sentence (two at most) -- no JSON, no markdown, no preamble, nothing else, no matter how long "
        "or repetitive anything below looks.\n\n"
        "Caroline has been quietly working on the user's last message for over a minute with nothing said "
        "back yet. Write ONE short remark AS her, first person (\"I\", never \"the user\"/\"Caroline\" as a "
        "third party), reacting to something real already in the conversation below -- a detail, an "
        "observation, a follow-up thought. Never: a status update (\"I'm checking/looking into X\"), a "
        "recap/summary of the conversation, a new promise or plan (\"I'll do X\") -- you don't know what "
        "she's actually doing right now. Never expose internal machinery either (restarts, tools, files, "
        "backups, session mechanics) even if the conversation below mentions it or repeats the same line "
        "several times -- skip past that, react to the real substance instead.\n"
        f"  Bad (third person AND a new promise): \"{_NARRATION_COMBINED_BAD}\"\n"
        f"  Good (first person, a real reaction, no promise): \"{_NARRATION_COMBINED_GOOD}\"\n\n"
        # Bug fix (2026-09-16), confirmed live via real logs (two separate
        # incidents on the same day, RUSSIAN-language drafts both times):
        # "write in whichever language feels natural" let the model draft
        # directly in the conversation's own language -- and it kept
        # producing exactly the third-person-recap failure this prompt's
        # own rules explicitly forbid ("ÐšÐ°Ñ€Ð¾Ð»Ð°Ð¹Ð½ Ñ…Ð¾Ñ‡ÐµÑ‚ ÑƒÐ·Ð½Ð°Ñ‚ÑŒ..." --
        # "Caroline wants to know...", "ÐŸÐ¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÑŒ Ð¿Ñ€Ð¾ÑÐ¸Ñ‚ ÐšÐ°Ñ€Ð¾Ð»Ð¸Ð½Ñƒ..." --
        # "The user asks Caroline...") -- invisible to _NARRATION_GARBAGE_
        # PATTERNS below, which are English-only. Forcing English drafting
        # unconditionally closes that whole language gap at once instead of
        # trying to translate every pattern into every language a
        # conversation might be in -- the translation step already exists
        # and runs regardless of what language is drafted here.
        "Write your remark in English, always, no matter what language the conversation below is in -- a "
        "separate step translates it into the right language afterward. Do not attempt to write in any other "
        "language yourself.\n\n"
        f"Conversation (oldest first):\n---\n{recent_dialogue}\n---\n\n"
        "Reply now with ONLY your own real remark about the conversation above, wrapped in the tag -- same "
        "shape as this unrelated example, copy the TAG not the words: "
        f"<narration>{_NARRATION_EXAMPLE_TAG}</narration>"
    )
    # Bug fix (2026-09-11), per explicit instruction: reverted to "SMALL"
    # (the openai/gpt5-nano bypass above is no longer needed -- see this
    # function's own docstring).
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt, "model": "SMALL"}
    if session:
        body["session"] = session
    try:
        data = await _post_json(body, timeout=timeout)
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
    translated = await translate_text(text, language, session=session, timeout=timeout, gender=gender)
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
