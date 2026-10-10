"""One TLS context for every httpx client in the backend.

httpx builds a fresh ssl.SSLContext for every client it creates, and on
Windows building one means loading the certificate store -- synchronously,
on the event loop. Found 2026-10-10 by app/loop_watchdog.py: every SW API,
memory, Ratatosk and app-browser call (each creates its own AsyncClient)
stopped the whole loop for 2-2.5 s; several such calls at once added up to
the 10-15 s stalls that made sub-second requests take 30-90 s. Building the
context once and passing it as `verify=` keeps exactly the same checks --
the same certifi bundle httpx uses by default -- without re-reading it.

warm_up() builds it in a worker thread at startup, so even the first
client does not pay for it on the loop.
"""

from __future__ import annotations

import asyncio
import ssl
import threading

import httpx

_context: ssl.SSLContext | None = None
_lock = threading.Lock()


def ssl_context() -> ssl.SSLContext:
    """The shared context, httpx's own default (certifi), built on first use."""
    global _context
    if _context is None:
        with _lock:
            if _context is None:
                _context = httpx.create_ssl_context()
    return _context


async def warm_up() -> None:
    await asyncio.to_thread(ssl_context)
