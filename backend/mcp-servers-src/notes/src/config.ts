import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const CONFIG_DIR = join(homedir(), ".mcp-notes");
const CREDENTIALS_PATH = join(CONFIG_DIR, "credentials.json");

export interface Credentials {
  email: string;
  password: string;
}

export async function loadCredentials(): Promise<Credentials | null> {
  try {
    const raw = await readFile(CREDENTIALS_PATH, "utf8");
    return JSON.parse(raw) as Credentials;
  } catch (err: any) {
    if (err?.code === "ENOENT") return null;
    throw err;
  }
}

export async function saveCredentials(creds: Credentials): Promise<void> {
  await mkdir(dirname(CREDENTIALS_PATH), { recursive: true });
  await writeFile(CREDENTIALS_PATH, JSON.stringify(creds, null, 2), "utf8");
}

export { CREDENTIALS_PATH };
