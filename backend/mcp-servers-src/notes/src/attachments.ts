import { readFile, stat, writeFile } from "node:fs/promises";
import { basename } from "node:path";
import { MAX_ATTACHMENT_BYTES, callPlugin, genAttachmentFilename, toBase64 } from "./api.js";

export interface AttachmentEntry {
  filename: string;
  originalName: string;
  uploaded: string;
  noteId: string;
}

export async function attachFile(
  session: string,
  noteId: string,
  localFilePath: string,
  originalName?: string
): Promise<AttachmentEntry> {
  const stats = await stat(localFilePath);
  if (stats.size > MAX_ATTACHMENT_BYTES) {
    throw new Error(
      `File is ${(stats.size / 1024 / 1024).toFixed(1)}MB, which exceeds the ~${(
        MAX_ATTACHMENT_BYTES /
        1024 /
        1024
      ).toFixed(0)}MB effective attachment limit (base64 inflation over the 64MB request-body cap).`
    );
  }

  const bytes = await readFile(localFilePath);
  const filename = genAttachmentFilename();
  const entry: AttachmentEntry = {
    filename,
    originalName: originalName ?? basename(localFilePath),
    uploaded: new Date().toISOString(),
    noteId,
  };

  // Single call — writes the raw bytes and the meta entry atomically server-side.
  await callPlugin("saveAttachment", session, {
    filename,
    content: toBase64(bytes),
    originalName: entry.originalName,
    noteId,
  });

  return entry;
}

export async function listAttachments(session: string, noteId?: string): Promise<AttachmentEntry[]> {
  const result = await callPlugin("listAttachments", session);
  const attachments: AttachmentEntry[] = result?.attachments ?? [];
  return noteId ? attachments.filter((a) => a.noteId === noteId) : attachments;
}

// The backend's removeAttachment always drops both the meta entry and the underlying blob
// in one call — there's no supported "detach but keep the file" action anymore.
export async function removeAttachment(session: string, noteId: string, filename: string): Promise<void> {
  const existing = await listAttachments(session, noteId);
  if (!existing.some((a) => a.filename === filename)) {
    throw new Error(`Attachment "${filename}" on note "${noteId}" not found.`);
  }
  await callPlugin("removeAttachment", session, { filename });
}

// Confirmed live (2026-09-03): the plain "files/{hash16}/{filename}" static path this used
// to hit was removed from the server intentionally (hash16 = sha256(login).slice(0,16) is
// NOT a secret, so serving attachments from it let anyone with just a victim's email read/
// overwrite their files via a bare curl -- see reforce's apps/Notes/main.py header comment).
// The server hasn't answered that route at all since; every request to it 404s, on both old
// and freshly-uploaded attachments alike. readAttachment (via the same authenticated
// plugins:call envelope every other Notes action already goes through) is the only way to
// fetch an attachment's bytes now -- see portal/NOTES_API.md's "Read an attachment's bytes"
// section, which says exactly that: "There is no plain URL for attachment content." Do NOT
// reintroduce a static/URL-based path for this -- that's the vulnerability this closed.
export async function downloadAttachment(session: string, filename: string, savePath: string): Promise<number> {
  const result = await callPlugin("readAttachment", session, { filename });
  const contentB64 = result?.content;
  if (typeof contentB64 !== "string") {
    throw new Error(`Attachment "${filename}" not found (readAttachment returned no content).`);
  }
  const bytes = Buffer.from(contentB64, "base64");
  await writeFile(savePath, bytes);
  return bytes.length;
}
