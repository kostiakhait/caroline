import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { spawn } from "node:child_process";
import { readFile, writeFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { z } from "zod";

// Same bundled ffmpeg.exe Caroline's Visual Mode / screen-video server already use
// (see MCP/screen-video/src/index.ts's own copy of this comment) -- located via the
// same CAROLINE_FFMPEG_PATH env var, inherited automatically since this server is
// launched as a plain child of the backend process that has it set.
const FFMPEG = process.env.CAROLINE_FFMPEG_PATH || "ffmpeg";

const VIDEO_EXTENSIONS = new Set(["mp4", "mkv", "mov", "webm", "avi", "m4v", "wmv", "flv"]);

/** Extracts the audio track from a video file to a temp MP3 via ffmpeg, so
 *  speech_to_text can accept a video file directly (the user pointing at "this
 *  video", not just a standalone audio file) instead of the model having to figure
 *  out and run its own ffmpeg extraction command first. */
async function extractAudioToTempMp3(videoPath: string): Promise<string> {
  const outPath = join(await mkdtemp(join(tmpdir(), "mcp-voice-extract-")), "audio.mp3");
  await new Promise<void>((resolve, reject) => {
    const proc = spawn(FFMPEG, ["-y", "-i", videoPath, "-vn", "-acodec", "libmp3lame", outPath]);
    let stderrAll = "";
    proc.stderr.on("data", (d) => { stderrAll += d.toString(); });
    proc.on("error", reject);
    proc.on("exit", (code) => {
      if (code === 0) { resolve(); return; }
      // Last few lines only -- ffmpeg's own startup banner (build config, library
      // versions) is otherwise noisy enough to bury the actual error underneath it.
      const lastLines = stderrAll.trim().split(/\r?\n/).slice(-6).join("\n");
      reject(new Error(`ffmpeg exited with code ${code}: ${lastLines}`));
    });
  });
  return outPath;
}

// Same Camerlengo v2 API (ai:tts / ai:stt) Caroline's own backend already uses
// in-process for her automatic voice pipeline (see Caroline/backend/src/voice.ts) --
// duplicated here (not imported) since this is a separately-built package, same
// pattern as every other small deliberate duplication between this repo's
// independently-published projects. This server is for ON-DEMAND use (the user
// asking Caroline to voice something or transcribe an audio file), not a
// replacement for that existing automatic pipeline, which stays as-is.
const API_URL = "https://www.squirrelwisdom.com/";
const API_KEY = "QvR-sujLOgpKWZ-yhSOK5ZNgEe4sgF0EUU7GexQqr4M";

interface ApiEnvelope {
  ".status": "ok" | "error";
  [key: string]: unknown;
}

async function callApi(body: Record<string, unknown>, timeoutMs: number): Promise<ApiEnvelope> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: API_KEY, ...body }),
      signal: controller.signal,
    });
    return (await res.json()) as ApiEnvelope;
  } finally {
    clearTimeout(timer);
  }
}

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function errorResult(text: string) {
  return { isError: true, content: [{ type: "text" as const, text }] };
}

