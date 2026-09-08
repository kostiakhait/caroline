import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { spawn, type ChildProcessByStdio } from "node:child_process";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { existsSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Writable } from "node:stream";
import { z } from "zod";

// Same bundled ffmpeg.exe Caroline's Visual Mode already uses (see
// VisualModeManager.cs's ResolveFfmpegPath / FfmpegInstaller.cs), located via the
// same CAROLINE_FFMPEG_PATH env var BackendProcess.cs sets on the backend it
// spawns -- inherited here automatically since this server is launched as a plain
// child of that same backend process (see sharedMcpServers.ts, no env override
// there), no separate plumbing needed. Falls back to a bare "ffmpeg" PATH lookup
// for dev runs / machines without the bundled copy, same fallback Visual Mode uses.
const FFMPEG = process.env.CAROLINE_FFMPEG_PATH || "ffmpeg";

// Reference implementation: reforce/Tools/Screen Capture/screen_capture.pyw --
// gdigrab (Windows GDI desktop capture) into an H.264 MP4, silent audio track so
// the container is a normal playable video, "ultrafast" preset so encoding never
// falls behind real time on a slower machine. Graceful stop is sending "q" on
// stdin (ffmpeg's own documented quit key), not killing the process -- killing it
// leaves the MP4's moov atom unwritten, producing an unplayable/truncated file.
interface ActiveRecording {
  proc: ChildProcessByStdio<Writable, null, import("node:stream").Readable>;
  outputPath: string;
  startedAt: number;
}
let activeRecording: ActiveRecording | null = null;

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function errorResult(text: string) {
  return { isError: true, content: [{ type: "text" as const, text }] };
}

