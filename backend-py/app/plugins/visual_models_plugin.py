"""visual-models -- lets Caroline buy an additional Visual Mode talking-head
model (beyond the built-in Caroline/Peter pair) using the SAME piaster
wallet/asset-ownership rails Camerlengo already has for any other payable
asset -- see reforce/API/Api2WalletCommands.py (wallet:charge, asset:
getInfo, asset:recordPayment) and reforce/AssetRegistry.py -- then
downloads and activates it as an independent Visual Mode skin.

CATALOG_MODEL_NAMES is a temporary, hand-maintained list (per the source
directory shown live, 2026-09-23) -- not yet backed by a real manifest
file. Pricing is NOT decided yet either (per explicit instruction) --
get_visual_model_price/list_visual_models report exactly what AssetRegistry
actually has for each one right now (0/unregistered until someone runs the
registration step), never a guessed or invented number.

Stage 2 (2026-09-23), per explicit instruction: the .xcfa files themselves
are ALREADY served (confirmed live) at FACE_ANIMATION_BASE_URL below --
this is reforce's own shared face-animation CDN path
(FaceAnimation/ModelResolver.py's CFA_DOWNLOAD_BASE), NOT Caroline's own
"/apps/caroline/models/" tree deploy.mk's deploy-models target uploads to;
the two are unrelated subtrees on the same downloader.multi-portal.org
host. Only the per-file .sha256 sidecar needed computing + deploying (see
this repo's own scratch scripts from that session, not checked in here) --
the download/activation code below assumes it now exists next to each
.xcfa the same way CarolineA.xcfa's already does.

A purchased model has no day-parity A/B pair and no bio/gender/name
identity the way Caroline/Peter do (confirmed against the real files) --
activating one is a pure visual-skin override (visual_mode.py's
get/set_active_visual_model_override), independent of persona.py's
ProfileKey/character system, which is left completely untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.plugins.sw_api import CAROLINE_SW_KEY, SessionManager, SwApiError, call_v2
from app.session_context import get_send
from app.visual_mode import get_active_visual_model_override, models_dir, set_active_visual_model_override
from app.workspace_dir import WORKSPACE_DIR

_sessions = SessionManager()

# Per this module's own docstring -- hand-maintained until a real catalog
# manifest exists. Matches the .cfa/.xcfa pairs visible in
# Z:\ImmersiveLionSchool\Contents\face_animation\models as of 2026-09-23.
CATALOG_MODEL_NAMES = [
    "Alice", "Amalia", "Aranu", "Constantine", "Cripper", "Diego", "Elisa",
    "Esquirelle", "Evan", "Iara", "Ivanhoe", "Jack", "Joseph", "Margarette",
    "Megan", "Pedro", "Richard", "Roger", "Stephan",
]

# Confirmed live (2026-09-23): reforce/FaceAnimation/ModelResolver.py's own
# CFA_DOWNLOAD_BASE, already serving these exact .xcfa files today -- see
# this module's own docstring for why this is NOT the same path Caroline's
# own deploy.mk uses for CarolineA/B, PeterA/B.
FACE_ANIMATION_BASE_URL = "https://downloader.multi-portal.org/face_animation/models/"


def _asset_id_for(model_name: str) -> str:
    return f"caroline:visual-model:{model_name}"


def _model_file_path(model_name: str) -> Path:
    return Path(models_dir()) / f"{model_name}.xcfa"


def _pending_downloads_path() -> Path:
    return Path(WORKSPACE_DIR) / "visual-model-pending-downloads.json"


def load_pending_visual_model_downloads() -> list[str]:
    """Public (also read by main.py to flush against a fresh real WS
    connection -- see add_pending_download's own doc comment)."""
    try:
        return json.loads(_pending_downloads_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_pending_downloads(names: list[str]) -> None:
    _pending_downloads_path().write_text(json.dumps(names, indent=2) + "\n", encoding="utf-8")


def add_pending_download(model_name: str) -> None:
    """Called right after a successful purchase (below), and by
    download_visual_model for an explicit manual (re)trigger. Persisted, not
    just in-memory -- the desktop app may not have a real WS connection open
    at all right now (e.g. the purchase happened from the headless Ratatosk
    owner-channel session, see app/ratatosk_channel.py) or may close before
    a multi-GB download finishes; main.py flushes this list against the
    PRIMARY tab's real connection the same way it already does for
    visual_mode_config (see main.py's own _has_sent_visual_mode_config)."""
    pending = load_pending_visual_model_downloads()
    if model_name not in pending:
        pending.append(model_name)
        _save_pending_downloads(pending)


def remove_pending_visual_model_download(model_name: str) -> None:
    """Public -- also called by main.py's flush (already-downloaded
    cleanup) and its visual_model_download_done control-op handler."""
    pending = load_pending_visual_model_downloads()
    if model_name in pending:
        pending.remove(model_name)
        _save_pending_downloads(pending)


async def _try_trigger_download_now(model_name: str) -> bool:
    """Best-effort immediate trigger over THIS turn's own WS connection, if
    it's a real one -- returns whether it was actually sent. Never raises:
    a headless/non-WS session (get_send() still returns SOMETHING there,
    just not a real websocket -- see session_context.py's own doc comment)
    must not fail the purchase/retry flow just because there was nowhere
    to deliver this to; add_pending_download's own flush-on-connect path is
    what actually guarantees delivery eventually."""
    try:
        send = get_send()
        await send({
            "type": "visual_model_download_start",
            "modelName": model_name,
            "url": f"{FACE_ANIMATION_BASE_URL}{model_name}.xcfa",
            "shaUrl": f"{FACE_ANIMATION_BASE_URL}{model_name}.xcfa.sha256",
        })
        log_event("engine", "visual_model_download_triggered", model_name=model_name)
        return True
    except Exception as exc:
        log_event("engine", "visual_model_download_trigger_failed", model_name=model_name, error=str(exc))
        return False


async def _get_asset_info(session: str, model_name: str) -> dict[str, Any]:
    result = await call_v2("asset:getInfo", key=CAROLINE_SW_KEY, session=session, assetId=_asset_id_for(model_name))
    return result["result"] if "result" in result else result


async def list_visual_models(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    session = await _sessions.ensure_session()
    active_override = get_active_visual_model_override(WORKSPACE_DIR)
    rows = []
    for name in sorted(CATALOG_MODEL_NAMES):
        try:
            info = await _get_asset_info(session, name)
            cost_pia = info.get("costPia", 0)
            # asset:getInfo reports alreadyPaid=True for an asset that was
            # never registered at all (its "nothing to charge" case) -- only
            # meaningful once the asset is actually priced.
            already_owned = cost_pia > 0 and bool(info.get("alreadyPaid"))
        except Exception as exc:
            rows.append({"name": name, "error": str(exc)})
            continue
        rows.append({
            "name": name,
            "costPia": cost_pia,
            "priced": cost_pia > 0,
            "alreadyOwned": already_owned,
            "downloaded": _model_file_path(name).exists(),
            "active": name == active_override,
        })
    return {"text": json.dumps(rows, indent=2, ensure_ascii=False)}


async def get_visual_model_price(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    name = args["model_name"]
    if name not in CATALOG_MODEL_NAMES:
        return {"text": f'"{name}" is not in the current catalog. See list_visual_models for the real list.'}
    session = await _sessions.ensure_session()
    info = await _get_asset_info(session, name)
    cost_pia = info.get("costPia", 0)
    # Order matters: asset:getInfo reports alreadyPaid=True for an asset
    # that was never registered in AssetRegistry at all (its "nothing to
    # charge for" case, not a real ownership fact) -- checking cost_pia
    # first tells the two cases apart correctly.
    if cost_pia <= 0:
        return {"text": f'"{name}": not priced yet (no cost set in AssetRegistry) -- cannot be purchased right now.'}
    if info.get("alreadyPaid"):
        return {"text": f'"{name}": already owned -- no charge needed.'}
    return {"text": f'"{name}": {cost_pia} PIA, not yet owned.'}


async def purchase_visual_model(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    name = args["model_name"]
    if name not in CATALOG_MODEL_NAMES:
        return {"text": f'"{name}" is not in the current catalog. See list_visual_models for the real list.'}

    session = await _sessions.ensure_session()
    asset_id = _asset_id_for(name)
    info = await _get_asset_info(session, name)
    cost_pia = info.get("costPia", 0)

    # Order matters: asset:getInfo reports alreadyPaid=True for an asset
    # that was never registered in AssetRegistry at all (its "nothing to
    # charge for" case, not a real ownership fact) -- checking cost_pia
    # first tells the two cases apart correctly.
    if cost_pia <= 0:
        return {"text": f'"{name}" is not priced yet (no cost set in AssetRegistry) -- cannot be purchased right now.'}

    already_owned = bool(info.get("alreadyPaid"))
    if not already_owned:
        try:
            # wallet:charge's own error path (insufficient funds, etc.) comes
            # back as {".status": "error", ...} with no ".reason" key --
            # call_v2 then raises SwApiError with a raw json.dumps of the
            # envelope (still contains the real "message"/"code" fields,
            # just unparsed). A ".status": "ok" response here always has
            # "result": True -- call_v2 already turns a False result into
            # that raised error, so there's nothing left to double-check
            # once this returns normally.
            charge = await call_v2(
                "wallet:charge", key=CAROLINE_SW_KEY, session=session,
                currency="PIA", amount=cost_pia,
                item_type="caroline-visual-model", item_id=name,
                description=f"Caroline Visual Mode model: {name}",
                # Deterministic, not random -- a retried call with the SAME
                # model for the SAME user must never double-charge, per
                # wallet:charge's own idempotency_key contract.
                idempotency_key=f"caroline-visual-model-purchase:{name}",
            )
        except SwApiError as exc:
            return {"text": f'Charge failed for "{name}": {exc}'}

        await call_v2("asset:recordPayment", key=CAROLINE_SW_KEY, session=session, assetId=asset_id, amountPia=cost_pia)
        balance_note = f" (new balance: {charge.get('balance_major')} PIA)"
    else:
        balance_note = " -- already owned, nothing charged"

    # Per explicit instruction (2026-09-23): download starts right away, in
    # the background, immediately after a successful purchase -- not a
    # separate step the user has to ask for. Already-downloaded is a no-op
    # here (add_pending_download / the native downloader are both
    # idempotent), so a purchase_visual_model call for an already-owned,
    # already-downloaded model is harmless.
    if _model_file_path(name).exists():
        download_note = "already downloaded and ready to activate with activate_visual_model."
    else:
        add_pending_download(name)
        sent_now = await _try_trigger_download_now(name)
        download_note = (
            "download started in the background -- it's tens of GB, this will take a while; "
            "I'll let you know when it's ready." if sent_now else
            "download queued -- it'll start automatically next time the desktop app is connected "
            "(this turn has no live desktop connection to start it from right now, e.g. Ratatosk)."
        )

    return {"text": f'"{name}"{balance_note}. {download_note}'}


async def download_visual_model(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    name = args["model_name"]
    if name not in CATALOG_MODEL_NAMES:
        return {"text": f'"{name}" is not in the current catalog.'}
    session = await _sessions.ensure_session()
    info = await _get_asset_info(session, name)
    if not (info.get("costPia", 0) > 0 and info.get("alreadyPaid")):
        return {"text": f'"{name}" isn\'t owned yet -- use purchase_visual_model first.'}
    if _model_file_path(name).exists():
        return {"text": f'"{name}" is already downloaded.'}
    add_pending_download(name)
    sent_now = await _try_trigger_download_now(name)
    return {"text": "Download (re)started in the background." if sent_now else "Download queued -- will start once the desktop app is connected."}


async def activate_visual_model(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    name = args["model_name"]
    if name not in CATALOG_MODEL_NAMES:
        return {"text": f'"{name}" is not in the current catalog.'}
    if not _model_file_path(name).exists():
        return {"text": f'"{name}" hasn\'t finished downloading yet -- check list_visual_models, or call download_visual_model.'}
    set_active_visual_model_override(WORKSPACE_DIR, name)
    return {"text": f'Visual Mode now uses "{name}". Your Caroline/Peter character identity is unchanged -- this only swaps the rendered face.'}


async def deactivate_visual_model(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    current = get_active_visual_model_override(WORKSPACE_DIR)
    if current is None:
        return {"text": "No purchased model is active right now -- nothing to do."}
    set_active_visual_model_override(WORKSPACE_DIR, None)
    return {"text": f'Deactivated "{current}" -- Visual Mode is back to the normal Caroline/Peter model.'}


def _usage_instructions() -> str:
    return (
        "purchase_visual_model spends the user's REAL SquirrelWisdom piaster balance -- never call it without "
        "the user's own explicit, specific go-ahead for that exact model, every single time (same rule as any "
        "other real-money/real-value action -- a general \"sure, get more models sometime\" is not specific "
        "enough). Always check the price with get_visual_model_price or list_visual_models first and tell the "
        "user the real cost before purchasing, not after. A purchase downloads the model automatically in the "
        "background (tens of GB, takes a while) -- you'll get a proactive note when it's ready; don't tell the "
        "user it's usable in Visual Mode until then. activate_visual_model switches Visual Mode to a model the "
        "user already owns and has finished downloading -- it only changes which face renders, not the "
        "Caroline/Peter character identity/personality itself. deactivate_visual_model reverts to the normal "
        "Caroline/Peter model."
    )


PLUGIN = Plugin(
    name="visual-models",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "list_visual_models",
            "Lists every Visual Mode talking-head model in the current purchasable catalog, with its price in "
            "PIA (SquirrelWisdom's piaster currency), whether the user already owns it, whether it's finished "
            "downloading, and whether it's the currently active one. A model showing priced:false has no price "
            "set yet and cannot be purchased.",
            {}, list_visual_models,
        ),
        PluginTool(
            "get_visual_model_price",
            "Checks one specific model's price and ownership status by name (see list_visual_models for valid "
            "names).",
            {"model_name": str}, get_visual_model_price,
        ),
        PluginTool(
            "purchase_visual_model",
            "Spends the user's real piaster balance to buy ownership of one Visual Mode model by name. Charges "
            "exactly the price get_visual_model_price/list_visual_models reports, once per model per user "
            "(idempotent -- a repeat call after already owning it charges nothing). Automatically starts "
            "downloading it in the background afterward -- see this plugin's own usage_instructions.",
            {"model_name": str}, purchase_visual_model,
        ),
        PluginTool(
            "download_visual_model",
            "Manually (re)starts the background download for a model the user already owns, e.g. if the "
            "automatic download after purchase never started (no live desktop connection at the time) or "
            "stalled. A no-op if it's already downloaded.",
            {"model_name": str}, download_visual_model,
        ),
        PluginTool(
            "activate_visual_model",
            "Switches Visual Mode to render a purchased, already-downloaded model instead of the normal "
            "Caroline/Peter one. Only changes which face renders -- the character identity/personality/name "
            "stays whatever it already is.",
            {"model_name": str}, activate_visual_model,
        ),
        PluginTool(
            "deactivate_visual_model",
            "Reverts Visual Mode from a purchased model back to the normal Caroline/Peter rendering.",
            {}, deactivate_visual_model,
        ),
    ],
)
