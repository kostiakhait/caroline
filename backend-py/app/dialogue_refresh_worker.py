"""Entry point for rebuilding a tab's "last 24h of dialogue" file in a
SEPARATE PROCESS -- `python <this file> <session_id|-> <tab_id> <workspace_dir>`
(run as a script, not `-m`: the bundled embeddable Python's ._pth file keeps the
working directory off sys.path, so the package root is added explicitly below).

Why a process and not a thread (2026-09-23, after a live outage where all four
tabs sat in "reconnecting" for minutes): building that file walks the tab's
transcript backwards and JSON-parses every line inside the window, and once a
session has been rotated the window always reaches into the multi-hundred-MB
cold archive -- measured at 15-152 s per refresh. That is pure-Python work, so
even in a worker thread it holds the GIL and starves the asyncio event loop
(health-watchdog then sees /api/status stall and declares the backend frozen,
restarts it, and every restarted session kicks off another refresh). A
separate process has its own GIL. See ChatSession._refresh_recent_24h_dialogue_async.
"""

from __future__ import annotations

import sys
from pathlib import Path

# backend-py/ (the directory that CONTAINS the app package) -- see module docstring.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    session_id = None if argv[1] == "-" else argv[1]
    tab_id, workspace_dir = argv[2], argv[3]
    from app.chat_session import _write_recent_24h_dialogue_file

    _write_recent_24h_dialogue_file(session_id, tab_id, workspace_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
