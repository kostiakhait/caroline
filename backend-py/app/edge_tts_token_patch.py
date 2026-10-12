"""Patches edge_tts's own Sec-MS-GEC token generator (edge_tts.drm.DRM.
generate_sec_ms_gec) to Microsoft's new V2 algorithm.

Confirmed live (2026-10-12): Microsoft shipped a new Edge release (155.x)
that changed how the Sec-MS-GEC token is computed -- the vendored edge_tts
package (7.2.8, the latest release as of this fix; no newer release exists
and the upstream maintainer has not responded since March) still only
implements the OLD (V1) algorithm, a plain SHA256 of "<ticks><trusted
client token>". Every real request now gets `403, message='Invalid
response status'` on the WebSocket handshake -- reproduced directly
against this exact install. Tracked upstream: https://github.com/rany2/
edge-tts/issues/490 (open, unresolved, hundreds of reports worldwide,
confirmed Microsoft-side, not specific to this app or network).

The V2 algorithm below is a community reverse-engineering of msedge.dll
(credited to GitHub user gexgd0419, issue #490's own comments) covering
MicrosoftGenuineEdgeToken::TokenValue's regular (non-automation) path: a
Microsoft copyright string is prefixed to the usual "<ticks><token>"
string, then hashed with both SHA-256 and a proprietary 32-bit "patent
hash" (CS64) mixing function; the final token is `<16-hex CS64>-<64-hex
SHA256>`. The exact copyright string is Microsoft's own fixed salt baked
into their algorithm -- required verbatim for the hash to match what
their server independently computes, not something of ours to alter.

Only the TOKEN VALUE and its version-string accompaniment change here --
the request still goes to the same, still-live legacy endpoint
(speech.platform.bing.com, not Edge 155's own newer api.msedgeservices.com
one), confirmed live to accept a correctly-computed V2 token on that same
old URL. SEC_MS_GEC_VERSION must be patched too (edge_tts hardcodes "1-"
+ an old Chromium build number) -- Microsoft's server checks that this
version prefix/number is itself a real, current Edge release, independent
of whether the token hash is correct.

Monkey-patched the same way win_subprocess_patch.py patches anyio.open_
process: both DRM.generate_sec_ms_gec (a method on a class object, so
patching it in place is visible to every caller, edge_tts's own included)
and communicate.SEC_MS_GEC_VERSION (a plain module-level name that
edge_tts.communicate already imported BY VALUE, so the constants.py
original must be patched there too, not just at its own definition site,
or the stale import keeps winning).

Revisit CURRENT_EDGE_VERSION whenever Microsoft rotates it again (same
staleness class the upstream project's own daily CI job exists to catch,
per that project's README) -- there's no live way to detect this from
here short of re-deriving it from a fresh Edge release each time it
breaks again.
"""

from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timezone

_MASK = 0xFFFFFFFF
_COPYRIGHT = b"Copyright (C) Microsoft Corporation. All rights reserved."
_WINDOW_US = 300_000_000
_UNIX_TO_FILETIME_US = 11_644_473_600 * 1_000_000

# Confirmed working live (2026-10-12) against the exact token shown in
# https://github.com/rany2/edge-tts/issues/490 -- rotates with every Edge
# release, same as edge_tts's own CHROMIUM_FULL_VERSION constant already did.
CURRENT_EDGE_VERSION = "155.0.4283.45"

_applied = False


def _u32(x: int) -> int:
    return x & _MASK


def _rol16(x: int) -> int:
    x &= _MASK
    return ((x << 16) | (x >> 16)) & _MASK


def _words_le4_even(data: bytes) -> list[int]:
    padded = data + b"\x00" * ((-len(data)) % 4)
    if not padded:
        return []
    words = list(struct.unpack("<" + "I" * (len(padded) // 4), padded))
    if len(words) % 2:
        words.append(0)
    return words


def _cs64_word_swap(words: list[int], key_words: list[int]) -> tuple[int, int]:
    a, b = key_words[0] | 1, key_words[1] | 1
    acc = last = 0
    for x, y in zip(words[0::2], words[1::2]):
        t = _u32(_rol16(x) * 0xEF0569FB + x * a)
        t = _rol16(t)
        q = _u32(t * 0x76F50F34)
        q = _u32(_rol16(q) * 0xC3EFEA97 + q)
        acc = _u32(acc + q)
        t = _u32(_rol16(y) * 0xC31713DB + y * b)
        t = _rol16(t)
        q = _u32(t * 0x1881CE3C)
        last = _u32(_rol16(q) * 0x35BD1EC9 + q)
        acc = _u32(acc + last)
    return last, acc


def _cs64_reversible(words: list[int], key_words: list[int]) -> tuple[int, int]:
    a, b = key_words[0] | 1, key_words[1] | 1
    state = acc = 0
    for x, y in zip(words[0::2], words[1::2]):
        state = _u32((state + x) * a)
        for const in (0xCF98B111, 0x87085B9F, 0x12CEB96D, 0x257E1D83):
            state = _u32(_rol16(state) * const)
        t = _u32((y + state) * b)
        t = _u32(_rol16(t) * 0xA27416F5)
        t = _u32(_rol16(t) * 0xD38396FF)
        t = _rol16(t)
        acc = _u32(acc + state)
        state = _u32(_rol16(_u32(t * 0x7C932B89)) * 0xBFA49F69)
        acc = _u32(acc + state)
    return state, acc


def _patent_hash(data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).digest()
    data_words = _words_le4_even(data)
    digest_words = _words_le4_even(digest)
    x0, y0 = _cs64_word_swap(data_words, digest_words)
    x1, y1 = _cs64_reversible(data_words, digest_words)
    cs64_hex = struct.pack("<II", x0 ^ x1, y0 ^ y1).hex().upper()
    return cs64_hex, digest.hex().upper()


def _filetime_ticks_from_unix_us(unix_us: int) -> int:
    chromium_us = unix_us + _UNIX_TO_FILETIME_US
    return (chromium_us // _WINDOW_US) * _WINDOW_US * 10


def generate_sec_ms_gec_v2(trusted_client_token: str) -> str:
    """Microsoft's new V2 Sec-MS-GEC token: `<16-hex CS64>-<64-hex SHA256>`,
    bucketed to the current 5-minute window, same clock-bucketing edge_tts's
    own V1 generator already used."""
    now = datetime.now(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = now - epoch
    unix_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    ticks = _filetime_ticks_from_unix_us(unix_us)
    suffix = str(ticks).encode("ascii") + trusted_client_token.encode("ascii")
    cs64_hex, sha_hex = _patent_hash(_COPYRIGHT + suffix)
    return f"{cs64_hex}-{sha_hex}"


def apply() -> None:
    global _applied
    if _applied:
        return
    import edge_tts.communicate as _ec
    import edge_tts.constants as _const
    from edge_tts.drm import DRM

    def _patched_generate_sec_ms_gec() -> str:
        return generate_sec_ms_gec_v2(_const.TRUSTED_CLIENT_TOKEN)

    DRM.generate_sec_ms_gec = staticmethod(_patched_generate_sec_ms_gec)
    # communicate.py did `from .constants import SEC_MS_GEC_VERSION` --
    # a by-value import already resolved at its own import time, so the
    # name must be re-patched on communicate.py itself, not constants.py.
    _ec.SEC_MS_GEC_VERSION = f"2-{CURRENT_EDGE_VERSION}"
    _applied = True
