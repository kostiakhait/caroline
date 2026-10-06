"""Production entry point -- invoked directly by BackendProcess.cs (pythonw.exe
run_server.py) instead of `python -m app.main`, because the embeddable Python
distribution's own python3XX._pth file fully overrides sys.path (per its own
documented contract: when a ._pth file exists, ALL registry/env vars are
ignored and sys.path is built solely from that file's own entries) -- this
means `-m`'s usual cwd-prepend behavior can't be relied on to make the `app`
package importable. A plain script invocation doesn't have that problem: the
script's own directory is always sys.path[0], confirmed empirically against
the actual embeddable runtime this ships with (see PythonInstaller.cs).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import PORT, REMOTE_PORT, app  # noqa: E402
from app.logging_setup import log_event  # noqa: E402
from app.skills_seed import seed_skills  # noqa: E402
from app.workspace_dir import WORKSPACE_DIR  # noqa: E402

if __name__ == "__main__":
    import asyncio

    import uvicorn

    log_event("engine", "starting", port=PORT, remote_port=REMOTE_PORT, workspace_dir=WORKSPACE_DIR)
    seed_skills(WORKSPACE_DIR)
    # Bug fix (2026-09-27), per explicit instruction, found live (backend
    # crash-looping on startup, "ModuleNotFoundError: No module named
    # 'app.local_tts_launcher'"): main.py's OWN __main__ block (dead code
    # when run_server.py is the real entry point, which it always is --
    # see this file's own docstring) had already been fixed the same day to
    # drop the local_tts_launcher/launch_local_tts_server() call (see its
    # comment: "no longer launches local_tts_server.py as a separate
    # subprocess ... in-process app.local_edge_tts now, this file/launcher
    # was leftover from the old Node.js backend") -- but THIS copy, the one
    # actually invoked by SupervisorClient.cs/BackendProcess.cs, was missed,
    # importing a module (app/local_tts_launcher.py) that no longer exists
    # in the repo at all. Nothing replaces this call: local_edge_tts is
    # called in-process, on demand, from voice_api.py's own TTS path, not
    # launched once at startup.
    #
    # Two listeners (2026-10-05), same app/event loop -- see main.py's
    # _require_remote_owner_session for the SW-session gate that makes
    # REMOTE_PORT (0.0.0.0, reachable from another device) safe to serve the
    # exact same routes as PORT (127.0.0.1, what the WPF/GTK shells use,
    # unchanged/unauthenticated as before this feature existed).
    async def _serve_remote_forever() -> None:
        # The local listener (what the WPF/GTK shells actually depend on to
        # function at all) must never go down because the remote one
        # couldn't bind -- a busy REMOTE_PORT, a firewall, no route to
        # 0.0.0.0, etc. is a real possibility on a machine this wasn't
        # tested on, and remote control is a bonus feature, not something
        # the local app should regress over. Caught and logged, not raised.
        remote = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=REMOTE_PORT, log_level="info"))
        try:
            await remote.serve()
        except BaseException as exc:
            # Bug fix (2026-10-05), found live: uvicorn's own Server.startup()
            # calls sys.exit() on a bind failure (port in use, no route to
            # 0.0.0.0, etc.), which raises SystemExit -- NOT a subclass of
            # Exception. An `except Exception` here looked right but did
            # nothing: SystemExit propagated straight out of this task,
            # unhandled, and took the entire asyncio loop down with it --
            # the exact opposite of "local must survive a remote failure"
            # this function exists for. Confirmed live: local's own listener
            # died in the same run. BaseException (and re-raising real
            # interpreter-exit signals below) is the correct catch here.
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            log_event("engine", "remote_listener_failed", port=REMOTE_PORT, error=str(exc))

    async def _serve_both() -> None:
        local = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="info"))
        asyncio.create_task(_serve_remote_forever())
        await local.serve()

    asyncio.run(_serve_both())
