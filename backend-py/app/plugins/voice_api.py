"""Ports backend/src/voice.ts -- TTS/STT/language-detection/cheap-image-
description via Camerlengo, plus the local (in-process) edge-tts path for
TTS -- see app.local_edge_tts. Used by app/main.py's tts/stt control_request
ops and by app_browser_plugin.py's app_browser_describe.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any, Awaitable, Callable

from app.logging_setup import log_event
from app.login_api import get_cached_v2_session
from app.plugins.sw_api import CAROLINE_SW_KEY, _post_json


class VoiceApiError(Exception):
    pass


# check_background_relay_worth's own retry budget (2026-10-19) -- see that
# function's own docstring for why this gate specifically gets several flat
# (never exponential) attempts instead of the usual single try.
_RELAY_WORTH_ATTEMPTS = 4
_RELAY_WORTH_RETRY_DELAY_S = 1.0


async def transcribe_audio(audio_base64: str, fmt: str, session: str | None = None) -> str:
    body: dict[str, Any] = {"command": "ai:stt", "key": CAROLINE_SW_KEY, "audio": audio_base64, "format": fmt}
    if not session:
        session = await get_cached_v2_session()
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
        body: dict[str, Any] = {"command": "ai:detectLanguage", "key": CAROLINE_SW_KEY, "text": text}
        session = await get_cached_v2_session()
        if session:
            body["session"] = session
        data = await _post_json(body)
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
    if not session:
        session = await get_cached_v2_session()
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


def _first_person_examples(gender_lower: str) -> str | None:
    if "female" in gender_lower:
        return '"поняла" not "понял", "сказала" not "сказал", "сделала" not "сделал"'
    if "male" in gender_lower:
        return '"понял" not "поняла", "сказал" not "сказала", "сделал" not "сделала"'
    return None


def _second_person_examples(gender_lower: str) -> str | None:
    if "female" in gender_lower:
        return '"ты сделала" not "ты сделал", "ты сказала" not "ты сказал"'
    if "male" in gender_lower:
        return '"ты сделал" not "ты сделала", "ты сказал" not "ты сказала"'
    return None


def _gender_agreement_clause(speaker_gender: str | None, addressee_gender: str | None = None) -> str:
    """Same grammatical-gender-agreement rule persona.py's own system-prompt
    block states for the main session (persona_system_prompt_append) and
    owner_profile.py's own clause states for addressing the owner --
    duplicated here (not imported/shared) because this module's callers are
    a completely separate, small-model side channel (narration drafting,
    forced translation) that never sees either system prompt at all.
    Without this, a translated/drafted line has no idea which grammatical
    gender to use for the SPEAKER (Caroline, first person: "я сделала") or
    for who she's ADDRESSING/describing (the owner, second/third person:
    "ты сделал") and defaults inconsistently -- confirmed live (2026-09-26)
    for the speaker side, and again (2026-10-05, explicit report: "все
    время ко мне в женском роде обращается") for the addressee side --
    translating from English (no grammatical gender marking on "you") left
    the owner's own gender completely unknown to the translator, so it
    guessed. Each half is independent and optional: pass whichever genders
    are actually known; an unknown one is simply omitted, never guessed."""
    parts = []
    speaker_examples = _first_person_examples(speaker_gender.lower()) if speaker_gender else None
    if speaker_examples:
        parts.append(
            f"The speaker (Caroline, first person) is {speaker_gender} -- in every language where verbs/"
            f"adjectives inflect for the speaker's grammatical gender (e.g. Russian past tense), use that "
            f"gender's forms consistently, e.g. {speaker_examples} -- never mix or default to the other gender."
        )
    addressee_examples = _second_person_examples(addressee_gender.lower()) if addressee_gender else None
    if addressee_examples:
        parts.append(
            f"The person being addressed or referred to (second/third person -- \"ты\"/\"он\"/\"она\", or a past-"
            f"tense verb about something THEY did) is {addressee_gender} -- a fact about THEM, separate from "
            f"the speaker's own gender (if stated elsewhere in this instruction); use grammatical forms "
            f"matching THEIR gender, e.g. {addressee_examples} -- never bleed the speaker's own gender into "
            "how they're addressed."
        )
    # Bug fix (2026-10-15), confirmed live, repeatedly, same real incident:
    # Caroline drafted a letter for the owner (male) to send to Oksana in
    # HIS OWN first-person voice ("28 и 29 октября я заберу его сам") --
    # the speaker-gender rule above has no way to tell that this quoted
    # draft's "I" is the addressee speaking, not Caroline, and kept forcing
    # it to her own (female) gender ("я заберу его сама"), surviving three
    # direct corrections in the same conversation because every retranslate
    # pass re-applied the same blanket rule. Only fires when BOTH genders
    # are known -- needs the addressee's own first-person forms, not just
    # their second-person ones above.
    quoted_speaker_examples = _first_person_examples(addressee_gender.lower()) if addressee_gender else None
    if speaker_examples and quoted_speaker_examples:
        parts.append(
            "If part of the text is a DRAFTED OR QUOTED message meant to be sent in someone else's own voice "
            "(e.g. a letter or reply Caroline composed for the user to send as themselves -- often introduced "
            "by something like \"Here's the text for...\" or set off with a quote/dash/colon), the first-person "
            f"forms INSIDE that quoted message belong to THAT person ({addressee_gender}), not to Caroline -- "
            f"use their gender there instead, e.g. {quoted_speaker_examples}. Only Caroline's own surrounding "
            "commentary, outside the quoted message, uses her own gender from the rule above."
        )
    if parts:
        # Bug fix (2026-10-05), confirmed live: this instruction, meant to fix
        # ONLY grammatical gender, once caused ai:translate to also flip an
        # unrelated verb's PERSON and MOOD in the same sentence -- Caroline's
        # own "Отправила..." (I sent it, first person, reporting a thing she'd
        # already done herself) came back as "Отправь это..." (an imperative
        # telling the user to send it), while the actual gender fix elsewhere
        # in that same sentence (ты писала -> ты писал) was applied correctly.
        # Scoped explicitly below so a gender-agreement instruction can never
        # again be read as license to restructure who did what.
        parts.append(
            "This is a narrow grammatical-gender instruction, not license to rewrite anything else: change "
            "ONLY the gender of inflected forms per the rules above. Never change grammatical person (first/"
            "second/third) or mood on any verb, and never change who is reported to have done something -- a "
            "declarative statement about an action the speaker already completed (e.g. \"I sent it\") must "
            "never become an imperative telling someone else to do it (e.g. \"send it\"), or vice versa. If "
            "a verb's gender is already correct, or doesn't need fixing, leave that verb completely untouched."
        )
    return (" " + " ".join(parts)) if parts else ""


# Bug fix (2026-09-26), per explicit instruction ("3 с разными промптами"): three
# genuinely different phrasings, not the same wording repeated -- a retry that resends
# the identical prompt a refusal or garbage-filter already rejected once is likely to
# fail the same way again for the same reason. Each still ends in the same <translation>
# tag contract so translate_text's own extraction/garbage-filter logic doesn't need to
# know which variant answered.
async def translate_text(
    text: str, language: str, session: str | None = None, timeout: float = 30.0,
    gender: str | None = None, addressee_gender: str | None = None, context: str | None = None,
    retries: int = 3,
) -> str | None:
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

    Widened (2026-09-15), per explicit instruction: no longer narration-
    only. Confirmed live -- the real Claude model itself (not narration)
    can drop a terse English status line into an otherwise-Russian
    conversation (mid-tool-call-chain text like "Now executing deletes and
    marks in batches."), and language_hint_instruction's system-prompt
    nudge is advisory, not a hard guarantee. chat_session.py now runs
    EVERY real visible assistant text block through this too, not just
    generate_progress_comment's filler.

    `timeout` (2026-09-22): forwarded to _post_json's own per-attempt
    budget, default unchanged (30s) for the two real-visible-reply call
    sites (chat_session.py) -- narration passes a much shorter value, see
    generate_progress_comment's own doc comment for why.

    `retries` (2026-10-15): forwarded to _post_json's own `retries` --
    narration passes 1 (no internal multiplication of its already-short
    timeout); see _post_json's own doc comment for the real incident this
    fixes.

    Rewritten (2026-09-28), per explicit instruction ("Используй ai:translate
    вместо ai:resolve"), after a real incident traced to its actual root
    cause: this used to route through the generic ai:resolve command with a
    hand-written <translation>-tag prompt, retried across three differently
    worded prompt variants (2026-09-26 fix) because that generic path kept
    producing well-formed, non-refusal, correctly-tagged replies that were
    simply still in the SOURCE language -- a failure the tag/refusal/length
    checks below have no way to catch by construction, confirmed live (a
    297-char reply that changed nothing about the actual language). The
    ORIGINAL reason ai:resolve was used at all -- "CAROLINE_SW_KEY's ACL
    rejects the dedicated ai:translate command outright" -- turned out to
    be a real but fixable server-side restriction, not a permanent one:
    the key's scope now explicitly grants ai:translate (see reforce's
    key_scopes table, key id 17), confirmed live with a real call. ai:translate
    is a purpose-built server-side translation function (AI.translateText),
    not a generic instruction-following completion, so it needs no <tag>
    contract, no extraction, and no multi-prompt retry -- one call, plain
    text/language params. The garbage filter stays as defense-in-depth
    (refusal/JSON-leak/wrong-script safety net), not the primary mechanism,
    same philosophy as everywhere else it's used.

    `context` (2026-10-15), per explicit instruction ("ввести контекст в
    перевод" -- not another hand-written special-case rule): AI.translateText
    already accepts preceeding_context/following_context (used for exactly
    this -- see its own docstring, "provided as a reference to improve
    translation accuracy"), but this wrapper never passed anything through
    them. Without real context, a general instruction like "a quoted/drafted
    message belongs in someone else's voice" has nothing to ground itself
    in beyond pattern-matching the bare text, which is exactly how a letter
    Caroline drafted for the owner to send to his ex-wife came back with
    the quoted half switched to English mid-translation (confirmed live,
    2026-10-15) -- the translator had no idea who anyone in that text
    actually was. Passed as preceeding_context: the real task this reply is
    answering (the same string review_reply already receives as `task` at
    every call site that has one), so the model translating has the same
    grounding the review step already does, not a blind substring.

    Rewritten (2026-10-15), per explicit instruction, after a real incident:
    ai:translate (AI.translateText, reforce's shared general-purpose
    translation command) forces the WHOLE text into `language`
    unconditionally -- confirmed live, asked to translate "Это кот" into
    English, the reply "Вот перевод на английский: This is a cat" came back
    with "This is a cat" retranslated back into Russian, destroying the
    actual deliverable. Caroline's own server-side agent, reply.translate
    (apps/microagents/ReplyTranslate.py in reforce-microagents, "reply:
    translate" here), replaces ai:translate for this wrapper only -- every
    other ai:translate caller in reforce is untouched. It takes the SAME
    `context` as `task` (what this exception needs to judge against) instead
    of preceeding_context/following_context, and -- like every other reply:
    command -- answers {"status", "output": {"text"}}, not ai:translate's
    bare {"result"}."""
    from app.evolution_client import run_reply_agent
    from app.microagents.reply_translate import TRANSLATE

    agent_input: dict[str, Any] = {
        "text": text, "language": language,
        "instructions": _gender_agreement_clause(gender, addressee_gender) or None,
        "task": context or None,
    }
    try:
        result = await run_reply_agent(TRANSLATE, agent_input)
    except Exception as exc:
        log_event("plugin:voice", "translate_text_failed", reason="request_failed", error=str(exc))
        return None
    output = result.get("output") if result.get("status") == "ok" else None
    if not isinstance(output, dict) or not isinstance(output.get("text"), str):
        log_event("plugin:voice", "translate_text_failed", reason="bad_response", error=result.get("error"))
        return None
    translated = output["text"]
    if not translated.strip():
        log_event("plugin:voice", "translate_text_failed", reason="empty")
        return None
    # See _NARRATION_MAX_CHARS/_looks_like_narration_garbage's own comments --
    # sized off the ORIGINAL text's own length, not the narration-sized
    # constant, since a real reply being translated can legitimately be
    # much longer than a narration aside.
    max_chars = max(_NARRATION_MAX_CHARS, len(text) * 3)
    if _looks_like_narration_garbage(translated, language, max_chars=max_chars):
        log_event("plugin:voice", "translate_text_failed", reason="rejected_garbage", text=translated[:300])
        return None
    return translated


async def review_reply(text: str, task: str, session: str | None = None, timeout: float = 15.0) -> str | None:
    """Per explicit instruction (2026-10-11): every piece of outbound text
    (a progress-narration remark or a real final reply) must be checked for
    task relevance, absence of internal-mechanics leaks, and sounding like a
    person rather than an AI -- backed by a real microagent (server-side
    reply.review, same escalation ladder as FactMemory/AddressBook/etc.),
    not just the system-prompt-only policies.py instruction that was the
    only check before this. Runs AFTER translate_text, on the final,
    already-translated text.

    Fail-open, same philosophy as translate_text: any error, timeout, or
    malformed/empty response returns None, and the caller is expected to
    fall back to the text it already had rather than block or drop a reply
    over this."""
    from app.evolution_client import run_reply_agent
    from app.microagents.reply_review import REVIEW

    # `timeout` is honored again (2026-10-22): since the review moved to the client
    # (20739d0) it was accepted and never used, and the voice narrator's
    # VOICE_NARRATION_REVIEW_TIMEOUT_S (6s, "a review that has not answered in this time
    # lets the remark go as it was drafted") stopped bounding anything -- measured
    # 3.2/14.0/5.1s per remark, heard as silences of up to 21.6s. The run is shielded:
    # past the timeout the caller goes on with its text, and the run itself finishes in
    # the background, so whatever evolution bookkeeping it does is not cut off midway.
    job = asyncio.ensure_future(run_reply_agent(REVIEW, {"text": text, "task": task}))
    job.add_done_callback(lambda j: j.cancelled() or j.exception())  # a late failure is not "never retrieved"
    try:
        result = await asyncio.wait_for(asyncio.shield(job), timeout)
    except asyncio.TimeoutError:
        log_event("plugin:voice", "review_reply_failed", reason="timeout", timeout_s=timeout)
        return None
    except Exception as exc:
        log_event("plugin:voice", "review_reply_failed", reason="request_failed", error=str(exc))
        return None
    if result.get("status") != "ok":
        log_event("plugin:voice", "review_reply_failed", reason="bad_response", error=result.get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("text"), str) or not output["text"].strip():
        log_event("plugin:voice", "review_reply_failed", reason="empty")
        return None
    return output["text"]


async def check_permission_guard(
    task: str, recent_dialogue: str, previous_block_reason: str | None = None,
    session: str | None = None, timeout: float = 190.0, facts: dict[str, Any] | None = None,
    workspace_dir: str | None = None, action_id: str | None = None, track_id: str | None = None,
) -> dict[str, Any] | None:
    """Per explicit instruction (2026-10-11): Caroline often asks a yes/no
    permission question and then, a turn or two later, does the thing
    anyway even though the user never answered -- silence must be treated
    as a refusal. The same applies, even without a question, to any
    communication sent on the user's behalf or anything affecting their
    finances: those always need a clear, specific prior approval. Backed by
    a real microagent (server-side turn.permission.guard, same escalation
    ladder as reply.review/FactMemory/etc.), not a system-prompt-only
    instruction -- deciding whether approval was actually given needs real
    judgment over the dialogue, not a keyword check.

    `previous_block_reason` (2026-10-15): the reason string from this
    session's own last block, if any -- lets the agent judge whether the
    action it is flagging NOW is the SAME one as before (needed so the
    client can keep its own escalation counter running across a user's
    CLARIFICATION of the same still-pending action, instead of resetting on
    every new message). Judged by the agent itself, semantically, from the
    whole dialogue it already reads -- never a hardcoded string comparison
    on this end, confirmed live to be unsafe: the SAME action's own reason
    text genuinely drifted in wording across two consecutive real calls.

    Returns {"needs_approval": bool, "reason": str, "same_action_as_before":
    bool} on a clean result, or None on any error/timeout/malformed response
    (fail-open, same philosophy as translate_text/review_reply: an
    infrastructure hiccup must not paralyze Caroline -- this is
    defense-in-depth on top of the system prompt, not the only line of
    defense). The caller (chat_session.py) decides what to do with None --
    typically, leave whatever gate state it already had untouched.

    Bug fix (2026-10-16), confirmed live, a real incident: the user approved
    a pending send THREE times in a row ("Подтверждаю") and Caroline kept
    re-asking anyway. Root cause traced through Caroline's own log: this
    call was failing very often (390 check_permission_guard_failed events
    over 2 days, many "HTTP 504", many a bare transport timeout) against the
    OLD 15.0s default -- nowhere near enough for turn.permission.guard's own
    2-rung ladder (apps/microagents/PermissionGuard.py's GUARD_LADDER,
    server-side MICROAGENT_CALL_TIMEOUT_S=60s per rung, hard-capped overall
    by MICROAGENT_AGENT_DEADLINE_S=180s). Because fail-open means a failed
    recheck leaves the gate state UNCHANGED (see above), a failed recheck
    right after the user's own approval silently swallowed that approval --
    the user had actually answered, the server probably even judged it
    correctly, but the client never got to see the answer because it gave up
    first. Fixed two ways: (a) timeout raised to 190.0, just past the
    server's own 180s hard ceiling, so the client no longer gives up before
    the server could possibly have finished either way; (b) retries=1 below
    (same fix already established for narration's own short-timeout calls,
    2026-10-15, see _post_json's own docstring) -- _post_json's default
    retries=3 would otherwise have silently multiplied the new, much longer
    timeout into an 9-plus-minute worst case, one real attempt is what a
    190s budget is supposed to mean.

    Pilot (2026-10-17): step 1 of the gate (turn.permission.identify --
    what the pending action is, whose identity it's under) now runs
    LOCALLY via _identify_pending_action_locally, not on Camerlengo -- see
    app/microagent_client.py's own module docstring for why. When it finds
    nothing pending, this function returns straight away without even
    contacting Camerlengo. When it finds something, its facts are sent
    ALONGSIDE this call so Camerlengo's own step 1 (turn.permission.
    identify) is skipped there too -- only step 2 (turn.permission.guard,
    the approval-policy judgment, still the evolution engine's pilot role)
    still runs server-side. Falls back to today's fully-remote behavior
    whenever the local step can't run (see that function's own docstring).

    `facts` (2026-10-18, the action-boundary tracking system): when the
    caller already ran _identify_pending_action_locally itself (see
    permission_guard.py's own _check_action_before_send, which needs the
    identify step's own matched_action_id BEFORE deciding whether this
    guard call is even needed at all), it passes the already-fetched facts
    straight through instead of this function repeating the same local
    model call a second time. None (the common case: every other call site)
    means run identify locally here, same as always.

    `workspace_dir`/`action_id`/`track_id` (2026-10-19, the evolution-feedback
    loop): forwarded straight through to run_reply_agent so a tournament
    round actually resolved by this call gets journaled against whatever
    action/track it was serving (see app/evolution_journal.py) -- the only
    call site that passes these today is permission_guard.py's own
    _check_action_before_send, since turn.permission.guard is the one
    reply-domain role evolution is piloted on; every other call site
    omits them and that round simply never attributes."""
    if facts is None:
        facts = await _identify_pending_action_locally(task, recent_dialogue)
    if facts is not None and facts.get("action_type", "none") == "none":
        # Same short-circuit Camerlengo's own cmdV2ReplyPermissionGuard
        # applies server-side when step 1 finds nothing pending -- doing it
        # here too (step 1 already ran, locally) saves the round-trip
        # entirely for what is, in practice, most turns.
        return {"needs_approval": False, "reason": "", "same_action_as_before": False}
    from app.evolution_client import run_reply_agent
    from app.microagents.permission_guard import GUARD

    agent_input: dict[str, Any] = {"task": task, "recent_dialogue": recent_dialogue, "previous_block_reason": previous_block_reason or ""}
    if facts is not None:
        agent_input.update({
            "action_type": facts.get("action_type"), "action_summary": facts.get("action_summary"),
            "recipient": facts.get("recipient"), "sender_identity": facts.get("sender_identity"),
            "wording_shown_and_accepted": bool(facts.get("wording_shown_and_accepted")),
        })
    try:
        result = await run_reply_agent(GUARD, agent_input, workspace_dir=workspace_dir, action_id=action_id, track_id=track_id)
    except Exception as exc:
        log_event("plugin:voice", "check_permission_guard_failed", reason="request_failed", error=str(exc))
        return None
    if result.get("status") != "ok":
        log_event("plugin:voice", "check_permission_guard_failed", reason="bad_response", error=result.get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("needs_approval"), bool):
        log_event("plugin:voice", "check_permission_guard_failed", reason="malformed")
        return None
    return {
        "needs_approval": output["needs_approval"], "reason": str(output.get("reason") or ""),
        "same_action_as_before": bool(output.get("same_action_as_before")),
    }


async def _identify_pending_action_locally(
    task: str, recent_dialogue: str, open_actions: list[dict[str, str]] | None = None, tool_call: str | None = None,
) -> dict[str, Any] | None:
    """turn.permission.identify, step 1 of check_permission_guard's gate,
    run LOCALLY in this process (2026-10-17 pilot -- see
    app/microagent_client.py's own module docstring for why) instead of
    remotely on Camerlengo. Returns the step-1 facts dict (action_type/
    action_summary/recipient/sender_identity/wording_shown_and_accepted/
    matched_action_id), or None when the local runtime isn't available
    (Microagent.py couldn't be resolved -- see that module's own fallback)
    or the local call itself failed -- the caller then falls back to the
    old fully-remote path (Camerlengo runs step 1 itself), never a hard
    dependency, same fail-open philosophy as every other microagent check
    in this file. Runtime.run() is synchronous (see Microagent.py's own
    module docstring); offloaded to a thread so it never blocks the event
    loop.

    `open_actions` (2026-10-18, the action-boundary tracking system): the
    calling tab's own currently-open actions ({"id", "summary"} each, see
    app/action_registry.py) -- lets this same call also resolve
    matched_action_id (permission_guard.py's own _check_action_before_send
    is the only caller that passes this; every other call site omits it,
    and the agent just reports matched_action_id=null)."""
    from app.microagent_client import build_local_runtime, new_budget
    from app.microagents.permission_identify import IDENTIFY, llm_agents as identify_agents

    runtime = build_local_runtime()
    if runtime is None:
        return None
    for agent in identify_agents():
        try:
            runtime.register(agent)
        except ValueError:
            pass  # already registered -- build_local_runtime() is called fresh per request, but be defensive
    try:
        context = runtime.new_context(budget=new_budget())
        agent_input = {"task": task, "recent_dialogue": recent_dialogue, "open_actions": open_actions or []}
        if tool_call:
            # The call itself, name and arguments (2026-10-20): its own "account"
            # argument settles whose identity it goes out under -- see the agent's
            # own prompt, THE TOOL CALL.
            agent_input["tool_call"] = tool_call
        result = await asyncio.to_thread(runtime.run, IDENTIFY, agent_input, context)
    except Exception as exc:
        log_event("plugin:voice", "permission_identify_local_failed", error=str(exc))
        return None
    if result.get("status") != "ok":
        log_event("plugin:voice", "permission_identify_local_failed", reason="bad_status", status=result.get("status"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("action_type"), str):
        log_event("plugin:voice", "permission_identify_local_failed", reason="malformed")
        return None
    return output


_SECRET_ARG = re.compile(r"pass(word|wd)?|secret|token|api[_-]?key|credential", re.IGNORECASE)


def masked_args(value: Any) -> Any:
    """Tool arguments with the value of every argument whose name says it is
    a secret (password, token, API key...) masked, at any depth -- what a
    model may be shown. Found 2026-10-20: summarize_action used to send
    email_send's SMTP password to the model provider in every prompt."""
    if isinstance(value, dict):
        return {key: ("***" if _SECRET_ARG.search(str(key)) else masked_args(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [masked_args(item) for item in value]
    return value


def tool_call_for_model(tool_name: str, args: dict[str, Any]) -> str:
    """A tool call as text for a model to read: its name and masked arguments."""
    return json.dumps({"tool": tool_name, "arguments": masked_args(args)}, ensure_ascii=False, default=str)


async def summarize_action(
    tool_name: str, args: dict[str, Any], session: str | None = None, timeout: float = 10.0,
) -> str | None:
    """Per explicit instruction (2026-10-15), after a real incident: Caroline
    sent a Telegram message AS the user (account="owner") to a business
    contact with no approval for that specific identity choice --
    check_permission_guard's own instructions already covered this exactly,
    but that agent reads only the recent dialogue TEXT, never a tool call's
    actual arguments, and nothing in the visible dialogue before the three
    sends said which account each one would use. The guard had nothing to
    see.

    Backed by the server-side turn.action.summary microagent
    (ActionSummary.py) -- deliberately cheap and NOT a judgment call: it
    only describes what a tool call will do in one plain sentence (naming
    the account/identity and recipient when present in the arguments), never
    decides whether that needs approval. The caller (chat_session.py) splices
    the sentence into the SAME dialogue check_permission_guard already reads
    (via its own extra_context parameter), so permissionGuard's own prompt
    and judgment stay completely unchanged -- it just stops being blind to
    this one fact.

    Returns the one-sentence summary, or None on any error/timeout/malformed
    response (fail-open: the caller must decide what "no summary available"
    means for whether a send may proceed -- see its own call site)."""
    from app.evolution_client import run_reply_agent
    from app.microagents.action_summary import SUMMARY

    args_json = json.dumps(masked_args(args), ensure_ascii=False, default=str)
    try:
        result = await run_reply_agent(SUMMARY, {"tool_name": tool_name, "args_json": args_json})
    except Exception as exc:
        log_event("plugin:voice", "summarize_action_failed", reason="request_failed", error=str(exc))
        return None
    if result.get("status") != "ok":
        log_event("plugin:voice", "summarize_action_failed", reason="bad_response", error=result.get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("summary"), str) or not output["summary"].strip():
        log_event("plugin:voice", "summarize_action_failed", reason="empty")
        return None
    return output["summary"]


async def fuzzy_match(query: str, candidates: list[dict[str, str]], session: str | None = None, timeout: float = 15.0) -> list[str] | None:
    """Per explicit instruction (2026-10-15): confirmed live -- messenger
    chat/contact lookups across this backend were doing hardcoded, literal
    substring matching (app/plugins/companion_contacts_store.py's own
    search_contacts()) or just dumping a raw, un-matched list back to the
    model to eyeball itself (every messenger's own *_list_chats/*_list_
    channels tool) -- "Mikhail Murashov" never found a real contact named
    "Mikhail Murashev" (one letter off). Backed by a real, domain-agnostic
    microagent (server-side search.fuzzy_match, apps/microagents/Search.py)
    -- matching is the model's own semantic judgment (transliteration,
    diminutives, typos, word order), never a hardcoded string/substring
    comparison here or on the server.

    `candidates`: a list of {"label": <a short id/number the caller can map
    back to its own data, unique per call>, "text": <what the model should
    judge the candidate by -- a name, plus any other identifying detail>}.

    Returns the list of matched labels (possibly empty, meaning no
    candidate fit -- not an error), in best-match-first order, or None on
    any error/timeout/malformed response (fail-open, same philosophy as
    every other check_*/translate_text here). The caller decides what None
    means for its own flow -- typically, fall back to whatever cruder
    matching it already had, rather than treating "couldn't ask" the same
    as "found nothing"."""
    body: dict[str, Any] = {"command": "search:fuzzyMatch", "key": CAROLINE_SW_KEY, "query": query, "candidates": candidates}
    if not session:
        session = await get_cached_v2_session()
    if session:
        body["session"] = session
    try:
        data = await _post_json(body, timeout=timeout)
    except Exception as exc:
        log_event("plugin:voice", "fuzzy_match_failed", reason="request_failed", error=str(exc))
        return None
    if data.get(".status") != "ok" or data.get("status") != "ok":
        log_event("plugin:voice", "fuzzy_match_failed", reason="bad_response", status=data.get("status"), api_reason=data.get(".reason"))
        return None
    output = data.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("matches"), list):
        log_event("plugin:voice", "fuzzy_match_failed", reason="malformed")
        return None
    return [str(m) for m in output["matches"]]


async def check_refusal_guard(task: str, assistant_reply: str, session: str | None = None, timeout: float = 15.0) -> dict[str, Any] | None:
    """Per explicit instruction (2026-10-12): a separate concern from
    check_permission_guard -- that one is about a missing user APPROVAL;
    this one is about the assistant itself voluntarily DECLINING a real
    user task on its own judgment (a real incident the same day: declining
    to modify/deploy its own permission-control code unsupervised). Backed
    by a real microagent (server-side turn.refusal.guard).

    Returns {"declined": bool, "reason": str} on a clean result, or None on
    any error/timeout/malformed response (fail-open, same philosophy as
    translate_text/check_permission_guard -- an infrastructure hiccup must
    not be mistaken for a decline, nor paralyze Caroline). The caller
    (chat_session.py) decides what to do with None -- typically, do nothing
    further, same as if the reply were not a decline."""
    from app.evolution_client import run_reply_agent
    from app.microagents.refusal_guard import GUARD as REFUSAL_GUARD

    try:
        result = await run_reply_agent(REFUSAL_GUARD, {"task": task, "assistant_reply": assistant_reply})
    except Exception as exc:
        log_event("plugin:voice", "check_refusal_guard_failed", reason="request_failed", error=str(exc))
        return None
    if result.get("status") != "ok":
        log_event("plugin:voice", "check_refusal_guard_failed", reason="bad_response", error=result.get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("declined"), bool):
        log_event("plugin:voice", "check_refusal_guard_failed", reason="malformed")
        return None
    return {"declined": output["declined"], "reason": str(output.get("reason") or "")}


async def check_background_relay_worth(text: str, task: str | None = None, session: str | None = None, timeout: float = 15.0) -> dict[str, Any] | None:
    """Per explicit instruction (2026-10-14): a real incident the same
    morning -- three of four messages relayed from Caroline's own headless
    background/reminder-chore session into the user's primary chat (via
    ChatSession.deliver_relayed_reply) were routine "nothing happened" status
    noise (a Telegram check, an in-progress mailbox cleanup, a backup
    confirmation), not genuine findings. reply.review (review_reply) already
    ran on this text inside the ORIGINATING headless session and left it
    alone -- it is clean, on-task, human-sounding text, so review_reply has
    no reason to touch it; newsworthiness is a different question it was
    never designed to answer. Backed by a real microagent (server-side
    turn.background.relay.worth), called once more here, at the actual relay
    chokepoint, where the fact that this is about to become a STANDALONE,
    UNPROMPTED interruption is known.

    `task` (2026-10-15): the reminder's own note, or a sub-agent's own brief --
    whatever this text is actually supposed to be a result OF, when known. Lets
    the microagent judge the result AGAINST the task instead of in isolation;
    confirmed live, a session that merely DESCRIBED delegating work to its own
    sub-agent ("I'll delegate X, then do Y myself") was judged worth relaying
    on its own, even though it was a plan, not a result.

    Returns {"worth_relaying": bool, "reason": str} on a clean result, or
    None once every attempt below has failed (fail-open, same philosophy as
    every other check_*/translate_text here -- an infrastructure hiccup
    must default to SHOWING the message, never to silently swallowing one
    that might have mattered). The caller (ChatSession.deliver_relayed_reply)
    treats None the same as worth_relaying=True.

    Retried (2026-10-19), per explicit instruction: this is the ONE gate
    standing between Caroline's own routine background chores (a backup
    confirming it ran, a mailbox sweep finding nothing) and the user's real,
    visible chat -- unlike an ordinary reply check, a transient infra blip
    HERE specifically means fail-open lets exactly the kind of noise this
    agent exists to catch straight through. _RELAY_WORTH_ATTEMPTS flat
    retries (no exponential backoff, same convention as every other retry
    loop in this backend) before giving up to the existing fail-open
    default -- deliberately NOT flipped to fail-closed (that risks
    swallowing a genuine finding that only failed to judge because of the
    SAME infra hiccup, not because it was actually routine), just made far
    less likely to ever actually happen."""
    from app.evolution_client import run_reply_agent
    from app.microagents.background_relay_worth import WORTH

    last_error: str | None = None
    for attempt in range(_RELAY_WORTH_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_RELAY_WORTH_RETRY_DELAY_S)
        try:
            result = await run_reply_agent(WORTH, {"task": task or "", "text": text})
        except Exception as exc:
            last_error = str(exc)
            log_event("plugin:voice", "check_background_relay_worth_attempt_failed", reason="request_failed", attempt=attempt, error=last_error)
            continue
        if result.get("status") != "ok":
            last_error = result.get("error")
            log_event("plugin:voice", "check_background_relay_worth_attempt_failed", reason="bad_response", attempt=attempt, error=last_error)
            continue
        output = result.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("worth_relaying"), bool):
            last_error = "malformed"
            log_event("plugin:voice", "check_background_relay_worth_attempt_failed", reason="malformed", attempt=attempt)
            continue
        return {"worth_relaying": output["worth_relaying"], "reason": str(output.get("reason") or "")}
    log_event("plugin:voice", "check_background_relay_worth_failed", attempts=_RELAY_WORTH_ATTEMPTS, error=last_error)
    return None


async def check_reply_expected(task: str, channel: str, sent_message: str, session: str | None = None, timeout: float = 15.0) -> dict[str, Any] | None:
    """Per explicit instruction (2026-10-12): after Caroline sends ANY
    message (email or messenger), decide whether a reply is actually
    expected -- a question/request left open versus a pure FYI nobody need
    answer -- so chat_session.py can arm its own tapering follow-up checks
    (email only; every messenger channel already notifies on arrival via
    its own push connection or, for Ratatosk, a tight global poll, so
    there is nothing useful to schedule there -- see
    _on_outbound_message_sent's own docstring). Backed by a real
    microagent (server-side turn.reply.expected).

    Returns {"needs_reply": bool, "reason": str} on a clean result, or None
    on any error/timeout/malformed response (fail-open, same philosophy as
    check_permission_guard/check_refusal_guard -- an infrastructure hiccup
    must not be mistaken for "no reply needed", it just means the caller
    skips watching this once)."""
    from app.evolution_client import run_reply_agent
    from app.microagents.reply_expected import EXPECTED

    try:
        result = await run_reply_agent(EXPECTED, {"task": task, "channel": channel, "sent_message": sent_message})
    except Exception as exc:
        log_event("plugin:voice", "check_reply_expected_failed", reason="request_failed", error=str(exc))
        return None
    if result.get("status") != "ok":
        log_event("plugin:voice", "check_reply_expected_failed", reason="bad_response", error=result.get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("needs_reply"), bool):
        log_event("plugin:voice", "check_reply_expected_failed", reason="malformed")
        return None
    return {"needs_reply": output["needs_reply"], "reason": str(output.get("reason") or "")}


async def _run_local_microagent(agent_name: str, agent_module: str, agent_input: dict[str, Any]) -> dict[str, Any] | None:
    """Shared body for the two action-boundary/delivery-completeness agents
    below -- both are new, Caroline-specific, non-evolvable client agents
    (never deployed to Camerlengo, see each module's own docstring), run
    the exact same way _identify_pending_action_locally already runs
    turn.permission.identify: a fresh local Microagent.Runtime, one
    registration, one call, offloaded to a thread since Runtime.run() is
    synchronous. Returns the raw {"status", "output", "error"} dict, or
    None when the local runtime itself isn't available (Microagent.py
    couldn't be resolved) -- fail-open, same philosophy as every other
    microagent check in this file."""
    from app.microagent_client import build_local_runtime, new_budget
    import importlib

    runtime = build_local_runtime()
    if runtime is None:
        return None
    module = importlib.import_module(agent_module)
    for agent in module.llm_agents():
        try:
            runtime.register(agent)
        except ValueError:
            pass
    context = runtime.new_context(budget=new_budget())
    return await asyncio.to_thread(runtime.run, agent_name, agent_input, context)


async def check_action_sanction(message: str, open_actions: list[dict[str, str]]) -> list[dict[str, Any]] | None:
    """Step 3 of the action-boundary tracking system (2026-10-18): after one
    real user message, decide whether it grants/revokes sanction for any of
    the tab's currently open actions. The caller (chat_session_mixins/
    permission_guard.py's _check_action_sanction) already skips calling
    this at all when open_actions is empty -- no cost when nothing is
    pending.

    Returns a list of {"action_id", "decision"} dicts (decision in
    {"approved", "revoked", "unchanged"}), or None on any error/malformed
    response -- fail-open: the caller leaves every action's flag exactly as
    it was."""
    from app.microagents.action_sanction import SANCTION

    try:
        result = await _run_local_microagent(SANCTION, "app.microagents.action_sanction", {"message": message, "open_actions": open_actions})
    except Exception as exc:
        log_event("plugin:voice", "check_action_sanction_failed", reason="request_failed", error=str(exc))
        return None
    if result is None or result.get("status") != "ok":
        log_event("plugin:voice", "check_action_sanction_failed", reason="bad_response", error=(result or {}).get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("decisions"), list):
        log_event("plugin:voice", "check_action_sanction_failed", reason="malformed")
        return None
    return output["decisions"]


async def check_delivery_completeness(final_text: str, recent_dialogue: str) -> dict[str, Any] | None:
    """Per explicit instruction (2026-10-18), after a real incident:
    checks whether `final_text` (the exact text about to reach the user,
    post-review/post-translate) actually contains everything it claims or
    implies it is delivering right now. Called from
    chat_session_mixins/dialogue_transcript.py on EVERY outbound text
    block.

    Returns {"complete": bool, "missing": str}, or None on any error/
    malformed response -- fail-open: the caller treats None as complete
    (an infrastructure hiccup must never itself trigger a corrective
    note)."""
    from app.microagents.delivery_completeness import COMPLETENESS

    try:
        result = await _run_local_microagent(
            COMPLETENESS, "app.microagents.delivery_completeness", {"final_text": final_text, "recent_dialogue": recent_dialogue},
        )
    except Exception as exc:
        log_event("plugin:voice", "check_delivery_completeness_failed", reason="request_failed", error=str(exc))
        return None
    if result is None or result.get("status") != "ok":
        log_event("plugin:voice", "check_delivery_completeness_failed", reason="bad_response", error=(result or {}).get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("complete"), bool):
        log_event("plugin:voice", "check_delivery_completeness_failed", reason="malformed")
        return None
    return {"complete": output["complete"], "missing": str(output.get("missing") or "")}


async def check_question_already_answered(final_text: str, recent_dialogue: str) -> dict[str, Any] | None:
    """Per explicit instruction (2026-10-20), after a real incident: checks
    whether `final_text` (a clarifying question about to reach the user)
    was already answered earlier in the dialogue, even if worded
    differently -- see app/microagents/question_already_answered.py's own
    docstring for the incident. Called from chat_session_mixins/
    dialogue_transcript.py on EVERY outbound text block, same pipeline
    check_delivery_completeness runs on.

    Returns {"already_answered": bool, "answer": str}, or None on any
    error/malformed response -- fail-open: the caller treats None as "not
    already answered" (an infrastructure hiccup must never itself suppress
    a real question the user hasn't actually answered yet)."""
    from app.microagents.question_already_answered import ALREADY_ANSWERED

    try:
        result = await _run_local_microagent(
            ALREADY_ANSWERED, "app.microagents.question_already_answered",
            {"final_text": final_text, "recent_dialogue": recent_dialogue},
        )
    except Exception as exc:
        log_event("plugin:voice", "check_question_already_answered_failed", reason="request_failed", error=str(exc))
        return None
    if result is None or result.get("status") != "ok":
        log_event("plugin:voice", "check_question_already_answered_failed", reason="bad_response", error=(result or {}).get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("already_answered"), bool):
        log_event("plugin:voice", "check_question_already_answered_failed", reason="malformed")
        return None
    return {"already_answered": output["already_answered"], "answer": str(output.get("answer") or "")}


async def score_reaction(
    message: str, open_actions: list[dict[str, str]], candidate_track_ids: list[str],
) -> dict[str, Any] | None:
    """turn.reaction.score, Agent 1 of the evolution-feedback loop (per
    explicit special order, 2026-10-19). Called from chat_session_mixins/
    permission_guard.py's _check_reaction_score on every real user message.

    Returns {"reaction_score", "target_action_id", "target_track_id"}, or
    None on any error/malformed response -- fail-open: the caller treats
    None exactly like a 0.0 score with no target."""
    from app.microagents.reaction_score import SCORE

    try:
        result = await _run_local_microagent(
            SCORE, "app.microagents.reaction_score",
            {"message": message, "open_actions": open_actions, "candidate_track_ids": candidate_track_ids},
        )
    except Exception as exc:
        log_event("plugin:voice", "score_reaction_failed", reason="request_failed", error=str(exc))
        return None
    if result is None or result.get("status") != "ok":
        log_event("plugin:voice", "score_reaction_failed", reason="bad_response", error=(result or {}).get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("reaction_score"), (int, float)):
        log_event("plugin:voice", "score_reaction_failed", reason="malformed")
        return None
    return output


async def run_evolution_backprop(
    target_summary: str, reaction_score: float, recent_rounds: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """evolution.backprop, Agent 2 of the evolution-feedback loop (per
    explicit special order, 2026-10-19). Called from chat_session_mixins/
    permission_guard.py's _run_evolution_backprop, only when Agent 1 found a
    non-trivial, attributed reaction with at least one matching journaled
    round.

    Returns {"adjustments": [...]} (possibly empty), or None on any error/
    malformed response -- fail-open: the caller applies nothing."""
    from app.microagents.evolution_backprop import BACKPROP

    try:
        result = await _run_local_microagent(
            BACKPROP, "app.microagents.evolution_backprop",
            {"target_summary": target_summary, "reaction_score": reaction_score, "recent_rounds": recent_rounds},
        )
    except Exception as exc:
        log_event("plugin:voice", "evolution_backprop_failed", reason="request_failed", error=str(exc))
        return None
    if result is None or result.get("status") != "ok":
        log_event("plugin:voice", "evolution_backprop_failed", reason="bad_response", error=(result or {}).get("error"))
        return None
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("adjustments"), list):
        log_event("plugin:voice", "evolution_backprop_failed", reason="malformed")
        return None
    return output


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
# Split (2026-10-15) into two tiers:
# - _NARRATION_DRAFT_GARBAGE_PATTERNS: the full set, for what this was all
#   built catching in the first place -- narration-draft-specific tells
#   that only matter when checking generate_progress_comment's raw draft
#   before translation, where "Caroline wrote her own name" or "sounds
#   like a recap of my own prompt" really is a reliable tell of a broken
#   translation, where "Caroline wrote her own name" or "sounds like a
#   recap of my own prompt" really is a reliable tell of a broken
#   narration. Used by _looks_like_narration_garbage(draft_only=True).
# - _GENERAL_GARBAGE_PATTERNS: the narrower subset safe to apply to ANY
#   text (translate_text's post-translation output -- real replies,
#   relayed background messages, permission-guard asks, not just
#   narration drafts). Bug fix (2026-10-15), confirmed live: the full
#   list's `\bCaroline\b` pattern (added for the narration-draft case,
#   where her own name in third person means a broken remark) rejected a
#   genuinely correct Russian translation of a real relayed reply that
#   legitimately mentioned her by name ("...а не через Caroline") --
#   translate_text fell back to the untranslated English original, which
#   is exactly the English-leak bug this filter exists to prevent. Same
#   reasoning for every other pattern here that's about the SHAPE of a
#   narration draft specifically (third-person recap, "short remark",
#   "<narration>" tag vocabulary, "which one should I") rather than a
#   genuine cross-content failure (refusal, JSON leak, catalog/error
#   leak) that can show up in any piece of text regardless of its origin.
_GENERAL_GARBAGE_PATTERNS = [
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
    # Bug fix (2026-10-03): a literal "[tool_name(args)]" fragment leaking
    # through is a genuine cross-content tell (echoed tool-call shape),
    # not narration-draft-specific -- kept in the general set too.
    re.compile(r"\[\s*\w+\([^)]*\)\s*\]"),
]

_NARRATION_DRAFT_GARBAGE_PATTERNS = [
    *_GENERAL_GARBAGE_PATTERNS,
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
    # Bug fix (2026-10-03), confirmed live ("вкладка 3 запустила эмулятор,
    # но сама этого не поняла"): this remark can never actually receive a
    # reply -- the real task keeps going regardless of what's written here
    # -- so a question that reads as waiting for the user to pick between
    # options is always false: by the time it's shown, the real work has
    # typically already moved past that exact choice. Catches the common
    # English phrasings this drafts in (the prompt forces English); doesn't
    # need to cover every language since this runs on the pre-translation
    # draft, same reasoning as the Cyrillic check below.
    re.compile(r"\bwhich (?:one|of (?:them|these|those))\b.{0,40}\bshould i\b", re.IGNORECASE),
    re.compile(r"\bshould i (?:use|pick|choose|launch|run|start) .{0,40}\bor\b", re.IGNORECASE),
    re.compile(r"\bwhat would you like me to\b", re.IGNORECASE),
]
_NARRATION_MAX_CHARS = 400
# Han / Hiragana / Katakana / Hangul. Progress narration for this product
# is only ever asked for in a European language; a CJK reply is the SMALL
# model flailing, never legitimate here.
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def _looks_like_narration_garbage(
    text: str, language: str = "", max_chars: int = _NARRATION_MAX_CHARS, draft_only: bool = False,
) -> bool:
    """`draft_only=True` is for generate_progress_comment's own raw,
    pre-translation small-model draft, where the fuller set of narration-
    specific tells (self-reference, third-person recap, prompt-vocabulary
    echoes) applies. Every other caller (translate_text's post-translation
    check, applied to arbitrary real text) gets the narrower general set --
    see the two pattern lists' own comments for why."""
    if len(text) > max_chars:
        return True
    if _CJK_RE.search(text) and not re.search(r"chin|japan|korea|mandarin|中文", language, re.IGNORECASE):
        return True
    # Bug fix (2026-09-28), confirmed live via translate_text's own real
    # incident: the CJK check above catches a wrong-script reply for a
    # Chinese/Japanese/Korean target, but had no equivalent for Cyrillic --
    # the one that actually bit in practice. A translation call can return
    # well-formed, non-refusal, correctly-tagged prose that is simply still
    # in the SOURCE language (confirmed live: a "translate into Russian"
    # call whose entire reply was fluent, grammatical English -- nothing
    # about it looked like a refusal, JSON leak, or garbage by any of the
    # checks below, so it passed straight through). Same shape as the CJK
    # check: when the target language is Cyrillic-scripted, a reply with
    # real Latin letters and not a single Cyrillic one did not actually
    # translate, regardless of how well-formed it otherwise reads.
    if re.search(r"russ|ukrain|belarus|bulgar|serb|macedon", language, re.IGNORECASE):
        if _LATIN_LETTER_RE.search(text) and not _CYRILLIC_RE.search(text):
            return True
    patterns = _NARRATION_DRAFT_GARBAGE_PATTERNS if draft_only else _GENERAL_GARBAGE_PATTERNS
    return any(p.search(text) for p in patterns)


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


# Ported from a teammate's branch (2026-10-09/-11): the news can be a single
# word -- "Нашла курс доллара к гривне." after "Ищу курс доллара к гривне."
# shares every other word with it and was dropped as a repeat three times
# out of three on their golden set. A remark that reports an outcome --
# found, saved, nothing, not -- where the earlier one reported none (or a
# different one) is new, whatever else the two share.
_OUTCOME_WORDS = re.compile(
    r"\b(?:(нашл|найден|получил|узнал|выяснил|сохранил|отправил|создал|записал|found|got|saved|sent|created)\w*"
    r"|(ничего|нет|не|nothing|none|not|no))\b",
    re.IGNORECASE,
)


def _outcomes(text: str) -> set[str]:
    return {(m.group(1) or m.group(2)).lower() for m in _OUTCOME_WORDS.finditer(text.lower().replace("ё", "е"))}


def _is_repeat(text: str, previous: str, ignore_stems: frozenset[str] = frozenset()) -> bool:
    """Same remark in other words. _normalized_overlap only catches near-
    verbatim containment; "я всё ещё проверяю прогноз..." vs "я все еще ищу
    прогноз..." differ in one verb and a letter. Word-set overlap catches
    those while a remark with genuinely new content shares few words with
    the old one. Words of one or two letters don't count: "Нашла прогноз на
    завтра в Киеве." after "Ищу прогноз погоды на завтра в Киеве." is the
    news itself -- the search is over -- and would otherwise be dropped as
    a repeat on the strength of "на" and "в"."""
    if _outcomes(text) != _outcomes(previous):
        return False  # before the containment check: "Не нашла ..." contains "Нашла ..."
    if _normalized_overlap(text, previous):
        return True

    def words(s: str) -> set[str]:
        s = s.lower().replace("ё", "е").translate(_CYRILLIC_LATIN_CONFUSABLES)
        return {w for w in re.findall(r"\w+", s) if len(w) > 2 and w[:_STEM_LEN] not in ignore_stems}

    a, b = words(text), words(previous)
    if len(a) < 3 or len(b) < 3:
        return False
    return len(a & b) / len(a | b) >= 0.6


# A crude stem: enough to match "Киеве" in a request with "Киева" in a remark.
_STEM_LEN = 5


def _request_stems(task: str) -> frozenset[str]:
    """The request's own words, as stems (see _is_repeat's ignore_stems). Live 2026-10-15:
    "Какая погода будет завтра в Киеве, Львове и Одессе?" -- every remark about it is
    mostly those words, so every new draft looked like a repeat of the last one by word
    overlap, and the user heard nothing for 75s in the middle of the turn. What the
    request is about is not news either way; what is said about it is."""
    s = task.lower().replace("ё", "е").translate(_CYRILLIC_LATIN_CONFUSABLES)
    return frozenset(w[:_STEM_LEN] for w in re.findall(r"\w+", s) if len(w) > 2)


def _already_said_block(already_said: list[str] | None) -> str:
    """Ported from a teammate's branch (2026-10-09/-10): without its own
    earlier remarks in view, the model restates the same status each time
    -- the "talking to herself" stream consecutive_narration_count already
    tracks. None (the default) means the caller never passes this at all,
    same as before this was added -- no change in behavior. An empty list
    means a real episode with nothing said yet: that the work has started
    is itself a fact worth saying once, not silence."""
    if already_said is None:
        return ""
    if not already_said:
        return (
            "Nothing has been said to the user about this request yet. If STATUS shows the work has started, "
            "say so in one short present-tense sentence in the user's own terms (what you're looking into) -- "
            "that is a fact, not a promise.\n\n"
        )
    lines = "\n".join(f"- {remark}" for remark in already_said[-6:])
    # "...and STATUS does not say the user has heard nothing": only the voice loop's
    # STATUS ever says that (chat_session.py's _step_voice_narration) -- there, staying
    # silent for want of news is the one thing not to do.
    return (
        "ALREADY SAID to the user during this request (they heard these; never repeat or rephrase them -- say "
        "only what STATUS adds beyond them, or reply with exactly <narration></narration> if it adds nothing "
        "and STATUS does not say the user has heard nothing for a while):\n"
        f"---\n{lines}\n---\n\n"
    )


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
# sw-mode work-log examples (2026-10-09) -- see generate_progress_comment's STATUS rules.
_NARRATION_WORKLOG_GOOD = "I've found two emails from Anna about the invoice."
_NARRATION_WORKLOG_BAD = "Running the mail search on the first tier now."
_NARRATION_DEADEND_GOOD = "The web search didn't give me the timetable, and my notes have nothing on it either."
_NARRATION_STEP_START_GOOD = "I'm checking what I already have on the Lisbon flights."
# Main's own example pair for a figure in a result (see generate_progress_comment's prompt).
_NARRATION_PRELIMINARY_GOOD = "I found a rate around 1.0847, confirming it now."
_NARRATION_PRELIMINARY_BAD = "The rate is 1.0847."
# The found thing is the awaited answer itself (the voice loop's prompt): golden set,
# 2026-10-11 -- with the rule above alone both drafting models read a forecast out whole.
_NARRATION_FOUND_GOOD = "I've found a forecast for Oslo and I'm checking it now."
_NARRATION_FOUND_BAD = "Tomorrow in Oslo it will be sunny, up to seventy-five degrees."
_NARRATION_STILL_GOING_GOOD = "Still working through the quarterly numbers."

# Ported from a teammate's branch (commit 69b8d20), confirmed live there:
# "Закончила считать дни до Нового года." was drafted from "Checking the
# result", two seconds before the real checked answer streamed -- and
# would have been false had the check sent the work back. The prompt's
# own rule (finished only if STATUS says so) isn't airtight on its own;
# this is the deterministic backstop. Narration only ever runs while the
# turn is still genuinely pending (see chat_session.py's
# _check_progress_narration), so a real "finished" claim is never
# legitimate here -- unconditional, no STATUS marker to check against.
_FINISH_CLAIM = re.compile(
    r"\b(закончил[аи]?|завершил[аи]?|готов[оаы]?|всё\s+сделал[аи]?|все\s+сделал[аи]?|сделано|"
    r"finished|completed|done|all set)\b",
    re.IGNORECASE,
)


def _is_echo_of_prompt_example(text: str) -> bool:
    return any(
        _normalized_overlap(text, example)
        for example in (
            _NARRATION_EXAMPLE_BAD, _NARRATION_EXAMPLE_GOOD, _NARRATION_EXAMPLE_TAG,
            _NARRATION_EXAMPLE_THIRDPERSON_BAD, _NARRATION_EXAMPLE_THIRDPERSON_GOOD,
            _NARRATION_COMBINED_BAD, _NARRATION_COMBINED_GOOD,
            _NARRATION_WORKLOG_GOOD, _NARRATION_WORKLOG_BAD, _NARRATION_DEADEND_GOOD,
            _NARRATION_STEP_START_GOOD, _NARRATION_STILL_GOING_GOOD, _NARRATION_PRELIMINARY_GOOD, _NARRATION_PRELIMINARY_BAD,
            _NARRATION_FOUND_GOOD, _NARRATION_FOUND_BAD,
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
    # Bug fix (2026-10-03), confirmed live: the model wrote `<narity>...
    # </narration>` -- a misspelled/mismatched opening tag -- so the pair
    # regex above never matched, and this fell all the way through to
    # "plain prose" and returned the raw text WITH the broken tags still
    # in it straight to the user. Any leftover tag-shaped fragment here
    # (open or close, whatever its name) means the model attempted and
    # botched the <tag> contract, not that it wrote genuine plain prose --
    # a real remark has no reason to contain angle-bracket syntax at all.
    # Same "never guess past a shape we don't recognize" principle as the
    # JSON branch above -- reject rather than pass the wreckage through.
    if re.search(r"</?[a-zA-Z][\w-]*>", stripped):
        return ""
    return stripped


def _draft_language_line(draft_language: str | None) -> str:
    if draft_language is None:
        return "Write in English. A separate step translates it afterward."
    # Direct drafting (2026-10-10): a strong model writes the tab's language itself --
    # the English-draft-then-translate route left names untranslated ("встреча с
    # Oleg") and cost a second round trip.
    # Numbers as words (2026-10-10): the remark is read aloud, and a digit in it makes
    # clean_text_for_speech spend a whole model call spelling it out first.
    return (f"Write in {draft_language}, the language of the conversation. Keep people's names in the form "
            f"{draft_language} speakers would use. Write numbers, dates and temperatures out in words, "
            "exactly as they would be spoken.")


# The rule for a figure out of a tool result (main's, 2026-10-11): it may be said, as
# something found and still being confirmed, never as the settled answer. The cheap
# drafting model does not hold that from the prompt alone: on the golden set, 3 runs of
# 3, a forecast in STATUS came out as "Завтра в Киеве будет облачно с дождем утром,
# температура днем около шестидесяти градусов по Фаренгейту ..." -- the raw result read
# out as the answer, half a minute before the reply converts it. So when STATUS holds a
# figure in a result and the remark carries one, the remark must also say that it was
# found or is being checked; otherwise the next model drafts, or nothing is said.
# Russian and English words only -- in another language the prompt rule stands alone.
_RESULT_WITH_FIGURE = re.compile(r"\); result: .*\d")
_FIGURE_SAID = re.compile(
    r"\d|\b(?:"
    r"дв(?:а|е|ух|ум|умя)|тр(?:и|ех|ем|емя)|четыр\w+|пят(?:ь|и|ью)|шест(?:ь|и|ью)|сем(?:ь|и|ью)|вос(?:емь|ьми|емью)"
    r"|девят(?:ь|и|ью)|десят(?:ь|и|ью)|\w+надцат\w+|двадцат\w+|тридцат\w+|сорок\w?|\w+десят\w*|девяност\w+"
    r"|сто|ста|двести|двухсот|триста|трехсот|четыреста|\w+ьсот|тысяч\w*|миллион\w*|миллиард\w*|полтор\w+|градус\w*|процент\w*"
    r"|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\w+teen|twenty|thirty|forty|fifty|sixty|seventy|eighty"
    r"|ninety|hundred|thousand|million|billion|degrees?|percent"
    r")\b",
    re.IGNORECASE,
)
_FOUND_OR_CHECKING = re.compile(
    r"\b(?:нашл|найден|получил|узнал|выяснил|вижу|уточня|проверя|перепровер|сверя|подтвержд|убежда|предварительн"
    r"|found|got|see|confirm|verif|check|double-check|preliminar)\w*",
    re.IGNORECASE,
)


# A number in words counts as a figure out of a result only next to what it measures.
# Live 2026-10-15: "Продолжаю обрабатывать прогнозы для трёх городов." was dropped by this
# check ("трёх" -- the three cities of the request), the second model's draft was the only
# usable one, and the user heard nothing for over a minute.
_MEASURE_SAID = re.compile(
    r"\b(?:градус\w*|процент\w*|гривн\w*|гривен|доллар\w*|евро|рубл\w*|курс\w*|цел(?:ых|ая)|тысячн\w*|сотых|десятых|"
    r"метр\w*|километр\w*|миль|милях|degrees?|percent|dollars?|euros?|hryvni?as?|rate|miles?)\b",
    re.IGNORECASE,
)


def _states_figure_as_settled(text: str, activity: str, task: str = "") -> bool:
    if not _RESULT_WITH_FIGURE.search(activity):
        return False
    said = text.lower().replace("ё", "е")
    # A number the request itself has is part of what is asked about, not a figure out of
    # a result. Live 2026-10-15: "Я углубляю поиск цен на iPhone 17 ..." was dropped for
    # its "17", twice, while the user had heard nothing for half a minute.
    for number in set(re.findall(r"\d+", task)):
        said = re.sub(rf"(?<!\d){number}(?!\d)", " ", said)
    figure = bool(re.search(r"\d", said)) or (bool(_FIGURE_SAID.search(said)) and bool(_MEASURE_SAID.search(said)))
    return figure and not _FOUND_OR_CHECKING.search(said)


def _progress_comment_prompt(
    task: str, activity: str, already_said: list[str] | None, draft_language: str | None, gender_clause: str = "",
) -> str:
    """The voice loop's narrator prompt (generate_progress_comment_direct): the rules of
    generate_progress_comment's own prompt below, plus what a remark every few seconds
    over a raw work log needs -- the work log itself, a step that has just started,
    and the user having heard nothing for a while.

    Figures in a result (2026-10-11): this branch first kept them from the narrator
    (masked as "<number>"), after a forecast that came back in Fahrenheit was read out
    as "шестьдесят градусов" for Kyiv in October. Main decided otherwise the same day
    -- a figure in STATUS is real and may be said, as a preliminary finding still being
    confirmed -- and that rule is the one here now, in main's words."""
    return (
        "CRITICAL: a program parses your reply, not a person. Reply with ONLY a <narration> tag around ONE "
        "short sentence, no JSON, no markdown, no preamble.\n\n"
        "You speak to the user about where their request stands right now: how far along the work is, or that "
        "it's finished. Use first person, plain everyday words, present tense.\n"
        "Never mention tools, functions, commands, systems, files, processes, session mechanics or any internal "
        "machinery -- describe the progress of the task itself, in the terms the user used.\n"
        "Use only what is in TASK and STATUS. Never invent results, names, numbers, dates or details; never "
        "guess the user's intent or mood; never promise or plan next steps.\n"
        "Say the task is finished ONLY if STATUS explicitly says so. Completed steps alone mean it is still in "
        "progress -- describe it that way.\n"
        "STATUS may be a raw work log: step names, model names, \"tier\"/\"уровень\", verification, raw "
        "arguments and results. Never repeat any of that machinery. But when a result in it is a real piece of "
        "the task -- something found, counted or done that the user asked about -- say THAT, in plain words, as "
        "progress so far, never as the final answer.\n"
        "If STATUS mentions a specific number or finding from a tool result, that's real data you may mention "
        "-- but it is preliminary, from work still in progress, not yet the final confirmed answer. Say you "
        "found it and are still confirming it, rather than stating it as settled.\n"
        "This matters most when what was found is the very answer the user is waiting for -- a forecast, a "
        "price, a time. Never read the result out as if it were the reply: say that you have found it, with at "
        "most its one key figure, and that you are checking it. The reply itself comes later and may still "
        "convert or correct the figures.\n"
        "A dead end is progress too: a step in STATUS that found nothing or failed is worth telling the user once, "
        "in plain words (what was looked at, that it gave nothing) -- that is concrete, not \"nothing to say\". "
        "Each different place that gave nothing is its own new fact: the web, the notes or memory, the mail -- "
        "\"nothing on the web\" being already said does not cover \"nothing in the notes either\".\n"
        "A step that has only just started is news as well: where the work is now -- your own memory, the web, "
        "the mail, the notes -- said once, in plain words, when it is a new place.\n"
        "STATUS may end with how long the request has been running, and with a line saying the user has heard "
        "nothing for a while. When that line is there and nothing else is new, do not stay silent -- on a live "
        "call silence sounds like a hang: say in one short sentence, in words different from anything already "
        "said, that the work on their request is still going on. Never guess how long is left.\n"
        "If STATUS has nothing concrete to say about the task, reply with exactly <narration></narration> and "
        "nothing else.\n\n"
        f"{_draft_language_line(draft_language)}{(' ' + gender_clause) if gender_clause else ''}\n\n"
        "Examples of the right kind of reply (copy the style, never the words -- these are unrelated to the "
        "real task below):\n"
        "  TASK: plan a three-day trip to the coast. STATUS: Steps in progress right now: 2.\n"
        "  Good: <narration>Still putting the coast trip plan together, a few more pieces to go.</narration>\n"
        "  TASK: translate the contract summary. STATUS: Steps completed so far this turn: 4 succeeded, 0 failed.\n"
        "  Good: <narration>The translation is still in progress, not quite finished yet.</narration>\n"
        "  TASK: book a table for two. STATUS: Steps completed so far this turn: 2 succeeded, 0 failed. "
        "Task finished: yes.\n"
        "  Good: <narration>Your table for two is booked.</narration>\n"
        "  TASK: review my budget spreadsheet. STATUS: nothing concrete beyond the request itself.\n"
        "  Good: <narration></narration>\n"
        "  Bad (names a machine step): <narration>Running the spreadsheet reader now.</narration>\n"
        "  TASK: find Anna's email about the invoice. STATUS: Tier 1 (openai/gpt-5-nano) is working. Tool call "
        "mail_search (arguments: {\"from\": \"Anna\"}); result: 2 messages: 'Invoice March', 'Re: invoice'\n"
        f"  Good: <narration>{_NARRATION_WORKLOG_GOOD}</narration>\n"
        f"  Bad (names the machinery): <narration>{_NARRATION_WORKLOG_BAD}</narration>\n"
        "  TASK: find train times to Lviv for Friday. STATUS: Tool call web_search (arguments: "
        "{\"question\": \"Lviv trains Friday\"}); result: I don't have access to live timetables. Tool call "
        "request_info (arguments: {\"query\": \"Lviv trains\"}); result: Memory holds nothing relevant.\n"
        f"  Good: <narration>{_NARRATION_DEADEND_GOOD}</narration>\n"
        "  TASK: when do the Lisbon flights leave. STATUS: Tool call request_info started (plugin \"memory\"; "
        "arguments: {\"query\": \"Lisbon flights\"}).\n"
        f"  Good: <narration>{_NARRATION_STEP_START_GOOD}</narration>\n"
        "  TASK: check today's EURUSD rate. STATUS: Tool call web_search (arguments: EURUSD rate); result: "
        "quote 1.0847.\n"
        f"  Good: <narration>{_NARRATION_PRELIMINARY_GOOD}</narration>\n"
        f"  Bad (states it as settled): <narration>{_NARRATION_PRELIMINARY_BAD}</narration>\n"
        "  TASK: what will the weather be in Oslo tomorrow. STATUS: Tool call web_search (arguments: "
        "{\"question\": \"Oslo weather tomorrow\"}); result: Tomorrow in Oslo: sunny, high 75 F, low 61 F.\n"
        f"  Good: <narration>{_NARRATION_FOUND_GOOD}</narration>\n"
        f"  Bad (reads the result out as the reply): <narration>{_NARRATION_FOUND_BAD}</narration>\n"
        "  TASK: sum up the quarterly numbers. STATUS: Tier 2 (openai/gpt-5.1-codex-mini) is working. Time spent "
        "on the request so far: 31 s. Nothing new has happened since the last remark, and the user has heard "
        "nothing for about 10 s.\n"
        f"  Good: <narration>{_NARRATION_STILL_GOING_GOOD}</narration>\n\n"
        f"TASK (the user's request):\n---\n{task}\n---\n\n"
        f"STATUS (what is happening):\n---\n{activity}\n---\n\n"
        f"{_already_said_block(already_said)}"
        "Reply now, wrapped in the tag."
    )


async def generate_progress_comment(
    recent_dialogue: str, language: str, session: str | None = None, timeout: float = 30.0, gender: str | None = None,
    activity: str | None = None, addressee_gender: str | None = None, task: str | None = None,
    already_said: list[str] | None = None, retries: int = 3,
) -> str | None:
    """`retries` (2026-10-15): forwarded to _post_json's own `retries` --
    narration passes 1; see _post_json's own doc comment for the real
    incident (hidden 3x multiplication of an already-short timeout) this
    fixes.

    Per explicit instruction (2026-09-10): Caroline has no way to
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
        # Bug fix (2026-10-03), confirmed live: this remark is a one-off
        # aside with no way to ever receive a reply -- the real work
        # continues on its own regardless of what you write here. A
        # question phrased as if waiting for the user to pick/decide
        # something ("which one should I...", "какое из них...") reads as
        # a real pause-and-wait, but nothing actually waits -- confirmed
        # live, the real task had already moved past the exact choice
        # being "asked" about by the time this remark was shown. Never ask "
        "the user to choose or decide anything, and never write a literal tool-call-looking fragment like "
        "\"[some_tool(arg=value)]\" even if the conversation below contains real ones -- copying that shape "
        "verbatim is never a real remark.\n"
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
    # Per explicit instruction (2026-10-06): the narrator comments ONLY on
    # actions, ONLY within the facts it's given -- never on the conversation.
    # The dialogue-based prompt above kept inventing people, dates and
    # intentions out of old chat lines, so it is no longer used at all: no
    # action facts means no narration.
    if not activity or not task:
        return None
    prompt = (
        "CRITICAL: a program parses your reply, not a person. Reply with ONLY a <narration> tag around ONE "
        "short sentence, no JSON, no markdown, no preamble.\n\n"
        "You speak to the user about where their request stands right now: how far along the work is, or that "
        "it's finished. Use first person, plain everyday words.\n"
        "Never mention tools, functions, commands, systems, files, processes, session mechanics or any internal "
        "machinery -- describe the progress of the task itself, in the terms the user used.\n"
        "Use only what is in TASK and STATUS. Never invent results, names, numbers, dates or details; never "
        "guess the user's intent or mood; never invent a step STATUS doesn't actually show.\n"
        # Bug fix (2026-10-17), confirmed live: two consecutive remarks about
        # the same already-sent email, 82 seconds apart, read as contradictory
        # ("I sent a message asking..." then "I'm writing to whoever
        # suggested this..."). The old blanket "present tense" instruction
        # gave the model no way to distinguish a step STATUS already marks
        # done from one still running or not yet started, so a later remark
        # about the same already-finished step could drift into present
        # tense as if it were still in progress. Per explicit instruction:
        # match tense/mood to what STATUS actually shows instead of forcing
        # one tense for everything.
        "Match your tense/mood to what STATUS actually shows: a step STATUS marks done is described as done "
        "(past tense is fine -- \"I sent it\", \"I found it\"); a step STATUS shows is still running is "
        "described as ongoing (\"I'm still working on X\"); a step that hasn't started yet but STATUS shows is "
        "coming next is described as a plan, explicitly (\"I'm about to...\", \"I'm going to...\", \"I still "
        "need to...\"). Never phrase a not-yet-done step as if it already happened, and never describe an "
        "already-done step as if it were still in progress or merely planned.\n"
        # Ported from a teammate's branch (commits 96de9fd/44a6036), confirmed
        # against their golden set (16/16): a step that found nothing is
        # still worth saying once, and each different place checked is its
        # own new fact -- "nothing on the web" already said doesn't cover
        # "nothing in the notes either".
        "A dead end is progress too: a step in STATUS that found nothing or failed is worth telling the user "
        "once, in plain words (what was looked at, that it gave nothing) -- that is concrete, not \"nothing to "
        "say\". Each different place that gave nothing is its own new fact: the web, the notes or memory, the "
        "mail -- \"nothing on the web\" being already said does not cover \"nothing in the notes either\".\n"
        "Say the task is finished ONLY if STATUS explicitly says so. Completed steps alone mean it is still in "
        "progress -- describe it that way.\n"
        # Per explicit instruction (2026-10-11): a figure STATUS does show
        # you is real, not invented -- say it. But everything in STATUS is
        # from work still in progress, never yet the finished, confirmed
        # answer: a number from a tool result can still change before the
        # real reply. State it as a preliminary finding you're still
        # confirming, not as the settled answer.
        "If STATUS mentions a specific number or finding from a tool result, that's real data you may mention "
        "-- but it is preliminary, from work still in progress, not yet the final confirmed answer. Say you "
        "found it and are still confirming it, rather than stating it as settled.\n"
        "If STATUS has nothing concrete to say about the task, reply with exactly <narration></narration> and "
        "nothing else.\n"
        # Bug fix (2026-10-16), confirmed live: TASK was an already-answered
        # question ("Did Oksana reply?" -- she had, Caroline had already told
        # the user so), but STATUS by then was about a completely different,
        # later piece of work (identifying an unrelated incoming message) and
        # said nothing about Oksana either way. Told to describe "where TASK
        # stands" from STATUS, the model invented a plausible-sounding update
        # for TASK anyway ("she still hasn't replied, still working on it") --
        # flatly contradicting what had already been said. STATUS not
        # mentioning TASK at all is exactly the "nothing concrete to say about
        # the task" case above, not license to guess -- added as its own
        # explicit rule plus a matching example, since the existing wording
        # evidently wasn't enough on its own for this one.
        "STATUS describing work that has nothing to do with TASK is ALSO \"nothing concrete to say about the "
        "task\" -- reply empty rather than guessing where TASK stands from it. Never state or imply TASK is "
        "still open, unresolved, or being worked on unless STATUS is actually about TASK; STATUS being about "
        "something else entirely is not evidence either way about TASK.\n\n"
        "Write in English. A separate step translates it afterward.\n\n"
        "Examples of the right kind of reply (copy the style, never the words -- these are unrelated to the "
        "real task below):\n"
        "  TASK: plan a three-day trip to the coast. STATUS: Steps in progress right now: 2.\n"
        "  Good: <narration>Still putting the coast trip plan together, a few more pieces to go.</narration>\n"
        "  TASK: translate the contract summary. STATUS: Steps completed so far this turn: 4 succeeded, 0 failed.\n"
        "  Good: <narration>The translation is still in progress, not quite finished yet.</narration>\n"
        "  TASK: did Oksana reply to my email. STATUS: Steps in progress right now: 1 (identifying the sender "
        "of an unrelated WhatsApp message).\n"
        "  Good: <narration></narration>\n"
        "  Bad (STATUS is unrelated to TASK, not evidence TASK is still open): <narration>She still hasn't "
        "replied, still working on it.</narration>\n"
        "  TASK: book a table for two. STATUS: Steps completed so far this turn: 2 succeeded, 0 failed. "
        "Task finished: yes.\n"
        "  Good: <narration>Your table for two is booked.</narration>\n"
        "  TASK: check today's EURUSD rate. STATUS: Tool call web_search (arguments: EURUSD rate); result: "
        "quote 1.0847.\n"
        "  Good: <narration>I found a rate around 1.0847, confirming it now.</narration>\n"
        "  Bad (states it as settled): <narration>The rate is 1.0847.</narration>\n"
        "  TASK: review my budget spreadsheet. STATUS: nothing concrete beyond the request itself.\n"
        "  Good: <narration></narration>\n"
        "  Bad (names a machine step): <narration>Running the spreadsheet reader now.</narration>\n"
        "  TASK: ask the vendor whether they can update the firmware. STATUS: Tool call email_send (arguments: "
        "to vendor); result: sent.\n"
        "  Good (STATUS marks this step done -- past tense, not a plan): <narration>I sent them a message "
        "asking whether they can update the firmware.</narration>\n"
        "  Bad (STATUS says it's sent, not still being written): <narration>I'm writing to them about the "
        "firmware now.</narration>\n"
        "  TASK: ask the vendor whether they can update the firmware. STATUS: Steps in progress right now: 1 "
        "(drafting the message, not yet sent).\n"
        "  Good (STATUS marks this step not-yet-done -- an explicit plan, not stated as already happened): "
        "<narration>I'm about to send them a message asking whether they can update the firmware.</narration>\n"
        "  Bad (claims a not-yet-sent message as already sent): <narration>I sent them a message about the "
        "firmware.</narration>\n\n"
        f"{_already_said_block(already_said)}"
        f"TASK (the user's request):\n---\n{task}\n---\n\n"
        f"STATUS (what is happening right now):\n---\n{activity}\n---\n\n"
        "Reply now, wrapped in the tag."
    )
    body: dict[str, Any] = {"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": prompt, "model": "SMALL"}
    if not session:
        session = await get_cached_v2_session()
    if session:
        body["session"] = session
    try:
        data = await _post_json(body, timeout=timeout, retries=retries)
    except Exception as exc:
        log_event("plugin:voice", "generate_progress_comment_request_failed", error=str(exc), error_type=type(exc).__name__, timeout_s=timeout)
        return None
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        log_event("plugin:voice", "generate_progress_comment_bad_response", status=data.get(".status"), reason=data.get(".reason"))
        return None
    if re.fullmatch(r"\s*<narration>\s*</narration>\s*", data["result"]):
        # The prompt's own "nothing new to say" reply (2026-10-09): a deliberate answer,
        # not a broken one -- "" lets the voice loop wait for new facts instead of
        # re-asking the same question three times in a row (seen live: 9 such retries
        # in one turn). Still falsy, so the 60s path treats it exactly as before.
        log_event("plugin:voice", "generate_progress_comment_nothing_new")
        return ""
    text = _extract_tagged_text(data["result"], "narration")
    if not text:
        log_event("plugin:voice", "generate_progress_comment_unextractable", raw=data["result"][:300])
        return None
    # Bug fix (2026-09-30), confirmed live: this draft is always written in
    # English by this function's own prompt ("a separate step translates it
    # into the right language afterward" -- see translate_text() below).
    # Passing the real target `language` here makes _looks_like_narration_
    # garbage's Cyrillic-script check (added 2026-09-28 for translate_text's
    # OWN post-translation output) reject this pre-translation English draft
    # on every single call for any Russian/Ukrainian/Belarusian/Bulgarian/
    # Serbian/Macedonian conversation -- confirmed live, 100% rejection rate
    # all day, zero narrations ever sent. Omit `language` so only the
    # script-agnostic checks (refusals, meta-commentary, CJK) apply here;
    # the real post-translation Cyrillic check still runs correctly inside
    # translate_text() itself just below.
    if _looks_like_narration_garbage(text, draft_only=True):
        log_event("plugin:voice", "generate_progress_comment_rejected_garbage", text=text[:300])
        return None
    if _is_echo_of_dialogue(text, recent_dialogue):
        log_event("plugin:voice", "generate_progress_comment_rejected_echo", text=text[:300])
        return None
    if _is_echo_of_prompt_example(text):
        log_event("plugin:voice", "generate_progress_comment_rejected_example_echo", text=text[:300])
        return None
    if _FINISH_CLAIM.search(text):
        log_event("plugin:voice", "generate_progress_comment_rejected_finish_claim", text=text[:300])
        return None
    if already_said and any(_is_repeat(text, prev) for prev in already_said):
        log_event("plugin:voice", "generate_progress_comment_rejected_repeat", text=text[:300])
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
    translated = await translate_text(text, language, session=session, timeout=timeout, gender=gender, addressee_gender=addressee_gender)
    final_text = translated or text
    if translated is None:
        log_event("plugin:voice", "generate_progress_comment_translate_failed_using_original", text=text[:300])
    if already_said and any(_is_repeat(final_text, prev) for prev in already_said):
        # The ALREADY SAID block is only an ask -- seen live, the small model said
        # "I'm still checking the forecast" five times in one turn regardless.
        log_event("plugin:voice", "generate_progress_comment_rejected_repeat", text=final_text[:300])
        return ""
    log_event("plugin:voice", "generate_progress_comment_ok", dialogue_chars=len(recent_dialogue), text=final_text)
    return final_text


async def generate_progress_comment_direct(
    task: str, activity: str, already_said: list[str], language: str,
    resolve: Callable[[str], Awaitable[str]], gender: str | None = None, addressee_gender: str | None = None,
    repeat_window: int | None = None, max_chars: int | None = None, too_long: str | None = None,
) -> str | None:
    """One-call narration (2026-10-10): drafted straight in `language` by the model
    behind `resolve` (prompt -> raw reply), checked exactly like
    generate_progress_comment's output. Same return contract: the remark, "" for
    nothing new to say (including a repeat of `already_said`), None on failure --
    callers fall back to generate_progress_comment.

    max_chars: a remark longer than this is asked for once more, shorter (`too_long` is
    the draft that was too long); a second long one is dropped as "".

    Measured on a narrator golden set before switching (see the commit): the
    two-call SMALL + translate route vs this one."""
    if not activity or not task:
        return None
    # The English-draft route gets grammatical gender from translate_text(gender=...);
    # drafting directly, the clause has to be in the prompt itself -- seen live without
    # it: "Я искал контекст..." from Caroline.
    prompt = _progress_comment_prompt(
        task, activity, already_said, draft_language=language,
        gender_clause=_gender_agreement_clause(gender, addressee_gender),
    )
    if too_long:
        prompt += (
            f"\n\nA draft of this remark was too long to be said aloud while the user waits: \"{too_long}\". "
            "Say it again as ONE short sentence of at most 14 words, with at most one figure and no list of "
            "places or prices."
        )
    try:
        raw = (await resolve(prompt)) or ""
    except Exception as exc:  # noqa: BLE001
        log_event("plugin:voice", "generate_progress_comment_direct_failed", error=str(exc)[:300])
        return None
    if raw.startswith("Agent error"):
        log_event("plugin:voice", "generate_progress_comment_direct_failed", error=raw[:300])
        return None
    if re.fullmatch(r"\s*<narration>\s*</narration>\s*", raw):
        log_event("plugin:voice", "generate_progress_comment_nothing_new", direct=True)
        return ""
    text = _extract_tagged_text(raw, "narration")
    if not text:
        log_event("plugin:voice", "generate_progress_comment_unextractable", raw=raw[:300], direct=True)
        return None
    if _looks_like_narration_garbage(text, language) or _is_echo_of_prompt_example(text):
        log_event("plugin:voice", "generate_progress_comment_rejected_garbage", text=text[:300], direct=True)
        return None
    # repeat_window: how many of the latest remarks a new one may not repeat; None, all of
    # them. See chat_session.py's _step_voice_narration for when it is narrowed.
    recent = already_said[-repeat_window:] if repeat_window else already_said
    # After a silence (repeat_window set) the request's own words are left out of the
    # comparison too: near-verbatim repeats are still caught by _normalized_overlap.
    ignore = _request_stems(task) if repeat_window else frozenset()
    if recent and any(_is_repeat(text, prev, ignore) for prev in recent):
        log_event("plugin:voice", "generate_progress_comment_rejected_repeat", text=text[:300], direct=True)
        return ""
    if _FINISH_CLAIM.search(text):
        log_event("plugin:voice", "generate_progress_comment_rejected_finish_claim", text=text[:300], direct=True)
        return ""
    if _states_figure_as_settled(text, activity, task):
        log_event("plugin:voice", "generate_progress_comment_rejected_settled_figure", text=text[:300], direct=True)
        return ""
    if max_chars and len(text) > max_chars:
        log_event("plugin:voice", "generate_progress_comment_rejected_too_long", text=text[:300], direct=True, retried=bool(too_long))
        if too_long:
            return ""
        return await generate_progress_comment_direct(
            task, activity, already_said, language, resolve, gender=gender, addressee_gender=addressee_gender,
            repeat_window=repeat_window, max_chars=max_chars, too_long=text,
        )
    log_event("plugin:voice", "generate_progress_comment_ok", text=text, direct=True)
    return text


async def _synthesize_speech_locally(text: str, voice: str) -> str:
    # Bug fix (2026-09-27), per explicit instruction: used to POST to a separate
    # local_tts_server.py subprocess over HTTP (port 9414) -- ported as-is from the old
    # Node.js backend, where that indirection was the only way to reach a Python TTS
    # library at all. backend-py IS Python, so this now calls app.local_edge_tts (itself
    # ported from reforce's own hardened EdgeTTS.py) directly, in-process: no subprocess
    # to launch/supervise, no HTTP round trip, and -- the actual fix for the real
    # incident this replaces -- a real connect/receive timeout on the underlying edge-tts
    # call, where the old subprocess had none at all and a single stuck request leaked a
    # worker thread forever (confirmed live: 0 of 14 real requests succeeded in one
    # session once that happened, every one paying Camerlengo's slow ai:tts fallback
    # instead, 20-97s even for a few words).
    from app.local_edge_tts import EdgeTtsOptions, synthesize_sentences_in_parallel

    audio = await synthesize_sentences_in_parallel(text, EdgeTtsOptions(voice=voice))
    if not audio:
        raise VoiceApiError("local edge-tts returned no audio")
    return base64.b64encode(audio).decode("ascii")


async def _synthesize_speech_via_camerlengo(text: str, voice: str, session: str | None = None) -> str:
    body: dict[str, Any] = {"command": "ai:tts", "key": CAROLINE_SW_KEY, "text": text, "voice": voice}
    if not session:
        session = await get_cached_v2_session()
    if session:
        body["session"] = session
    data = await _post_json(body)
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        if data.get(".errcode") == "402":
            raise VoiceApiError("insufficient_balance")
        raise VoiceApiError(str(data.get(".reason") or "TTS failed"))
    return data["result"]  # base64 MP3


async def synthesize_speech(
    text: str, voice: str = "Nova", session: str | None = None, language: str | None = None,
    session_provider: Callable[[], Awaitable[str | None]] | None = None,
) -> str:
    """Prefers the local edge-tts server over Camerlengo's ai:tts -- purely
    a latency optimization (no Camerlengo round trip), tried FIRST
    regardless of session, falling back to Camerlengo whenever the local
    attempt fails for any reason (never a hard new dependency)."""
    # `language` (ISO 639-1, 2026-10-10): a caller that already knows it skips the
    # ai:detectLanguage round trip -- voice narration does, since every remark is
    # already translated into the tab's own language.
    if language is None:
        language = await detect_language(text)
    edge_voice = _edge_tts_voice_for(voice, language)
    try:
        audio = await _synthesize_speech_locally(text, edge_voice)
        log_event("plugin:voice", "tts_local_ok", edge_voice=edge_voice, text_len=len(text))
        return audio
    except Exception as exc:
        log_event("plugin:voice", "tts_local_failed_falling_back", edge_voice=edge_voice, error=str(exc))
        # `session_provider` (2026-10-11): the session only matters here, on the fallback,
        # so a caller can hand over how to get one instead of minting it for every request.
        if session is None and session_provider is not None:
            session = await session_provider()
        return await _synthesize_speech_via_camerlengo(text, voice, session)


# Skip the LLM cleanup call entirely when the text plainly has nothing for
# it to fix -- no markdown/HTML syntax, no digits.
_NEEDS_TTS_CLEANUP = re.compile(r"[*_`#|~]|<[a-z][^>]*>|\d", re.IGNORECASE)


def speech_needs_cleanup(text: str) -> bool:
    """Whether clean_text_for_speech will spend a model call on this text."""
    return bool(_NEEDS_TTS_CLEANUP.search(text))


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[a-z/][^>]*>", re.IGNORECASE)
_MD_LINE_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|>\s?|[-*+•]\s+|\d+[.)]\s+)", re.MULTILINE)
_MD_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$|^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$", re.MULTILINE)
_MD_EMPHASIS = re.compile(r"(\*\*|\*|~~|`+)(?=\S)(.+?)(?<=\S)\1")
_MD_UNDERSCORE = re.compile(r"(?<!\w)(__|_)(?=\S)(.+?)(?<=\S)\1(?!\w)")  # not inside snake_case
_TABLE_BAR = re.compile(r"[ \t]*\|[ \t]*")
_DIGIT = re.compile(r"\d")
# One attempt, this long: a sentence waits for its sound meanwhile. Measured 2026-10-19 on
# real answer pieces with an idle server: 1.9-4.4s (median 2.5s); a busy one took 9-14s,
# and _post_json's defaults (30s x 3 attempts) allowed far more.
SPEECH_CLEANUP_TIMEOUT_S = 5.0


def strip_markup_for_speech(text: str) -> str:
    """Markdown/HTML down to plain prose, locally: links keep their text, list and
    header markers, emphasis marks, rules and table bars go."""
    out = _MD_LINK.sub(r"\1", text)
    out = _HTML_TAG.sub(" ", out)
    out = _MD_RULE.sub("", out)
    out = _MD_LINE_PREFIX.sub("", out)
    out = _MD_EMPHASIS.sub(r"\2", out)
    out = _MD_UNDERSCORE.sub(r"\2", out)
    lines = (_TABLE_BAR.sub(", ", line.strip().strip("|")).strip() for line in out.splitlines())
    out = "\n".join(line for line in lines if line)
    out = out.replace("`", "").replace("#", "")
    return re.sub(r"[ \t]{2,}", " ", out).strip()


async def clean_text_for_speech(text: str, session: str | None = None) -> str:
    """Strips markdown/HTML formatting and rewrites digits/numerals into
    their spoken, correctly-inflected form before text reaches TTS/Visual
    Mode.

    2026-10-21: the markup goes locally; the model is asked only when digits
    remain (inflecting "5 книг" is what it is for), for at most
    SPEECH_CLEANUP_TIMEOUT_S, one attempt. When it does not answer in time the
    text goes as stripped, digits and all -- the speech service reads digits
    itself, only without the case agreement."""
    if not speech_needs_cleanup(text):
        return text
    started = time.monotonic()
    stripped = strip_markup_for_speech(text)
    if not _DIGIT.search(stripped):
        log_event("plugin:voice", "speech_cleanup", how="local", ms=int((time.monotonic() - started) * 1000))
        return stripped
    text = stripped
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
    if not session:
        session = await get_cached_v2_session()
    if session:
        body["session"] = session
    how = "model"
    try:
        data = await asyncio.wait_for(_post_json(body, timeout=SPEECH_CLEANUP_TIMEOUT_S, retries=1), SPEECH_CLEANUP_TIMEOUT_S + 0.5)
        if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
            how, data = "model_refused", None
    except Exception as exc:  # noqa: BLE001 -- the text still goes, as stripped
        how, data = f"model_failed:{type(exc).__name__}", None
    log_event("plugin:voice", "speech_cleanup", how=how, ms=int((time.monotonic() - started) * 1000))
    return data["result"] if data else text


async def describe_image_cheap(base64_png: str) -> dict[str, Any]:
    """Cheap image understanding (Camerlengo's ai:describeImage, a small
    model) as a deliberately CHEAPER alternative to putting raw image
    bytes into Claude's own context."""
    body: dict[str, Any] = {"command": "ai:describeImage", "key": CAROLINE_SW_KEY, "content": base64_png, "type": "base64"}
    session = await get_cached_v2_session()
    if session:
        body["session"] = session
    data = await _post_json(body)
    if data.get(".status") != "ok" or not isinstance(data.get("result"), dict):
        raise VoiceApiError(str(data.get(".reason") or "describeImage failed"))
    r = data["result"]
    return {"description": str(r.get("description") or ""), "objects": r.get("objects"), "palette": r.get("palette")}
