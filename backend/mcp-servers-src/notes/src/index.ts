import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { ensureSession, login, withSession } from "./session.js";
import {
  createFolder,
  createNote,
  deleteFolder,
  deleteNote,
  getNote,
  listFolders,
  listNotes,
  moveNote,
  renameFolder,
  searchNotes,
  titleOf,
  updateNote,
  type NoteEntry,
} from "./notes.js";
import { attachFile, attachmentUrl, downloadAttachment, listAttachments, removeAttachment } from "./attachments.js";

const server = new McpServer({ name: "notes", version: "1.0.0" });

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function jsonResult(data: unknown) {
  return textResult(JSON.stringify(data, null, 2));
}

function summarize(entry: NoteEntry) {
  return {
    id: entry.id,
    title: titleOf(entry.text),
    folder: entry.folder ?? "",
    updatedAt: entry.updatedAt,
    updatedAtIso: new Date(entry.updatedAt).toISOString(),
    deleted: !!entry.deleted,
  };
}

const folderMemoryNote =
  ' For Claude\'s own long-term memory (not asked for by the user), use folder "Claude Memory" ' +
  "unless the user directs otherwise. This tool works with any note/folder the user names, too.";

server.registerTool(
  "notes_login",
  {
    title: "Log in to Squirrel Wisdom",
    description:
      "One-time login with the user's Squirrel Wisdom email/password. Verifies the credentials, " +
      "saves them locally (~/.mcp-notes/credentials.json) so the server can silently re-login on " +
      "every future start (sessions expire after 24h idle), and establishes the account for this " +
      "process. Call again to switch accounts.",
    inputSchema: {
      email: z.string().email().describe("Squirrel Wisdom account email."),
      password: z.string().describe("Squirrel Wisdom account password."),
    },
  },
  async ({ email, password }) => {
    const { hash16 } = await login(email, password);
    return textResult(`Logged in as ${email} (hash16=${hash16}). Credentials saved for automatic re-login.`);
  }
);

server.registerTool(
  "notes_whoami",
  {
    title: "Show current notes account",
    description: "Ensures a session is established (auto-login from saved credentials if needed) and reports which account is active.",
    inputSchema: {},
  },
  async () => {
    const { email, hash16 } = await ensureSession();
    return jsonResult({ email, hash16 });
  }
);

server.registerTool(
  "notes_list",
  {
    title: "List notes",
    description: "Lists notes, optionally scoped to an exact folder path (\"/\"-separated; omit for all folders).",
    inputSchema: {
      folder: z.string().optional().describe('Exact folder path, e.g. "Work/Projects". Omit to list notes in every folder.'),
      includeDeleted: z.boolean().optional().describe("Include soft-deleted notes. Defaults to false."),
    },
  },
  async ({ folder, includeDeleted }) => {
    const entries = await withSession(({ session }) => listNotes(session, { folder, includeDeleted }));
    return jsonResult(entries.map(summarize));
  }
);

server.registerTool(
  "notes_search",
  {
    title: "Search notes",
    description: "Case-insensitive substring search over note text (title + body), optionally scoped to a folder and its subfolders.",
    inputSchema: {
      query: z.string().describe("Substring to search for in note text."),
      folder: z.string().optional().describe('Restrict search to this folder and its subfolders, e.g. "Claude Memory".'),
      includeDeleted: z.boolean().optional().describe("Include soft-deleted notes. Defaults to false."),
    },
  },
  async ({ query, folder, includeDeleted }) => {
    const entries = await withSession(({ session }) => searchNotes(session, query, { folder, includeDeleted }));
    return jsonResult(entries.map(summarize));
  }
);

server.registerTool(
  "notes_get",
  {
    title: "Get a note",
    description: "Fetches a single note's full text and metadata by id.",
    inputSchema: { id: z.string().describe("Note id.") },
  },
  async ({ id }) => {
    const note = await withSession(({ session }) => getNote(session, id));
    return jsonResult({ id: note.id, text: note.text, folder: note.folder ?? "", updatedAt: note.updatedAt, deleted: !!note.deleted });
  }
);

server.registerTool(
  "notes_create",
  {
    title: "Create a note",
    description: "Creates a new note. The first line of `text` is treated as the title." + folderMemoryNote,
    inputSchema: {
      text: z.string().describe("Note content; first line is the title, rest is the body."),
      folder: z.string().optional().describe('Folder path, e.g. "Claude Memory". Omit for the root.'),
    },
  },
  async ({ text, folder }) => {
    const note = await withSession(({ session }) => createNote(session, text, folder));
    return jsonResult(summarize(note));
  }
);

server.registerTool(
  "notes_update",
  {
    title: "Update a note",
    description: "Updates a note's text and/or folder. Only provided fields are changed.",
    inputSchema: {
      id: z.string().describe("Note id."),
      text: z.string().optional().describe("New text. Omit to leave unchanged."),
      folder: z.string().optional().describe("New folder path. Omit to leave unchanged."),
    },
  },
  async ({ id, text, folder }) => {
    const note = await withSession(({ session }) => updateNote(session, id, { text, folder }));
    return jsonResult(summarize(note));
  }
);

server.registerTool(
  "notes_delete",
  {
    title: "Delete a note",
    description: "Soft-deletes a note (marks it deleted; it is never physically removed).",
    inputSchema: { id: z.string().describe("Note id.") },
  },
  async ({ id }) => {
    const note = await withSession(({ session }) => deleteNote(session, id));
    return jsonResult(summarize(note));
  }
);

