"""Persistent local text-to-speech server for Caroline's voice pipeline.

Wraps edge-tts (Microsoft's free cloud TTS service, no API key, no per-call
billing -- see Caroline/backend/src/voice.ts) behind a tiny local HTTP server,
started once and kept running for as long as Caroline's backend is (see
localTtsServer.ts), instead of spawning a fresh Python interpreter per TTS
call. Per explicit instruction (2026-09-07): the point is cutting per-call
latency (no Camerlengo round trip, no repeated Python startup cost), not
reducing cost -- the Camerlengo ai:tts path stays as the fallback whenever this
fails and a SquirrelWisdom session is available (see voice.ts).

Deliberately dependency-free beyond edge-tts itself (stdlib http.server, not
Flask/FastAPI) -- this whole thing is small enough not to need a real web
framework, and every extra pip dependency is one more thing that can fail to
install on some user's machine.
"""
import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import edge_tts


async def synthesize(text: str, voice: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice)
    chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    return b"".join(chunks)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Route through stderr with our own prefix instead of BaseHTTPRequestHandler's
        # default access-log format, so this shows up clearly in caroline.log next to
        # everything else (the backend pipes this process's stderr straight through --
        # see localTtsServer.ts).
        sys.stderr.write(f"[local-tts-server] {self.address_string()} - {fmt % args}\n")

    def do_POST(self):
        if self.path != "/tts":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            text = body["text"]
            voice = body.get("voice", "en-US-AriaNeural")
            audio = asyncio.run(synthesize(text, voice))
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
        except Exception as ex:  # noqa: BLE001 -- deliberately broad: any failure here
            # must produce a clean 500 the Node caller can fall back from, never an
            # unhandled exception that kills this persistent server.
            sys.stderr.write(f"[local-tts-server] /tts failed: {ex}\n")
            try:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(str(ex).encode("utf-8"))
            except Exception as write_ex:  # noqa: BLE001
                sys.stderr.write(f"[local-tts-server] failed to even send the 500 response: {write_ex}\n")


def main():
    port = 9500
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write(f"[local-tts-server] listening on http://127.0.0.1:{port}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
