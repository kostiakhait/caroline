"""uvicorn's access log without the status polls that answered fine.

The shell asks /api/status and /api/update/status about every 40 s, and the supervisor's /status is asked about
every 20 s: 2026-11-20..22 that was 19,190 access lines, 24% of caroline.log, all "200 OK". A poll that fails
(4xx/5xx) is still logged, and so is every other request."""

from __future__ import annotations

import logging

QUIET_POLL_PATHS = frozenset({"/status", "/api/status", "/api/update/status"})


class QuietPollFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn: '%s - "%s %s HTTP/%s" %d' % (client, method, path_with_query, http_version, status)
        if isinstance(args, tuple) and len(args) == 5:
            method, path, status = args[1], str(args[2]), args[4]
            if method == "GET" and path.split("?", 1)[0] in QUIET_POLL_PATHS and isinstance(status, int) and status < 400:
                return False
        return True


def install() -> None:
    """On the uvicorn.access logger; safe to call more than once, and before or after uvicorn configures logging."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietPollFilter) for f in logger.filters):
        logger.addFilter(QuietPollFilter())
