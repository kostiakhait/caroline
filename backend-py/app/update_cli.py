"""`python -m app.update_cli apply [--root DIST]`: the update for a platform where Caroline is not running yet and nobody is using her, so the rule says
install, by itself (docs/UPDATE_EVERYWHERE_PLAN.md): the Xopek desktops call this when a desktop starts, before the shell does.

Looks for a signed release, downloads and verifies this platform's artifact (app/update_service.py), applies it as an overlay over the dist (app/overlay_install.py).
Never fails the start: whatever happens (offline, no release, a bad file) the exit code is 0 and the installed Caroline is as it was; the log line says what happened.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import overlay_install, update_service
from app.update_service import UpdateService


def apply(root: Path) -> str:
    update_service.HTTP_TIMEOUT_S = 15  # a start must not wait long on a server that is down
    svc = UpdateService()
    state = svc.run_once()
    if state["status"] != "ready":
        return f"no update ({state['status']}{': ' + state['error'] if state.get('error') else ''}); Caroline {svc.version} stays"
    try:
        replaced = overlay_install.apply_overlay(state["path"], root)
    except overlay_install.OverlayError as exc:
        return f"update {state['version']} refused: {exc}; Caroline {svc.version} stays"
    svc.mark_installed()
    return f"updated {svc.version} -> {state['version']} ({len(replaced)} paths replaced)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["apply"])
    ap.add_argument("--root", default=str(update_service.APP_ROOT.parent), help="the dist: the folder with backend-py/ and wwwroot/")
    args = ap.parse_args(argv)
    try:
        print(f"[update] {apply(Path(args.root))}", flush=True)
    except Exception as exc:  # noqa: BLE001 -- never fail the start of a desktop
        print(f"[update] failed, Caroline stays as she is: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
