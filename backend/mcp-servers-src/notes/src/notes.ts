import { callPlugin, genNoteId } from "./api.js";

export interface NoteObject {
  text: string;
  updatedAt: number;
  deleted?: boolean;
  folder?: string;
  isFolderMarker?: boolean;
}

export interface NoteEntry extends NoteObject {
  id: string;
}

type NoteIndex = Record<string, NoteObject>;

async function readIndex(session: string): Promise<NoteIndex> {
  const result = await callPlugin("readIndex", session);
  return result?.notes ?? {};
}

async function readNoteFile(session: string, id: string): Promise<NoteObject | null> {
  const result = await callPlugin("getNote", session, { id });
  return result?.note ?? null;
}

async function writeNoteFile(session: string, id: string, note: NoteObject): Promise<void> {
  await callPlugin("writeNoteFile", session, { id, note });
}

// Merges just the given {id: note} pairs into index.json server-side — safer than
// read-modify-write, since it can't race a concurrent writer's own patchIndex call.
async function patchIndex(session: string, entries: NoteIndex): Promise<void> {
  await callPlugin("patchIndex", session, { entries });
}

function titleOf(text: string): string {
  return text.split("\n", 1)[0] ?? "";
}

function inFolderScope(folder: string | undefined, scope: string): boolean {
  const f = folder ?? "";
  return f === scope || f.startsWith(scope + "/");
}

export async function listNotes(
  session: string,
  opts: { folder?: string; includeDeleted?: boolean } = {}
): Promise<NoteEntry[]> {
  const index = await readIndex(session);
  const entries = Object.entries(index)
    .filter(([, n]) => opts.includeDeleted || !n.deleted)
    .filter(([, n]) => opts.folder === undefined || (n.folder ?? "") === opts.folder)
    .filter(([, n]) => !n.isFolderMarker)
    .map(([id, n]) => ({ id, ...n }));
  entries.sort((a, b) => b.updatedAt - a.updatedAt);
  return entries;
}

export async function searchNotes(
  session: string,
  query: string,
  opts: { folder?: string; includeDeleted?: boolean } = {}
): Promise<NoteEntry[]> {
  const q = query.toLowerCase();
  const index = await readIndex(session);
  const entries = Object.entries(index)
    .filter(([, n]) => opts.includeDeleted || !n.deleted)
    .filter(([, n]) => !n.isFolderMarker)
    .filter(([, n]) => opts.folder === undefined || inFolderScope(n.folder, opts.folder))
    .filter(([, n]) => n.text.toLowerCase().includes(q))
    .map(([id, n]) => ({ id, ...n }));
  entries.sort((a, b) => b.updatedAt - a.updatedAt);
  return entries;
}

export async function getNote(session: string, id: string): Promise<NoteEntry> {
  const note = await readNoteFile(session, id);
  if (!note) throw new Error(`Note "${id}" not found.`);
  return { id, ...note };
}

// Always in this order — the individual file first, then the index entry — so a crash
// mid-operation leaves the individual file (the source of truth) consistent.
async function saveNote(session: string, id: string, note: NoteObject): Promise<NoteEntry> {
  await writeNoteFile(session, id, note);
  await patchIndex(session, { [id]: note });
  return { id, ...note };
}

export async function createNote(
  session: string,
  text: string,
  folder?: string,
  isFolderMarker = false
): Promise<NoteEntry> {
  const id = genNoteId();
  const note: NoteObject = { text, updatedAt: Date.now(), deleted: false, folder: folder ?? "", isFolderMarker };
  return saveNote(session, id, note);
}

async function patchNote(session: string, id: string, patch: Partial<NoteObject>): Promise<NoteEntry> {
  const current = await readNoteFile(session, id);
  if (!current) throw new Error(`Note "${id}" not found.`);
  const definedPatch = Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined));
  const note: NoteObject = { ...current, ...definedPatch, updatedAt: Date.now() };
  return saveNote(session, id, note);
}

export async function updateNote(
  session: string,
  id: string,
  patch: { text?: string; folder?: string }
): Promise<NoteEntry> {
  return patchNote(session, id, patch);
}

export async function deleteNote(session: string, id: string): Promise<NoteEntry> {
  return patchNote(session, id, { deleted: true });
}

export async function moveNote(session: string, id: string, folder: string): Promise<NoteEntry> {
  return patchNote(session, id, { folder });
}

export async function listFolders(session: string): Promise<string[]> {
  const index = await readIndex(session);
  const folders = new Set<string>();
  for (const note of Object.values(index)) {
    if (note.deleted) continue;
    const folder = note.folder ?? "";
    if (!folder) continue;
    const segments = folder.split("/");
    for (let i = 1; i <= segments.length; i++) {
      folders.add(segments.slice(0, i).join("/"));
    }
  }
  return Array.from(folders).sort();
}

export async function createFolder(session: string, path: string): Promise<void> {
  await createNote(session, "", path, true);
}

// Rewrites every affected note individually, then merges them all into the index with a
// single patchIndex call — matches the API doc's guidance for batch folder rename/delete.
async function batchMoveFolder(
  session: string,
  matches: (path: string) => boolean,
  applyPatch: (note: NoteObject) => Partial<NoteObject>
): Promise<number> {
  const index = await readIndex(session);
  const affected = Object.entries(index).filter(([, n]) => !n.deleted && matches(n.folder ?? ""));

  const changed: NoteIndex = {};
  for (const [id, note] of affected) {
    const patched: NoteObject = { ...note, ...applyPatch(note), updatedAt: Date.now() };
    await writeNoteFile(session, id, patched);
    changed[id] = patched;
  }

  if (affected.length > 0) {
    await patchIndex(session, changed);
  }

  return affected.length;
}

export async function renameFolder(session: string, oldPath: string, newPath: string): Promise<number> {
  return batchMoveFolder(
    session,
    (folder) => folder === oldPath || folder.startsWith(oldPath + "/"),
    (note) => ({ folder: newPath + (note.folder ?? "").slice(oldPath.length) })
  );
}

export async function deleteFolder(session: string, path: string): Promise<number> {
  return batchMoveFolder(
    session,
    (folder) => folder === path || folder.startsWith(path + "/"),
    () => ({ deleted: true })
  );
}

export { titleOf };
