import { existsSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { extname } from "node:path";
import { z } from "zod";
import { tool, createSdkMcpServer, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";
import { prepareOfficeEditSession } from "./officeEditor.js";
import { requireSwOrPrompt } from "./swGate.js";

const IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]);
const VIDEO_EXT = new Set([".mp4", ".webm", ".mov", ".avi", ".mkv"]);

export type ViewerKind = "image" | "video" | "document";

export interface ViewerResult {
  outcome: "saved" | "cancelled" | "closed" | "error";
  path: string;
  message?: string;
}

function kindOf(path: string): ViewerKind {
  const ext = extname(path).toLowerCase();
  if (IMAGE_EXT.has(ext)) return "image";
  if (VIDEO_EXT.has(ext)) return "video";
  return "document";
}

interface OpenRequest {
  path: string;
  /** Set only for "document" kind -- the throwaway temp path on
   *  squirrelwisdom.com holding this session's edits (see officeEditor.ts).
   *  Needed by server.ts's "editor_result" handler to pull the saved
   *  content back down once the editor window closes. */
  remotePath?: string;
}

/** requestId -> bookkeeping, so a later "editor_result" can be described by
 *  path in the proactive nudge, and (for documents) synced back to disk. */
const openRequests = new Map<string, OpenRequest>();

/**
 * Called from server.ts's "editor_result" control op once the WPF viewer
 * window actually closes -- which can be seconds or the better part of an
 * hour later (the user is editing a document at their own pace). Doesn't
 * resolve anything a tool call is waiting on -- open_in_viewer already
 * returned as soon as the window opened, see its own doc comment for why --
 * this just tells the caller (server.ts) to nudge Caroline about it as a
 * new proactive turn, the same mechanism reminders use.
 */
export function takeViewerRequest(requestId: string): OpenRequest | undefined {
  const req = openRequests.get(requestId);
  openRequests.delete(requestId);
  return req;
}

export interface OfficeConfig {
  documentType: string;
  fileType: string;
  editable: boolean;
  key: string;
  documentUrl: string;
  onlyofficeUrl: string;
  title: string;
  callbackUrl?: string;
}

/**
 * Opens a local file in Caroline's own floating viewer/editor window (not
 * the OS default app -- see open_file for that) -- images and video display
 * directly; office documents (docx/xlsx/pptx/pdf) open for real editing via
 * an embedded OnlyOffice editor -- the same OnlyOffice Document Server
 * Notes already uses in production (see officeEditor.ts's doc comment),
 * not a live local LibreOffice process.
 *
 * Returns immediately once the window is open -- it does NOT wait for the
 * user to finish. A document edit can sit open for a long time at the
 * user's own pace, and this tool call is one turn inside one ongoing SDK
 * session; blocking it would freeze the whole conversation (no replies to
 * anything else) for however long that takes. Instead, when the window
 * closes, the outcome arrives as a separate proactive message (see
 * server.ts's due-check-style handling of "editor_result") -- the same
 * pattern reminders use to speak up on their own schedule rather than
 * inside the turn that scheduled them.
 *
 * Round trip for images/video: this tool -> WS "open_editor" push ->
 * chat.js relays to the WPF shell via window.chrome.webview -> WPF opens
 * DocumentViewerWindow. For documents: this tool uploads the file to a
 * throwaway temp path on squirrelwisdom.com, asks for an OnlyOffice editor
 * session for it, and pushes "open_office_editor" instead -- the WPF window
 * hosts a WebView2 pointed at OnlyOffice's own editor JS, not a reparented
 * soffice.exe window.
 */
type ViewerEvent =
  | { type: "open_editor"; requestId: string; path: string; kind: "image" | "video" }
  | { type: "open_office_editor"; requestId: string; path: string; config: OfficeConfig }
  | { type: "close_editor"; path: string }
  // Not really a "viewer" event -- included so open_in_viewer's document
  // branch can gate on SquirrelWisdom login the same way every other
  // SW-gated tool does (see swGate.ts's requireSwOrPrompt), reusing this
  // same sendToFrontend callback rather than threading a second one through.
  | { type: "open_login"; requestId: string; error?: string };

export function createViewerTool(sendToFrontend: (event: ViewerEvent) => void): McpServerConfig {
  const openInViewer = tool(
    "open_in_viewer",
    "Open a local image, video, or document in Caroline's own floating viewer window (separate from the " +
      "chat). Images/video just display; documents (docx/xlsx/pptx/pdf) open for real editing via an " +
      "embedded OnlyOffice editor -- this requires the user to be logged into SquirrelWisdom (see " +
      "ensure_squirrelwisdom_login) and a working internet connection, since the document is briefly " +
      "uploaded there to be edited and synced back. Returns immediately -- it does not wait for them to " +
      "finish, since that could take a while and would otherwise freeze the whole conversation. You'll be " +
      "nudged separately, as a new message, once they're done -- react to that when it arrives rather than " +
      "assuming an outcome now.",
    { path: z.string().describe("Absolute path to the file to open.") },
    async ({ path }) => {
      console.error(`[caroline] [tool:open_in_viewer] path=${path}`);
      if (!existsSync(path)) {
        console.error(`[caroline] [tool:open_in_viewer] path=${path} not found`);
        return { content: [{ type: "text", text: `No such file: ${path}` }], isError: true };
      }
      const requestId = randomUUID();
      const kind = kindOf(path);
      if (kind === "document") {
        const gate = requireSwOrPrompt(sendToFrontend);
        if (!gate.ok) {
          return { content: [{ type: "text", text: gate.message }], isError: true };
        }
        try {
          const { config, remotePath } = await prepareOfficeEditSession(path);
          openRequests.set(requestId, { path, remotePath });
          sendToFrontend({ type: "open_office_editor", requestId, path, config });
          console.error(`[caroline] [tool:open_in_viewer] path=${path} requestId=${requestId} opened as office document`);
        } catch (err) {
          console.error(`[caroline] [tool:open_in_viewer] path=${path} failed to prepare office edit session:`, err);
          return {
            content: [{ type: "text", text: `Could not open ${path} for editing: ${err instanceof Error ? err.message : String(err)}` }],
            isError: true,
          };
        }
      } else {
        openRequests.set(requestId, { path });
        sendToFrontend({ type: "open_editor", requestId, path, kind });
        console.error(`[caroline] [tool:open_in_viewer] path=${path} requestId=${requestId} opened as ${kind}`);
      }
      return { content: [{ type: "text", text: `Opened ${path} in the viewer window.` }] };
    },
  );

  const closeViewer = tool(
    "close_viewer",
    "Close the floating viewer window for a file you previously opened with open_in_viewer, without " +
      "waiting for the user to do it themselves. For a document that's open for editing, this closes it " +
      "the same way clicking Cancel does -- unsaved changes are discarded. Use this when you no longer " +
      "need it open (e.g. you opened the wrong file, or the task that needed it is done).",
    { path: z.string().describe("Absolute path of the file whose viewer window should be closed.") },
    async ({ path }) => {
      console.error(`[caroline] [tool:close_viewer] path=${path}`);
      sendToFrontend({ type: "close_editor", path });
      return { content: [{ type: "text", text: `Closed the viewer for ${path}.` }] };
    },
  );

  return createSdkMcpServer({ name: "caroline-viewer", tools: [openInViewer, closeViewer] });
}
