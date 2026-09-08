import { readFileSync, writeFileSync } from "node:fs";
import { basename, extname } from "node:path";
import { randomBytes } from "node:crypto";
import { getSession, SQUIRRELWISDOM_APP_KEY, SQUIRRELWISDOM_API_URL, SQUIRRELWISDOM_ORIGIN } from "./login.js";
import { fetchWithRetry } from "./httpRetry.js";

// document:openForEdit is a v2 command (undotted "command" key -- see
// Camerlengo.py's do_POST routing) with its own separate scoped-key auth
// model, distinct from the legacy APP_KEY used for verifyPassword/write/
// read/delete above -- confirmed live the legacy key gets rejected here
// with v2 error 104 ("Missing, unknown, expired or revoked key"), same as
// voice.ts hit for ai:tts/ai:stt before that got its own key. Minted
// specifically for Caroline, scoped only to document:openForEdit.
const V2_DOCUMENT_KEY = "m83G0G1mvtfT8gIMecDJY8oUaisIMiyfcgH2gvbDvzU";

/**
 * Replaces the old fragile approach (launching a real soffice.exe process and
 * reparenting its window by hand -- see DocumentViewerWindow's git history)
 * with the same OnlyOffice Document Server integration Notes already uses in
 * production (reforce's DocumentCommands.py + squirrel_wisdom/portal's
 * edit_note.html openOfficePreview -- read both directly before touching
 * this file). No reforce changes were needed: document:openForEdit already
 * works for any file reachable under the site's storage root, not just
 * Notes attachments.
 *
 * Caroline's documents live on the user's own machine, not already on
 * reforce's storage, so this uploads the local file to a throwaway
 * random-named path first (the legacy "write" command), asks for an editor
 * session for THAT path, and on close downloads whatever OnlyOffice's own
 * save callback wrote back to it (reforce's handle_onlyoffice_callback
 * already overwrites that exact path server-side -- nothing else to wire up)
 * before deleting the temp copy.
 */

interface OfficeConfig {
  documentType: string;
  fileType: string;
  editable: boolean;
  key: string;
  documentUrl: string;
  onlyofficeUrl: string;
  title: string;
  callbackUrl?: string;
}

async function callApi(body: Record<string, unknown>): Promise<any> {
  const res = await fetchWithRetry(SQUIRRELWISDOM_API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export async function prepareOfficeEditSession(localPath: string): Promise<{ config: OfficeConfig; remotePath: string }> {
  console.error(`[caroline] [officeEditor] prepareOfficeEditSession: localPath=${localPath}`);
  const session = await getSession();
  const ext = extname(localPath).slice(1).toLowerCase();
  // Long random component is the only access control on this temp copy,
  // same model as Notes' own short-lived preview temp files -- acceptable
  // here for the same reason: it exists only for the duration of one
  // editing session and is deleted in finishOfficeEditSession below.
  const remotePath = `caroline_docs/${randomBytes(20).toString("hex")}.${ext}`;

  const content = readFileSync(localPath);
  const writeRes = await callApi({
    ".command": "write", key: SQUIRRELWISDOM_APP_KEY, session,
    path: remotePath, content: content.toString("base64"),
  });
  if (writeRes?.[".status"] !== "ok") {
    throw new Error(String(writeRes?.[".reason"] ?? "Upload to SquirrelWisdom failed."));
  }

  const editRes = await callApi({
    command: "document:openForEdit", key: V2_DOCUMENT_KEY, session,
    path: remotePath, title: basename(localPath), origin: SQUIRRELWISDOM_ORIGIN,
  });
  if (editRes?.[".status"] !== "ok") {
    throw new Error(String(editRes?.[".reason"] ?? "Could not open document for editing."));
  }

  // reforce's makeResponse() mutates the result dict in place and returns it
  // flat (no nested "result" key) -- editRes itself carries these fields
  // alongside ".status"/".msgid".
  const { documentType, fileType, editable, key, documentUrl, onlyofficeUrl, title, callbackUrl } = editRes;
  console.error(`[caroline] [officeEditor] prepareOfficeEditSession: localPath=${localPath} remotePath=${remotePath} editable=${editable} ok`);
  return { config: { documentType, fileType, editable, key, documentUrl, onlyofficeUrl, title, callbackUrl }, remotePath };
}

/**
 * Called once the editor window closes (Done, or the window's own X) --
 * pulls back whatever OnlyOffice's save callback last wrote to the temp
 * remote path (it writes there directly, server-side, with no action needed
 * from Caroline in between) and overwrites the original local file, then
 * best-effort deletes the temp copy. A file that was opened view-only
 * (editable: false, e.g. pdf) never got a callbackUrl and can't have
 * changed, but re-downloading it anyway is harmless and keeps this one
 * code path simple.
 */
// Legacy quirk confirmed live against production: cmdReadFile (FileCommands.py)
// doesn't return a clean {".status":"ok", content:"<base64>"} -- it stuffs the
// path and base64 content INTO the ".status" string itself, delimited by
// "=====" markers: "ok\n=====\n<path>\n=====\n<base64>\n======\n". Not a bug
// to fix here (other callers may already depend on this exact shape) -- just
// something this code has to parse instead of the sane shape you'd expect.
const READ_STATUS_RE = /^ok\n=====\n([\s\S]*?)\n=====\n([\s\S]*?)\n======\n$/;

export async function finishOfficeEditSession(remotePath: string, localPath: string): Promise<void> {
  console.error(`[caroline] [officeEditor] finishOfficeEditSession: remotePath=${remotePath} localPath=${localPath}`);
  const session = await getSession();
  const readRes = await callApi({ ".command": "read", key: SQUIRRELWISDOM_APP_KEY, session, path: remotePath });
  const match = typeof readRes?.[".status"] === "string" ? READ_STATUS_RE.exec(readRes[".status"]) : null;
  if (match) {
    writeFileSync(localPath, Buffer.from(match[2], "base64"));
    console.error(`[caroline] [officeEditor] finishOfficeEditSession: wrote back edits to ${localPath}`);
  } else {
    console.error(`[caroline] [officeEditor] finishOfficeEditSession: no parseable content in read response for ${remotePath}, local file left untouched`);
  }
  await callApi({ ".command": "delete", key: SQUIRRELWISDOM_APP_KEY, session, path: remotePath })
    .catch((err) => console.error(`[caroline] [officeEditor] finishOfficeEditSession: failed to delete temp remote copy ${remotePath} (ignored):`, err));
}
