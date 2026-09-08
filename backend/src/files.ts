import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { z } from "zod";
import { tool, createSdkMcpServer, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";

/**
 * An in-process MCP tool letting Caroline open a local file (image, video,
 * document, anything) in whatever the user's default Windows viewer/editor
 * for that file type is -- the same as double-clicking it in Explorer.
 *
 * Complements (doesn't replace) inline chat previews: a photo she already
 * has shipped as an asset renders directly in the bubble via Markdown image
 * syntax (see persona.ts), but for an arbitrary file on disk -- something
 * the user asks her to open, or one she just created/downloaded -- there's
 * no way to embed it in the chat page (it's not under wwwroot), so this is
 * the fallback: hand it to the OS instead of failing or just describing it.
 */
/** Shared by the open_file tool and the chat UI's clickable document links (server.ts's "open_file" control op). */
export function openFileWithDefaultApp(path: string): void {
  const child = spawn("cmd.exe", ["/c", "start", "", path], { detached: true, stdio: "ignore" });
  child.unref();
}

export function createFileOpenerTool(): McpServerConfig {
  const openFile = tool(
    "open_file",
    "Open a local file in the user's default Windows application for that file type " +
      "(image viewer, video player, PDF reader, Office, etc.) -- exactly like double-clicking " +
      "it in File Explorer. Use this when the user asks you to open/show a file that isn't one " +
      "of your own shipped photos, or when you want to show them something you just created.",
    {
      path: z.string().describe("Absolute path to the file to open."),
    },
    async ({ path }) => {
      console.error(`[caroline] [tool:open_file] path=${path}`);
      if (!existsSync(path)) {
        console.error(`[caroline] [tool:open_file] path=${path} not found`);
        return { content: [{ type: "text", text: `No such file: ${path}` }], isError: true };
      }
      try {
        openFileWithDefaultApp(path);
        console.error(`[caroline] [tool:open_file] path=${path} opened`);
        return { content: [{ type: "text", text: `Opened ${path} in its default application.` }] };
      } catch (e) {
        console.error(`[caroline] [tool:open_file] path=${path} failed: ${(e as Error).message}`);
        return { content: [{ type: "text", text: `Failed to open ${path}: ${(e as Error).message}` }], isError: true };
      }
    },
  );

  return createSdkMcpServer({
    name: "caroline-files",
    tools: [openFile],
  });
}