function buildServer(): McpServer {
  const server = new McpServer({ name: "voice", version: "1.0.0" });

  server.registerTool(
    "text_to_speech",
    {
      title: "Synthesize speech from text",
      description:
        "Converts text to spoken audio (MP3) via Camerlengo's ai:tts, on the user's explicit request -- " +
        "e.g. \"say this out loud\", \"make an audio file of X\". This is separate from Caroline's own " +
        "automatic voice-reply pipeline; use this only when the user specifically wants an audio FILE " +
        "produced, not for normal spoken replies.",
      inputSchema: {
        text: z.string().describe("Text to synthesize."),
        voice: z.enum(["Nova", "Onyx"]).optional().describe('Voice to use ("Nova" = female, "Onyx" = male). Defaults to "Nova".'),
        savePath: z.string().optional().describe("Absolute path to save the MP3 to. Defaults to a new file in the system temp directory."),
        session: z.string().optional().describe("SquirrelWisdom v2 session token, to bill this call against that account's wallet. Omit to call unbilled/scope-gated-only."),
      },
    },
    async ({ text, voice, savePath, session }) => {
      try {
        const data = await callApi({ command: "ai:tts", text, voice: voice ?? "Nova", ...(session ? { session } : {}) }, 60_000);
        if (data[".status"] !== "ok" || typeof data.result !== "string") {
          if ((data as any)[".errcode"] === "402") return errorResult("insufficient_balance: the SquirrelWisdom wallet backing this call is out of funds.");
          return errorResult(String((data as any)[".reason"] ?? "TTS failed"));
        }
        const buffer = Buffer.from(data.result, "base64");
        const outPath = savePath ?? join(await mkdtemp(join(tmpdir(), "mcp-voice-")), `${randomUUID()}.mp3`);
        await writeFile(outPath, buffer);
        return textResult(`Saved ${buffer.length} byte(s) of audio to "${outPath}".`);
      } catch (err) {
        return errorResult(`text_to_speech failed: ${(err as Error).message}`);
      }
    }
  );

  server.registerTool(
    "speech_to_text",
    {
      title: "Transcribe speech from an audio or video file",
      description:
        "Transcribes a local audio OR video file to text via Camerlengo's ai:stt, on the user's explicit " +
        "request -- e.g. \"what does this recording say\", \"transcribe this voice memo\", \"what's said in " +
        "this video\". Given a video file (mp4/mkv/mov/webm/avi/m4v/wmv/flv), its audio track is extracted " +
        "automatically first -- just pass the video path directly, no separate extraction step needed. " +
        "Combine with sample_video_frames (see the analyzing-video skill) to understand a video's audio AND " +
        "visuals together. Separate from Caroline's own automatic voice-input pipeline (the mic button); use " +
        "this for a standalone file the user points at, not live voice input.",
      inputSchema: {
        filePath: z.string().describe("Absolute path to the local audio or video file to transcribe."),
        format: z.string().optional().describe('Audio format/extension (e.g. "mp3", "wav", "m4a"). Defaults to the file\'s own extension; ignored for video files (always extracted as mp3).'),
        session: z.string().optional().describe("SquirrelWisdom v2 session token, to bill this call against that account's wallet. Omit to call unbilled/scope-gated-only."),
      },
    },
    async ({ filePath, format, session }) => {
      const ext = filePath.split(".").pop()?.toLowerCase() ?? "";
      const isVideo = VIDEO_EXTENSIONS.has(ext);
      let audioPath = filePath;
      let extractedDir: string | null = null;
      try {
        if (isVideo) {
          audioPath = await extractAudioToTempMp3(filePath);
          extractedDir = join(audioPath, "..");
        }
        const buffer = await readFile(audioPath);
        const inferredFormat = isVideo ? "mp3" : (format ?? ext ?? "mp3");
        const audioBase64 = buffer.toString("base64");
        const data = await callApi({ command: "ai:stt", audio: audioBase64, format: inferredFormat, ...(session ? { session } : {}) }, 60_000);
        if (data[".status"] !== "ok" || typeof data.result !== "string") {
          if ((data as any)[".errcode"] === "402") return errorResult("insufficient_balance: the SquirrelWisdom wallet backing this call is out of funds.");
          return errorResult(String((data as any)[".reason"] ?? "STT failed"));
        }
        return textResult(data.result);
      } catch (err) {
        return errorResult(`speech_to_text failed: ${(err as Error).message}`);
      } finally {
        if (extractedDir) await rm(extractedDir, { recursive: true, force: true }).catch(() => {});
      }
    }
  );

  return server;
}

// --http-port <port> lets many query() instances (one per Caroline tab) share
// ONE running copy of this server instead of each spawning its own -- see
// MCP/time/src/index.ts's own copy of this comment for the full history (a
// 240+ node.exe process swarm, 2026-09-06) and MCP/notes/src/index.ts's own
// copy for why each request needs a FRESH server+transport (a stateless
// transport throws "Stateless transport cannot be reused across requests" on
// every request after its first if reused, confirmed live 2026-09-06). Falls
// back to stdio when the flag is absent, for any caller (an interactive
// `claude` session's own project-scoped .mcp.json, for instance) that still
// expects to spawn its own copy.
const httpPortIdx = process.argv.indexOf("--http-port");
if (httpPortIdx >= 0) {
  const port = Number(process.argv[httpPortIdx + 1]);
  createHttpServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/mcp") {
      res.writeHead(404).end();
      return;
    }
    const server = buildServer();
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res);
    } catch (err) {
      console.error("[mcp] request handling failed:", err);
      if (!res.headersSent) res.writeHead(500).end();
    }
    res.on("close", () => {
      transport.close();
      server.close();
    });
  }).listen(port, "127.0.0.1", () => {
    console.error(`[mcp] listening on http://127.0.0.1:${port}/mcp`);
  });
} else {
  const server = buildServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}