server.registerTool(
  "notes_move",
  {
    title: "Move a note",
    description: 'Moves a note to a different folder (use folder: "" to move it to the root).',
    inputSchema: {
      id: z.string().describe("Note id."),
      folder: z.string().describe('Destination folder path, e.g. "Work/Archive", or "" for the root.'),
    },
  },
  async ({ id, folder }) => {
    const note = await withSession(({ session }) => moveNote(session, id, folder));
    return jsonResult(summarize(note));
  }
);

server.registerTool(
  "notes_list_folders",
  {
    title: "List folders",
    description: "Lists every folder path that currently has at least one active note (including ancestor folders implied by nested paths).",
    inputSchema: {},
  },
  async () => {
    const folders = await withSession(({ session }) => listFolders(session));
    return jsonResult(folders);
  }
);

server.registerTool(
  "notes_create_folder",
  {
    title: "Create an empty folder",
    description: "Creates an empty folder by writing a hidden marker note. Not needed if you're about to create a real note in that folder anyway.",
    inputSchema: { path: z.string().describe('Folder path to create, e.g. "Claude Memory/Projects".') },
  },
  async ({ path }) => {
    await withSession(({ session }) => createFolder(session, path));
    return textResult(`Created folder "${path}".`);
  }
);

server.registerTool(
  "notes_rename_folder",
  {
    title: "Rename a folder",
    description: "Renames a folder and moves every note inside it (recursively) to the new path.",
    inputSchema: {
      oldPath: z.string().describe('Existing folder path, e.g. "Work/Old".'),
      newPath: z.string().describe('New folder path, e.g. "Work/New".'),
    },
  },
  async ({ oldPath, newPath }) => {
    const count = await withSession(({ session }) => renameFolder(session, oldPath, newPath));
    return textResult(`Renamed "${oldPath}" to "${newPath}" (${count} note(s) moved).`);
  }
);

server.registerTool(
  "notes_delete_folder",
  {
    title: "Delete a folder",
    description: "Soft-deletes every note inside a folder, recursively (the folder itself has no separate storage to delete).",
    inputSchema: { path: z.string().describe('Folder path to delete, e.g. "Work/Old".') },
  },
  async ({ path }) => {
    const count = await withSession(({ session }) => deleteFolder(session, path));
    return textResult(`Deleted folder "${path}" (${count} note(s) soft-deleted).`);
  }
);

server.registerTool(
  "notes_attach",
  {
    title: "Attach a file to a note",
    description: "Uploads a local file and attaches it to a note. Effective size limit ~47MB. Returns a public URL for the uploaded file.",
    inputSchema: {
      noteId: z.string().describe("Note id to attach the file to."),
      filePath: z.string().describe("Absolute local file path to upload."),
      originalName: z.string().optional().describe("Display filename. Defaults to the local file's basename."),
    },
  },
  async ({ noteId, filePath, originalName }) => {
    let hash16 = "";
    const entry = await withSession((ctx) => {
      hash16 = ctx.hash16;
      return attachFile(ctx.session, noteId, filePath, originalName);
    });
    return jsonResult({ ...entry, url: attachmentUrl(hash16, entry.filename) });
  }
);

server.registerTool(
  "notes_list_attachments",
  {
    title: "List attachments",
    description: "Lists attachments, optionally filtered to a single note.",
    inputSchema: { noteId: z.string().optional().describe("Restrict to attachments on this note. Omit to list every attachment in the account.") },
  },
  async ({ noteId }) => {
    let hash16 = "";
    const entries = await withSession((ctx) => {
      hash16 = ctx.hash16;
      return listAttachments(ctx.session, noteId);
    });
    return jsonResult(entries.map((e) => ({ ...e, url: attachmentUrl(hash16, e.filename) })));
  }
);

server.registerTool(
  "notes_download_attachment",
  {
    title: "Download an attachment",
    description:
      "Downloads an attachment's raw bytes to a local file path. The attachment URL is also " +
      "publicly fetchable directly (no auth needed), but this saves it to disk in one step.",
    inputSchema: {
      filename: z.string().describe("The attachment's stored filename (from notes_attach/notes_list_attachments, not the original display name)."),
      savePath: z.string().describe("Absolute local file path to write the downloaded bytes to."),
    },
  },
  async ({ filename, savePath }) => {
    const { hash16 } = await ensureSession();
    const bytes = await downloadAttachment(hash16, filename, savePath);
    return textResult(`Downloaded ${bytes} byte(s) to "${savePath}".`);
  }
);

server.registerTool(
  "notes_remove_attachment",
  {
    title: "Remove an attachment",
    description: "Removes an attachment from a note — drops it from the attachment list and deletes the uploaded file itself (the backend does both in one step; there's no way to detach without deleting).",
    inputSchema: {
      noteId: z.string().describe("Note id the attachment belongs to."),
      filename: z.string().describe("The attachment's stored filename (from notes_attach/notes_list_attachments, not the original display name)."),
    },
  },
  async ({ noteId, filename }) => {
    await withSession(({ session }) => removeAttachment(session, noteId, filename));
    return textResult(`Removed attachment "${filename}" from note "${noteId}" and deleted the file.`);
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
