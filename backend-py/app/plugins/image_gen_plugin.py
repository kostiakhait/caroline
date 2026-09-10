"""image_gen -- text-to-image generation, backed by the beautysqrl.com AI
API (see d:/REPO/gamepool/AI.md for the full protocol reference -- the
same service that project's own game-asset generation scripts use).

Per explicit instruction (2026-09-10): confirmed live that Caroline had NO
image-generation capability at all (she said so herself, mid-task, when
asked to render presentation graphics) -- this had been requested before
but never actually built during the backend-py port. Added directly.

Deliberately a separate, standalone client rather than routed through
Camerlengo/SquirrelWisdom -- beautysqrl.com is a wholly different service
with its own static API key and v1-style `.command` envelope (no
session/login at all, unlike sw_api.py's v2 protocol).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool

API_URL = "https://beautysqrl.com"
API_KEY = "01Az8nB8mB4cCV"


async def generate_image(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    body: dict[str, Any] = {".command": "generateImage", "key": API_KEY, "description": args["description"]}
    if args.get("size"):
        body["size"] = args["size"]
    if args.get("model"):
        body["model"] = args["model"]
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(API_URL, json=body)
        res.raise_for_status()
        data = res.json()
    if data.get(".status") != "ok" or not isinstance(data.get("result"), str):
        log_event("plugin:image-gen", "generate_image_failed", reason=data.get(".reason"))
        raise RuntimeError(str(data.get(".reason") or "generateImage failed"))
    image_b64 = data["result"]
    save_path = args.get("savePath")
    if save_path:
        Path(save_path).write_bytes(base64.b64decode(image_b64))
    log_event("plugin:image-gen", "generate_image_ok", description=args["description"][:200], saved=bool(save_path))
    return {
        "text": "Generated image" + (f" and saved to {save_path}." if save_path else " (not saved to disk -- pass savePath to keep a copy)."),
        "image_base64": image_b64,
        "mime_type": data.get("mime") or "image/png",
    }


PLUGIN = Plugin(
    name="image-gen",
    usage_instructions=(
        "Generates a real illustrated/photorealistic image from a text description (DALL-E 3) -- use this "
        "whenever a task genuinely needs an actual picture (a presentation slide's hero image, an icon, concept "
        "art, an asset), not a technical diagram/schematic you can already draw with existing tools (PowerPoint "
        "shapes, a chart, etc. -- use those directly for those). Always pass `savePath` to keep the result on "
        "disk (e.g. to embed into a document/presentation afterward) -- the image is ALSO returned inline in the "
        "tool result so you can look at it yourself and judge whether it matches before using it; without "
        "savePath nothing is kept. There is no native transparent-background support -- if you need one, ask "
        "for a solid magenta (#FF00FF) background in the description and chroma-key/crop it out afterward."
    ),
    tools=[
        PluginTool(
            "generate_image",
            "Generates a PNG image from a text description (DALL-E 3-backed). Pass `savePath` to save the "
            "result to a local file; the image is also returned inline either way.",
            {
                "description": str,
                "size": str | None,
                "model": str | None,
                "savePath": str | None,
            },
            generate_image,
        ),
    ],
)
