"""Quiet, per-user adaptation of speech recognition (2026-10-10), per explicit
instruction: Caroline should get better at hearing THIS user over time, on her
own, without asking them to train anything.

Whisper itself can't be tuned per user from here: Camerlengo's ai:stt takes no
vocabulary prompt, and AI.py's speechToTextBase64 accepts a `prompt` argument but
never sends it. So the adaptation happens after recognition instead -- corrections
learned from the user are applied to every new transcript (apply_adaptation),
deterministically: no extra model call, no added latency on a voice turn.

A correction is learned from two things the user does anyway:
  1. An edited transcript (strong). With voice auto-send off the transcript lands
     in the input box; chat.js reports it (voiceTranscript) next to what was
     actually sent. Every short word-span substitution between the two is a
     correction of weight 1, applied from the very next transcript.
  2. A quick re-ask (weak). A voice message followed within RE_ASK_WINDOW_S by a
     near-identical message that differs in one short span -- seen live: "что ты
     увидишь" recognized for "что ты умеешь", then asked again. Weight
     RE_ASK_WEIGHT, so it applies only after it has happened several times: once,
     it looks exactly like two genuinely different questions ("погода в Киеве" /
     "погода во Львове").
Undo is the same mechanism in reverse: editing an applied correction back
counts against it (apply only uses the net weight of a pair over its reverse).

Stored as stt-adaptation.json in the workspace dir: text pairs only, never audio,
and nothing leaves the machine.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

# Longest word span on either side of a learned substitution -- recognition errors
# are a word or two ("увидишь" for "умеешь"); a longer rewrite is the user changing
# what they meant to say, not a mishearing.
MAX_SPAN_WORDS = 3
# Spelling similarity a substitution needs to count as a mishearing at all ("умеешь"
# / "увидишь" ~0.46). Below it, the user replaced a word with a different word.
MIN_SPAN_SIMILARITY = 0.35
RE_ASK_WINDOW_S = 45.0
# How alike two whole messages must be for the second to count as a re-ask.
RE_ASK_MIN_SIMILARITY = 0.7
RE_ASK_WEIGHT = 0.34  # three re-asks make one applied correction
APPLY_MIN_NET_WEIGHT = 1.0
MAX_PAIRS = 300

_FILE_NAME = "stt-adaptation.json"
_WORD = re.compile(r"\w+", re.UNICODE)

# tab_id -> (monotonic time, text) of the last voice message, for re-ask detection.
_last_voice_message: dict[str, tuple[float, str]] = {}


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


def _words(text: str) -> list[str]:
    return [_norm(w) for w in _WORD.findall(text)]


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / _FILE_NAME


def _load(workspace_dir: str) -> dict[str, Any]:
    path = _path(workspace_dir)
    if not path.exists():
        return {"version": 1, "pairs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("pairs"), dict):
            return data
    except Exception as exc:  # noqa: BLE001 -- a bad file must never break voice input
        log_event("engine", "stt_adaptation_load_failed", error=str(exc))
    return {"version": 1, "pairs": {}}


def _save(workspace_dir: str, data: dict[str, Any]) -> None:
    pairs = data["pairs"]
    flat = [(src, dst, info) for src, dsts in pairs.items() for dst, info in dsts.items()]
    if len(flat) > MAX_PAIRS:
        flat.sort(key=lambda item: (item[2].get("w", 0), item[2].get("last", 0)))
        for src, dst, _info in flat[: len(flat) - MAX_PAIRS]:
            pairs[src].pop(dst, None)
            if not pairs[src]:
                pairs.pop(src)
    try:
        _path(workspace_dir).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "stt_adaptation_save_failed", error=str(exc))


def _substitutions(heard: str, meant: str) -> list[tuple[str, str]]:
    """Short word-span replacements turning `heard` into `meant`, as (heard phrase,
    meant phrase), both normalized. Insertions and deletions are ignored -- adding or
    dropping words is editing, not a mishearing."""
    a, b = _words(heard), _words(meant)
    out: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag != "replace" or i2 - i1 > MAX_SPAN_WORDS or j2 - j1 > MAX_SPAN_WORDS:
            continue
        src, dst = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if src == dst or src.isdigit() or dst.isdigit():
            continue
        if difflib.SequenceMatcher(a=src, b=dst).ratio() < MIN_SPAN_SIMILARITY:
            continue
        out.append((src, dst))
    return out


def _learn(workspace_dir: str, pairs: list[tuple[str, str]], weight: float, source: str) -> None:
    if not pairs:
        return
    data = _load(workspace_dir)
    now = time.time()
    for src, dst in pairs:
        info = data["pairs"].setdefault(src, {}).setdefault(dst, {"w": 0.0, "seen": 0})
        info["w"] = round(info.get("w", 0.0) + weight, 3)
        info["seen"] = info.get("seen", 0) + 1
        info["last"] = now
        log_event("engine", "stt_adaptation_learned", source=source, heard=src, meant=dst, weight=info["w"])
    _save(workspace_dir, data)


def _active_rules(data: dict[str, Any]) -> list[tuple[list[str], str]]:
    """(heard words, meant phrase) for every pair whose weight beats its own reverse
    by APPLY_MIN_NET_WEIGHT -- longest heard phrase first, so "что ты увидишь" wins
    over a shorter rule inside it."""
    pairs = data.get("pairs", {})
    rules = []
    for src, dsts in pairs.items():
        for dst, info in dsts.items():
            reverse = pairs.get(dst, {}).get(src, {}).get("w", 0.0)
            if info.get("w", 0.0) - reverse >= APPLY_MIN_NET_WEIGHT:
                rules.append((src.split(), dst))
    rules.sort(key=lambda rule: len(rule[0]), reverse=True)
    return rules


def apply_adaptation(workspace_dir: str, text: str) -> str:
    """Rewrites learned mishearings in a fresh transcript. Matches whole words,
    keeps the punctuation around them, and keeps a capital first letter."""
    if not text:
        return text
    rules = _active_rules(_load(workspace_dir))
    if not rules:
        return text
    tokens = [(m.start(), m.end(), _norm(m.group())) for m in _WORD.finditer(text)]
    used = [False] * len(tokens)
    edits: list[tuple[int, int, str, str]] = []
    for heard, meant in rules:
        n = len(heard)
        for i in range(len(tokens) - n + 1):
            if any(used[i:i + n]) or [t[2] for t in tokens[i:i + n]] != heard:
                continue
            start, end = tokens[i][0], tokens[i + n - 1][1]
            original = text[start:end]
            replacement = meant[:1].upper() + meant[1:] if original[:1].isupper() else meant
            edits.append((start, end, replacement, original))
            for k in range(i, i + n):
                used[k] = True
    if not edits:
        return text
    for start, end, replacement, _original in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    log_event("engine", "stt_adapted", replacements=[[o, r] for _s, _e, r, o in sorted(edits)])
    return text


def observe_user_message(workspace_dir: str, tab_id: str, text: str, is_voice: bool, voice_transcript: str | None) -> None:
    """Called for every real user message before it is submitted. Never raises."""
    try:
        text = (text or "").strip()
        now = time.monotonic()
        if voice_transcript and voice_transcript.strip() and voice_transcript.strip() != text:
            _learn(workspace_dir, _substitutions(voice_transcript, text), 1.0, "edit")
        previous = _last_voice_message.pop(tab_id, None)
        if previous is not None and text and now - previous[0] <= RE_ASK_WINDOW_S and previous[1] != text:
            similarity = difflib.SequenceMatcher(a=" ".join(_words(previous[1])), b=" ".join(_words(text))).ratio()
            subs = _substitutions(previous[1], text)
            if similarity >= RE_ASK_MIN_SIMILARITY and len(subs) == 1:
                _learn(workspace_dir, subs, RE_ASK_WEIGHT, "re-ask")
        if is_voice and text:
            _last_voice_message[tab_id] = (now, text)
    except Exception as exc:  # noqa: BLE001 -- learning must never cost the user their message
        log_event("engine", "stt_adaptation_observe_failed", error=str(exc))