function buildServer(): McpServer {
  const server = new McpServer({ name: "screen-video", version: "1.0.0" });

  server.registerTool(
    "start_screen_recording",
    {
      title: "Start recording the screen to video",
      description:
        "Starts recording the full desktop to an MP4 file (30fps, H.264) -- use this to capture something " +
        "happening over TIME (an animation, a multi-step UI flow, a game, anything a single screenshot can't " +
        "show) instead of app_browser_screenshot/take_screenshot. Call stop_screen_recording when done, then " +
        "sample_video_frames to actually look at what happened. Only one recording can be active at a time.",
      inputSchema: {
        savePath: z.string().optional().describe("Absolute path to save the MP4 to. Defaults to a new file in the system temp directory."),
      },
    },
    async ({ savePath }) => {
      if (activeRecording) {
        return errorResult(`A recording is already in progress (${activeRecording.outputPath}). Call stop_screen_recording first.`);
      }
      try {
        const outputPath = savePath ?? join(await mkdtemp(join(tmpdir(), "mcp-screen-video-")), `recording-${Date.now()}.mp4`);
        const args = [
          "-y",
          "-f", "gdigrab", "-framerate", "30", "-i", "desktop",
          "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
          "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
          "-crf", "23", "-preset", "ultrafast",
          outputPath,
        ];
        const proc = spawn(FFMPEG, args, { stdio: ["pipe", "ignore", "pipe"] });
        let stderrAll = "";
        proc.stderr.on("data", (d) => {
          stderrAll += d.toString();
        });
        const startupError = await new Promise<string | null>((resolve) => {
          // Last few lines only -- ffmpeg's own startup banner (build config, library
          // versions) is otherwise noisy enough to bury the actual error underneath it.
          const lastLines = () => stderrAll.trim().split(/\r?\n/).slice(-6).join("\n");
          proc.once("error", (err) => resolve(err.message));
          proc.once("exit", (code) => resolve(code !== null && code !== 0 ? `ffmpeg exited immediately with code ${code}: ${lastLines()}` : null));
          setTimeout(() => resolve(null), 800); // still running after 800ms -- looks like it started fine
        });
        if (startupError) {
          return errorResult(`start_screen_recording failed: ${startupError}`);
        }
        activeRecording = { proc, outputPath, startedAt: Date.now() };
        return textResult(`Recording started -> ${outputPath}`);
      } catch (err) {
        return errorResult(`start_screen_recording failed: ${(err as Error).message}`);
      }
    }
  );

  server.registerTool(
    "stop_screen_recording",
    {
      title: "Stop the current screen recording",
      description: "Stops the recording started by start_screen_recording and finalizes the MP4 file.",
      inputSchema: {},
    },
    async () => {
      if (!activeRecording) {
        return errorResult("No recording is currently in progress.");
      }
      const { proc, outputPath, startedAt } = activeRecording;
      activeRecording = null;
      try {
        const exited = new Promise<void>((resolve) => proc.once("exit", () => resolve()));
        proc.stdin.write("q\n");
        proc.stdin.end();
        const timedOut = await Promise.race([exited.then(() => false), new Promise<boolean>((r) => setTimeout(() => r(true), 5000))]);
        if (timedOut) {
          console.error(`[mcp-screen-video] stop_screen_recording: graceful quit timed out, terminating pid=${proc.pid}`);
          proc.kill();
        }
        const durationSec = (Date.now() - startedAt) / 1000;
        const size = existsSync(outputPath) ? statSync(outputPath).size : 0;
        return textResult(`Recording stopped -> ${outputPath} (${durationSec.toFixed(1)}s, ${size} byte(s)).`);
      } catch (err) {
        return errorResult(`stop_screen_recording failed: ${(err as Error).message}`);
      }
    }
  );

  server.registerTool(
    "sample_video_frames",
    {
      title: "Sample frames from a video file to see motion/dynamics",
      description:
        "Extracts a handful of frames from a video file (from start_screen_recording, or any other video) at a " +
        "fixed frame interval and returns them as images, in order -- lets you see change over time by comparing " +
        "frames, instead of a single still image. Capped at a small number of frames per call to keep this cheap; " +
        "call again with a higher startFrame to page through a longer video.",
      inputSchema: {
        videoPath: z.string().describe("Absolute path to the video file."),
        everyNthFrame: z.number().int().positive().optional().describe("Take every Nth frame (source is 30fps, so e.g. 30 = ~1 frame/sec, 15 = ~2 frames/sec). Defaults to 30."),
        startFrame: z.number().int().min(0).optional().describe("Frame index to start sampling from. Defaults to 0."),
        maxFrames: z.number().int().positive().max(20).optional().describe("Maximum number of frames to return (hard cap 20, to keep this cheap). Defaults to 8."),
      },
    },
    async ({ videoPath, everyNthFrame, startFrame, maxFrames }) => {
      const n = everyNthFrame ?? 30;
      const start = startFrame ?? 0;
      const limit = Math.min(maxFrames ?? 8, 20);
      let tmpDir: string | null = null;
      try {
        if (!existsSync(videoPath)) return errorResult(`No such file: ${videoPath}`);
        tmpDir = await mkdtemp(join(tmpdir(), "mcp-screen-video-frames-"));
        const outPattern = join(tmpDir, "frame_%04d.png");
        const selectExpr = start > 0 ? `select='gte(n\\,${start})*not(mod(n-${start}\\,${n}))'` : `select='not(mod(n\\,${n}))'`;
        await new Promise<void>((resolve, reject) => {
          const proc = spawn(FFMPEG, ["-y", "-i", videoPath, "-vf", selectExpr, "-vsync", "vfr", "-frames:v", String(limit), outPattern]);
          let stderrAll = "";
          proc.stderr.on("data", (d) => { stderrAll += d.toString(); });
          proc.on("error", reject);
          proc.on("exit", (code) => {
            if (code === 0) { resolve(); return; }
            const lastLines = stderrAll.trim().split(/\r?\n/).slice(-6).join("\n");
            reject(new Error(`ffmpeg exited with code ${code}: ${lastLines}`));
          });
        });
        const files = (await readdir(tmpDir)).filter((f) => f.endsWith(".png")).sort();
        if (files.length === 0) {
          return errorResult(`No frames extracted -- startFrame (${start}) may be past the end of the video, or the video has no frames matching the interval.`);
        }
        const content: Array<{ type: "text"; text: string } | { type: "image"; data: string; mimeType: string }> = [
          { type: "text", text: `Extracted ${files.length} frame(s) from ${videoPath}, starting at frame ${start}, every ${n} frame(s):` },
        ];
        for (const file of files) {
          const buffer = await readFile(join(tmpDir, file));
          content.push({ type: "image", data: buffer.toString("base64"), mimeType: "image/png" });
        }
        return { content };
      } catch (err) {
        return errorResult(`sample_video_frames failed: ${(err as Error).message}`);
      } finally {
        if (tmpDir) await rm(tmpDir, { recursive: true, force: true }).catch(() => {});
      }
    }
  );

  return server;
}

// --http-port <port> lets many query() instances (one per Caroline tab) share
// ONE running copy of this server instead of each spawning its own -- see
// MCP/time/src/index.ts's own copy of this comment for the full history, and
// MCP/notes/src/index.ts's own copy for why each request needs a FRESH
// server+transport. Falls back to stdio when the flag is absent.
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
