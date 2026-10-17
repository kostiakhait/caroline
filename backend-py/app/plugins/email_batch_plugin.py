"""email batch -- runs several mailbox operations concurrently in one tool
call, instead of the model looping email_plugin.py's single-item tools one
at a time. Confirmed live (2026-10-04) as a real cost/latency problem: an
8-mailbox Inbox+Sent sweep took 16+ sequential tool calls and several
minutes. These tools fan the same work out under a bounded semaphore
(BATCH_CONCURRENCY) instead.

Every batch tool reuses email_plugin.py's own single-item functions as
workers (no IMAP/Camerlengo logic duplicated here) -- each item gets its own
try/once-retry; a failing item is reported as an error entry, it never
aborts the rest of the batch. Same philosophy as small_model_engine.py's
parallel-plan-stage branches (bounded concurrency, one retry per unit,
partial results are still a real result).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from app.plugins.email_plugin import (
    _ACCOUNT_PARAM_NOTE,
    _CREDENTIAL_PARAMS,
    EMAIL_BODY_MAX_CHARS,
    email_delete,
    email_get_message,
    email_list_messages,
    email_mark,
    email_move,
    email_send,
)
from app.plugins.loader import Plugin, PluginTool
from app.policies import follow_explicit_parameters_instruction

BATCH_CONCURRENCY = 4
BATCH_RETRY_ATTEMPTS = 2
BATCH_MAX_ITEMS = 50


async def _run_batch(items: list[dict[str, Any]], label_fn: Callable[[dict[str, Any]], str], worker: Callable[[dict[str, Any]], Awaitable[Any]]) -> list[dict[str, Any]]:
    if len(items) > BATCH_MAX_ITEMS:
        raise ValueError(f"at most {BATCH_MAX_ITEMS} items per batch call, got {len(items)}")
    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def run_one(index: int, item: dict[str, Any]) -> dict[str, Any]:
        label = label_fn(item)
        last_error = ""
        async with semaphore:
            for attempt in range(1, BATCH_RETRY_ATTEMPTS + 1):
                try:
                    result = await worker(item)
                    return {"index": index, "label": label, "ok": True, "result": result}
                except Exception as exc:  # noqa: BLE001 -- one item's failure must not break the batch
                    last_error = f"{type(exc).__name__}: {exc}"
        return {"index": index, "label": label, "ok": False, "error": last_error}

    return await asyncio.gather(*(run_one(i, item) for i, item in enumerate(items)))


def _summarize(outcomes: list[dict[str, Any]]) -> str:
    ok = sum(1 for o in outcomes if o["ok"])
    failed = [o for o in outcomes if not o["ok"]]
    lines = [f"{ok}/{len(outcomes)} succeeded."]
    for o in failed:
        lines.append(f"FAILED {o['label']}: {o['error']}")
    return "\n".join(lines)


def _account_label(account: dict[str, Any]) -> str:
    return str(account.get("label") or account.get("address") or "?")


async def email_list_messages_batch(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    accounts = args.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return {"text": "accounts must be a non-empty list.", "is_error": True}
    folders = args.get("folders") or ["INBOX"]
    shared = {k: args[k] for k in ("unseenOnly", "limit", "query", "sinceIso") if args.get(k) is not None}

    work_items = [{"account": acc, "folder": folder} for acc in accounts for folder in folders]

    async def worker(item: dict[str, Any]) -> list[Any]:
        call_args = {**item["account"], "folder": item["folder"], **shared}
        envelope = await email_list_messages(call_args, None)
        return json.loads(envelope["text"])

    outcomes = await _run_batch(work_items, lambda item: f'{_account_label(item["account"])}:{item["folder"]}', worker)

    report: dict[str, dict[str, Any]] = {}
    total_messages = 0
    for outcome in outcomes:
        account_label, folder = outcome["label"].rsplit(":", 1)
        bucket = report.setdefault(account_label, {})
        if outcome["ok"]:
            bucket[folder] = {"count": len(outcome["result"]), "messages": outcome["result"]}
            total_messages += len(outcome["result"])
        else:
            bucket[folder] = {"error": outcome["error"]}

    summary = _summarize(outcomes)
    return {"text": f"{summary}\n{total_messages} message(s) total.\n\n" + json.dumps(report, indent=2, ensure_ascii=False)}


def _message_label(item: dict[str, Any]) -> str:
    return f'{item.get("address", "?")} {item.get("folder", "?")}:{item.get("uid", "?")}'


# Per explicit instruction (2026-10-12): reading a folder/date-range used to mean
# listMessages (headers only) followed by one email_get_message call PER message --
# slow, and easy to silently skip one. email_read_range does both steps itself: list
# the range, then fetch every message's FULL content (to/from/cc/subject/body)
# concurrently, same _run_batch machinery as the batch tools above. Attachments are
# listed (filename/contentType), never downloaded automatically -- email_download_
# attachment stays the on-request path for actual bytes, so a range read never pulls
# megabytes of attachment content nobody asked for.
BULK_READ_MAX_MESSAGES = 200

# Bug fix (2026-10-16), confirmed live: email_read_range's only size guard was a message-COUNT
# cap (BULK_READ_MAX_MESSAGES) -- nothing bounded the total TEXT returned in one response. Even
# with each individual body now truncated (email_get_message's own EMAIL_BODY_MAX_CHARS, 2026-
# 10-16 fix), a range with many not-tiny messages could still return hundreds of KB in one tool
# result, which is exactly what overflowed a conversation's context past what auto-compaction
# could recover from. Same number as files_plugin.py's own MAX_READ_BYTES -- one already-vetted
# "how much text is reasonable to hand the model in one call" budget, reused rather than picked
# fresh here.
EMAIL_RANGE_TOTAL_BODY_CHARS = 200_000


async def email_read_range(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    folder = args.get("folder") or "INBOX"
    creds = {k: args[k] for k in _CREDENTIAL_PARAMS if k in args}
    limit = min(int(args.get("limit") or BULK_READ_MAX_MESSAGES), BULK_READ_MAX_MESSAGES)

    list_args: dict[str, Any] = {**creds, "folder": folder, "limit": limit}
    if args.get("sinceIso"):
        list_args["sinceIso"] = args["sinceIso"]
    if args.get("untilIso"):
        list_args["untilIso"] = args["untilIso"]
    if args.get("unseenOnly") is not None:
        list_args["unseenOnly"] = args["unseenOnly"]
    if args.get("query"):
        list_args["query"] = args["query"]

    headers = json.loads((await email_list_messages(list_args, None))["text"])
    if not headers:
        return {"text": f'No messages found in "{folder}" for that date range/filter.'}

    items = [{**creds, "folder": folder, "uid": int(h["uid"])} for h in headers]
    # _run_batch's own BATCH_MAX_ITEMS (50) is tuned for the lighter write-batch tools below,
    # where a chunk's worst case is trivially small -- too big a chunk here (bug fix, 2026-10-16,
    # confirmed live: a single 50-message chunk, each body already capped at EMAIL_BODY_MAX_CHARS
    # by email_get_message, can still total up to 50*20_000 = 1,000,000 characters, five times
    # EMAIL_RANGE_TOTAL_BODY_CHARS, before the between-chunk check below ever gets a chance to
    # fire). Chunk size here is instead budget-aware: never more than the whole total budget
    # could plausibly need in one chunk, so the between-chunk check actually gets to fire well
    # before the budget is blown, not after.
    chunk_size = max(1, min(BATCH_MAX_ITEMS, EMAIL_RANGE_TOTAL_BODY_CHARS // EMAIL_BODY_MAX_CHARS))
    outcomes: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    body_chars_so_far = 0
    for start in range(0, len(items), chunk_size):
        if body_chars_so_far > EMAIL_RANGE_TOTAL_BODY_CHARS:
            skipped_items.extend(items[start:])
            break
        chunk = items[start:start + chunk_size]
        chunk_outcomes = await _run_batch(chunk, _message_label, lambda item: email_get_message(item, None))
        outcomes.extend(chunk_outcomes)
        for outcome in chunk_outcomes:
            if outcome["ok"]:
                body = json.loads(outcome["result"]["text"])
                body_chars_so_far += len(body.get("text") or "") + len(body.get("html") or "")

    messages: list[dict[str, Any]] = []
    errors: list[str] = []
    for outcome in outcomes:
        if outcome["ok"]:
            messages.append(json.loads(outcome["result"]["text"]))
        else:
            errors.append(f'{outcome["label"]}: {outcome["error"]}')
    messages.sort(key=lambda m: int(m["uid"]), reverse=True)

    lines = [f'{len(messages)}/{len(headers)} message(s) fetched in full from "{folder}".']
    if len(headers) >= limit:
        lines.append(
            f"NOTE: the folder/range may hold MORE than the {limit} messages returned here (the list was "
            "capped at `limit`) -- narrow sinceIso/untilIso or raise `limit` to be sure nothing was skipped."
        )
    if skipped_items:
        skipped_uids = ", ".join(str(item["uid"]) for item in skipped_items)
        lines.append(
            f"STOPPED EARLY: total body text passed {EMAIL_RANGE_TOTAL_BODY_CHARS} characters, so "
            f"{len(skipped_items)} more message(s) in this range were NOT fetched: uid {skipped_uids}. "
            "Fetch these individually with email_get_message (one at a time, or in a few small groups) if "
            "you actually need them -- do not assume they're unimportant just because this call stopped."
        )
    if errors:
        lines.append(f"{len(errors)} message(s) failed to fetch in full (shown as missing below):")
        lines.extend(errors)
    return {"text": "\n".join(lines) + "\n\n" + json.dumps(messages, indent=2, ensure_ascii=False)}


async def email_mark_batch(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return {"text": "items must be a non-empty list.", "is_error": True}
    outcomes = await _run_batch(items, _message_label, lambda item: email_mark(item, None))
    return {"text": _summarize(outcomes)}


async def email_delete_batch(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return {"text": "items must be a non-empty list.", "is_error": True}
    outcomes = await _run_batch(items, _message_label, lambda item: email_delete(item, None))
    return {"text": _summarize(outcomes)}


async def email_move_batch(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return {"text": "items must be a non-empty list.", "is_error": True}
    outcomes = await _run_batch(items, _message_label, lambda item: email_move(item, None))
    return {"text": _summarize(outcomes)}


def _send_label(item: dict[str, Any]) -> str:
    to = ", ".join(item.get("to") or []) if isinstance(item.get("to"), list) else str(item.get("to"))
    return f'{to} / {item.get("subject", "?")}'


async def email_send_batch(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    messages = args.get("messages")
    if not isinstance(messages, list) or not messages:
        return {"text": "messages must be a non-empty list.", "is_error": True}
    outcomes = await _run_batch(messages, _send_label, lambda item: email_send(item, None))
    return {"text": _summarize(outcomes)}


def _usage_instructions() -> str:
    return "\n\n".join((
        "Whenever an email task touches 2 or more mailboxes, or 2 or more messages for the same action "
        "(marking, moving, deleting, sending), use the batch tool below instead of calling the single-item "
        "email_* tool in a loop -- looping burns far more time and tokens for the exact same result. These "
        "run the work concurrently (up to 4 at once) and retry a failing item once before reporting it as "
        "failed -- one bad mailbox or message never stops the rest of the batch.\n"
        'Example -- "check all 8 mailboxes for new mail": one email_list_messages_batch call with all 8 '
        'accounts and folders: ["INBOX", "Sent"], not 16 separate email_list_messages calls.\n'
        "Each account entry takes the same credential fields as the single-item tools (look them up the same "
        "way, see email_plugin's own credentials instructions) plus an optional `label` to make the report "
        "easier to read; without it the address is used.\n"
        "Need the actual CONTENT (to/from/cc/subject/body), not just headers, for every message in one "
        "folder over a date range -- \"read everything in Inbox since Monday\", a digest, a sweep for "
        "something specific? Use email_read_range instead of email_list_messages followed by one "
        "email_get_message per uid: it does both steps itself, concurrently. Each body is truncated the "
        "same way email_get_message truncates one (long messages come back cut with a note), AND the whole "
        "call stops early once the combined total gets large, reporting which uids were skipped -- fetch "
        "those specific ones individually with email_get_message (one at a time, or a few at once) rather "
        "than assuming a shorter-than-expected result means the range was actually empty. Attachments come "
        "back as a list (filename/type) only, never their bytes -- call email_download_attachment yourself "
        "for a specific one only if its content is actually needed, not for every attachment by default.",
        follow_explicit_parameters_instruction(),
    ))


_ACCOUNT_LIST_PARAM = {**_CREDENTIAL_PARAMS, "label": str | None}


PLUGIN = Plugin(
    name="email-batch",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "email_list_messages_batch",
            "Lists messages across several mailboxes and/or folders at once, concurrently. folders defaults "
            "to [\"INBOX\"] -- pass [\"INBOX\", \"Sent\"] to sweep both per account in one call. unseenOnly/"
            "limit/query/sinceIso apply the same to every account/folder combination." + _ACCOUNT_PARAM_NOTE,
            {
                "accounts": list, "folders": list | None,
                "unseenOnly": bool | None, "limit": int | None, "query": str | None, "sinceIso": str | None,
            },
            email_list_messages_batch,
        ),
        PluginTool(
            "email_read_range",
            "Reads every message's content (to, from, cc, subject, body, attachment list) in one folder "
            "over a date range, in one call -- no need to list headers then fetch each uid separately. Use "
            "sinceIso/untilIso (ISO dates, e.g. \"2026-10-01\") for a [since, until) interval, unseenOnly/"
            "query to narrow further, and limit to raise the cap above the default "
            f"({BULK_READ_MAX_MESSAGES}) if the reply says the range may hold more. Each body is truncated "
            f"past {EMAIL_BODY_MAX_CHARS} characters (same as email_get_message), and the whole call stops "
            f"early past {EMAIL_RANGE_TOTAL_BODY_CHARS} combined characters, naming the uids it skipped -- "
            "fetch those with email_get_message individually if you actually need them. Attachments come "
            "back as {index, filename, contentType} only -- never their bytes; call email_download_attachment "
            "yourself for one specific attachment only when its actual content is needed." + _ACCOUNT_PARAM_NOTE,
            {
                **_CREDENTIAL_PARAMS, "folder": str | None, "sinceIso": str | None, "untilIso": str | None,
                "unseenOnly": bool | None, "query": str | None, "limit": int | None,
            },
            email_read_range,
        ),
        PluginTool(
            "email_mark_batch",
            "Adds or removes an IMAP flag on several messages at once, concurrently -- each item is a full "
            "{address, password, imapHost, ..., folder, uid, flag, set} object, same fields as email_mark, "
            "so items can span different mailboxes.",
            {"items": list}, email_mark_batch,
        ),
        PluginTool(
            "email_delete_batch",
            "Deletes several messages at once, concurrently -- each item is a full {address, password, "
            "imapHost, ..., folder, uid} object, same fields as email_delete, so items can span different "
            "mailboxes.",
            {"items": list}, email_delete_batch,
        ),
        PluginTool(
            "email_move_batch",
            "Moves several messages at once, concurrently -- each item is a full {address, password, "
            "imapHost, ..., folder, uid, destFolder} object, same fields as email_move, so items can span "
            "different mailboxes.",
            {"items": list}, email_move_batch,
        ),
        PluginTool(
            "email_send_batch",
            "Sends several emails at once, concurrently -- each item is a full message object with the same "
            "fields as email_send (to, subject, text/html, address/password/... to send as a specific "
            "mailbox, attachments, etc.).",
            {"messages": list}, email_send_batch,
        ),
    ],
)
