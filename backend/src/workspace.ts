import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import os from "node:os";
import { mcpAdd, mcpAddHttp, mcpGet, mcpRemove } from "./control.js";
import { launchSharedMcpServers } from "./sharedMcpServers.js";
import { launchLocalTtsServer } from "./localTtsServer.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
// backend/dist/workspace.js -> backend/  (mcp-servers/ is a sibling of dist/,
// shipped together as one unit wherever Caroline is installed)
const BACKEND_ROOT = join(__dirname, "..");
const BUNDLED_SERVERS_DIR = join(BACKEND_ROOT, "mcp-servers");
const SKILLS_SRC_DIR = join(BACKEND_ROOT, "skills-src");
const PYTHON_SCRIPTS_DIR = join(BACKEND_ROOT, "python-scripts");

/**
 * Copies Caroline's built-in skills (skills-src/, shipped with this backend
 * -- see build.bat) into the workspace's Skills/ folder, overwriting each
 * one every start. These are code-managed defaults (see policies.ts's split
 * between hard systemPrompt rules and skill-based procedural knowledge), so
 * a code update to an existing skill's content should actually take effect
 * on next start, the same way any other code change does -- this is NOT
 * gated behind the one-time .caroline-seeded marker below. Only touches
 * directories that exist in skills-src/ by name: a custom skill the user or
 * Caroline added directly under Skills/ under a different name is never
 * touched, so the catalog stays genuinely appendable, not just overwritable.
 */
function seedSkills(workspaceDir: string): void {
  if (!existsSync(SKILLS_SRC_DIR)) return; // dev run without a packaged backend
  const skillsDir = join(workspaceDir, "Skills");
  for (const entry of readdirSync(SKILLS_SRC_DIR, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    try {
      cpSync(join(SKILLS_SRC_DIR, entry.name), join(skillsDir, entry.name), { recursive: true, force: true });
    } catch (err) {
      console.error(`[caroline] failed to seed skill "${entry.name}":`, err);
    }
  }
}

const COPIED_NOT_BUNDLED = new Set(["browser", "email"]);

function bundled(name: string): string {
  // Most servers are a single esbuild-bundled file; a couple (see
  // bundle-mcp-servers.mjs for why) are still a plain copy of
  // dist/+node_modules and keep the old dist/index.js layout.
  if (COPIED_NOT_BUNDLED.has(name)) return join(BUNDLED_SERVERS_DIR, name, "dist", "index.js");
  return join(BUNDLED_SERVERS_DIR, name, "index.mjs");
}

interface DefaultServer {
  name: string;
  args: string[];
}

/**
 * The utility servers below (mouse/keyboard/notes/time/etc.) are launched
 * ONCE per backend process (see launchSharedMcpServers) and registered as
 * HTTP servers instead of spawned stdio commands -- per explicit instruction
 * (2026-09-06): stdio is strictly 1:1 (one parent, one child), so every one
 * of Caroline's tabs used to spawn its OWN copy of every single one of
 * these, confirmed live as the actual cause of a 240+ node.exe process
 * swarm accumulating over a day of restarts. One shared instance per port
 * fixes that regardless of how many tabs are open.
 */
interface SharedUtilityServer {
  name: string;
  port: number;
  bundledName: string;
}

/**
 * The default MCP server roster Caroline ships with, built from this dev
 * repo's MCP/* sources at package time (see scripts/bundle-mcp-servers.mjs)
 * but referenced here only via paths under Caroline's own install directory
 * -- nothing here points back at the source repo at runtime.
 *
 * "caroline-" prefixed so these can never collide with a same-named server
 * the target machine might already have registered elsewhere -- discovered
 * the hard way: this dev machine already has a user-scope "time"/"email"/
 * "windows-*" roster pointing at D:/REPO/silmarillion/MCP/*, and same-named
 * servers would otherwise get silently confused with Caroline's own.
 *
 * Registered at **user scope** (`claude mcp add --scope user`), not written
 * into the workspace's own .mcp.json: project-scoped .mcp.json servers sit
 * at "Pending approval" until a human runs an interactive `claude` session
 * to trust them, which a headless SDK session can never do (confirmed via
 * `claude mcp get` -- `settings.enableAllProjectMcpServers` does not clear
 * this either). User-scope servers connect with no such gate.
 */
const SHARED_UTILITY_SERVERS: SharedUtilityServer[] = [
  { name: "caroline-notes", port: 9401, bundledName: "notes" },
  { name: "caroline-sms", port: 9411, bundledName: "sms" },
  { name: "caroline-time", port: 9402, bundledName: "time" },
  { name: "caroline-windows-screenshot", port: 9403, bundledName: "screenshot" },
  { name: "caroline-windows-mouse", port: 9404, bundledName: "mouse" },
  { name: "caroline-windows-keyboard", port: 9405, bundledName: "keyboard" },
  { name: "caroline-windows-inspect", port: 9406, bundledName: "inspect" },
  { name: "caroline-windows-window-screenshot", port: 9407, bundledName: "window-screenshot" },
  { name: "caroline-windows-window-mouse", port: 9408, bundledName: "window-mouse" },
  { name: "caroline-windows-window-keyboard", port: 9409, bundledName: "window-keyboard" },
  { name: "caroline-windows-chain", port: 9410, bundledName: "chain" },
  { name: "caroline-voice", port: 9412, bundledName: "voice" },
  { name: "caroline-screen-video", port: 9413, bundledName: "screen-video" },
];

function sharedUtilityUrl(server: SharedUtilityServer): string {
  return `http://127.0.0.1:${server.port}/mcp`;
}

// caroline-email is NOT here -- it's an in-process SDK tool now (see
// server.ts's mcpServers / src/email/index.ts), not a spawned stdio server.
// removeStaleInProcessServers() below cleans up an old installation's stdio
// registration of it. caroline-browser stays a per-tab stdio server, NOT
// shared like the utility servers above -- each tab genuinely needs its own
// browser profile/CDP session, unlike a shared cursor/keyboard/notes client.
function defaultServers(): DefaultServer[] {
  const browserArgs = (label: string, port: number) => [
    bundled("browser"),
    "--port", String(port),
    "--user-data-dir", join(os.homedir(), `.caroline-browser-profile-${label}`),
    "--label", `browser-${label}`,
  ];

  return [
    { name: "caroline-browser", args: browserArgs("main", 9822) },
  ];
}

/** Reads the seeding marker's recorded set of server names already handled at least
 *  once, or null if there's nothing usable yet (no marker at all, or an old-format
 *  marker from before per-name tracking existed -- see ensureWorkspace's own doc
 *  comment for what happens then). */
function loadSeededNames(seededMarker: string): string[] | null {
  if (!existsSync(seededMarker)) return null;
  try {
    const parsed = JSON.parse(readFileSync(seededMarker, "utf-8"));
    return Array.isArray(parsed?.seededNames) ? parsed.seededNames : null;
  } catch (err) {
    console.error(`[caroline] loadSeededNames: ${seededMarker} isn't the new JSON format yet (treating as needing migration):`, err);
    return null;
  }
}

function saveSeededNames(seededMarker: string, seededNames: string[]): void {
  writeFileSync(seededMarker, JSON.stringify({ seededAt: new Date().toISOString(), seededNames }, null, 2) + "\n", "utf-8");
}

/**
 * Resolves Caroline's per-user workspace directory (CLAUDE.md / Skills/
 * live here), creating it and registering the default MCP servers (user
 * scope, see defaultServers()/SHARED_UTILITY_SERVERS above).
 *
 * Per explicit instruction (2026-09-07): defaultServers()/SHARED_UTILITY_SERVERS
 * grow over time as new tools ship (this same update added two: caroline-voice,
 * caroline-sms) -- every one of them needs to reach EVERY existing install
 * automatically on its next start, not just fresh ones, since there's no "do
 * this by hand on each user's machine" option at any real scale. The marker
 * file records exactly which names have been handled at least once (registered
 * OR deliberately left alone because the user removed them via Settings) --
 * each start just diffs the cheap in-memory name list against that (no `claude
 * mcp` subprocess spawns at all unless something actually changed), and only
 * registers whichever names are genuinely new. A name already in the marker is
 * NEVER touched again regardless of its current registration state, so a
 * removal the user made stays removed forever, exactly like before.
 *
 * The one-time migration from the OLD marker format (a bare timestamp, no
 * per-name list) pays for one real `claude mcp get` check per current entry --
 * whatever's already registered gets recorded as "already known" (left alone
 * from now on); whatever's missing gets registered right now, exactly as if it
 * were newly added today. This is the only point where a name the user
 * previously removed (if `claude mcp get` finds it genuinely gone) could
 * theoretically get silently re-added -- a one-time, low-stakes edge case
 * (these are all built-in utility tools, easily removed again) far outweighed
 * by permanently fixing the "new server never reaches existing installs"
 * problem this replaces.
 */
export async function ensureWorkspace(): Promise<string> {
  const workspaceDir = join(os.homedir(), "AppData", "Local", "Caroline", "workspace");
  const skillsDir = join(workspaceDir, "Skills");
  const seededMarker = join(workspaceDir, ".caroline-seeded");

  if (!existsSync(workspaceDir)) mkdirSync(workspaceDir, { recursive: true });
  if (!existsSync(skillsDir)) mkdirSync(skillsDir, { recursive: true });
  seedSkills(workspaceDir);

  const existingNames = loadSeededNames(seededMarker);
  if (existingNames === null) {
    const isTrulyFirstRun = !existsSync(seededMarker);
    console.log(isTrulyFirstRun
      ? "[caroline] first run: registering default MCP servers (user scope)..."
      : "[caroline] migrating MCP-server seeding marker to per-name tracking (one-time registration check)...");
    const seededNames: string[] = [];
    for (const server of defaultServers()) {
      const isKnownRegistered = isTrulyFirstRun ? false : (await mcpGet(workspaceDir, server.name)).code === 0;
      if (!isKnownRegistered) {
        const r = await mcpAdd(workspaceDir, server.name, process.execPath, server.args, "user");
        if (r.code !== 0) console.error(`[caroline] failed to register ${server.name}:`, r.stderr || r.stdout);
      }
      seededNames.push(server.name);
    }
    for (const server of SHARED_UTILITY_SERVERS) {
      const isKnownRegistered = isTrulyFirstRun ? false : (await mcpGet(workspaceDir, server.name)).code === 0;
      if (!isKnownRegistered) {
        const r = await mcpAddHttp(workspaceDir, server.name, sharedUtilityUrl(server), "user");
        if (r.code !== 0) console.error(`[caroline] failed to register ${server.name}:`, r.stderr || r.stdout);
      }
      seededNames.push(server.name);
    }
    saveSeededNames(seededMarker, seededNames);
  } else {
    const knownNames = new Set(existingNames);
    const newDefaultServers = defaultServers().filter((s) => !knownNames.has(s.name));
    const newSharedServers = SHARED_UTILITY_SERVERS.filter((s) => !knownNames.has(s.name));
    if (newDefaultServers.length > 0 || newSharedServers.length > 0) {
      const names = [...newDefaultServers, ...newSharedServers].map((s) => s.name).join(", ");
      console.log(`[caroline] registering ${newDefaultServers.length + newSharedServers.length} newly-added default MCP server(s): ${names}`);
      for (const server of newDefaultServers) {
        const r = await mcpAdd(workspaceDir, server.name, process.execPath, server.args, "user");
        if (r.code !== 0) console.error(`[caroline] failed to register newly-added ${server.name}:`, r.stderr || r.stdout);
      }
      for (const server of newSharedServers) {
        const r = await mcpAddHttp(workspaceDir, server.name, sharedUtilityUrl(server), "user");
        if (r.code !== 0) console.error(`[caroline] failed to register newly-added ${server.name}:`, r.stderr || r.stdout);
      }
      saveSeededNames(seededMarker, [...existingNames, ...newDefaultServers.map((s) => s.name), ...newSharedServers.map((s) => s.name)]);
    }
  }

  await healStaleServerPaths(workspaceDir);
  await removeStaleInProcessServers(workspaceDir);
  await migrateUtilityServersToShared(workspaceDir);
  launchSharedMcpServers(
    SHARED_UTILITY_SERVERS.map((s) => ({ name: s.name, port: s.port, scriptPath: bundled(s.bundledName) })),
  );

  // CAROLINE_PYTHON_PATH is set by BackendProcess.cs (WPF shell) on every spawn, same
  // convention as CAROLINE_FFMPEG_PATH/CAROLINE_MODELS_DIR -- falls back to a bare
  // "python" PATH lookup for dev runs / an install predating this env var.
  launchLocalTtsServer(
    process.env.CAROLINE_PYTHON_PATH || "python",
    join(PYTHON_SCRIPTS_DIR, "local_tts_server.py"),
  );

  return workspaceDir;
}

/**
 * Servers that used to be spawned stdio processes (registered here, at user
 * scope) but have since moved in-process (see server.ts's mcpServers) --
 * an old install's leftover registration would otherwise sit alongside the
 * in-process one and register the same tool names twice.
 *
 * Guarded by a marker (like defaultServers()'s own .caroline-seeded) once
 * confirmed clean, rather than re-checking every single startup forever --
 * each check is a full `claude mcp get`/`remove` subprocess spawn of the
 * bundled ~200MB claude.exe, and this list only ever grows, so left
 * unguarded it becomes permanent multi-second-per-server startup cost for a
 * migration that only ever needs to happen once. The marker is written only
 * after every name here is confirmed either absent or successfully removed
 * -- if a removal fails, it's withheld so the next startup retries instead
 * of silently leaving a stale registration in place forever.
 */
const FORMER_STDIO_SERVERS = ["caroline-email"];

async function removeStaleInProcessServers(workspaceDir: string): Promise<void> {
  const marker = join(workspaceDir, ".caroline-inprocess-migrated");
  if (existsSync(marker)) return;

  let allClean = true;
  for (const name of FORMER_STDIO_SERVERS) {
    const got = await mcpGet(workspaceDir, name);
    if (got.code !== 0) continue; // not registered -- nothing to clean up for this one
    console.log(`[caroline] ${name} is now in-process -- removing its old stdio registration`);
    const r = await mcpRemove(workspaceDir, name);
    if (r.code !== 0) {
      console.error(`[caroline] failed to remove stale ${name} registration:`, r.stderr || r.stdout);
      allClean = false;
    }
  }
  if (allClean) writeFileSync(marker, new Date().toISOString(), "utf-8");
}

/**
 * An existing install (from before 2026-09-06) has each of
 * SHARED_UTILITY_SERVERS registered the OLD way -- a spawned stdio command,
 * one independent copy per tab. Removes that registration and re-adds it as
 * the shared HTTP one instead -- see SHARED_UTILITY_SERVERS' own doc
 * comment for why. Same once-only marker pattern as
 * removeStaleInProcessServers, and for the same reason (each check is a
 * multi-second claude.exe subprocess spawn).
 */
async function migrateUtilityServersToShared(workspaceDir: string): Promise<void> {
  const marker = join(workspaceDir, ".caroline-shared-mcp-migrated");
  if (existsSync(marker)) return;

  let allClean = true;
  for (const server of SHARED_UTILITY_SERVERS) {
    const got = await mcpGet(workspaceDir, server.name);
    if (got.code !== 0) continue; // not registered -- never seeded, or the user removed it; leave it alone
    if (got.stdout.includes(sharedUtilityUrl(server))) continue; // already migrated
    console.log(`[caroline] ${server.name} is still registered as a spawned stdio server -- migrating to the shared HTTP one`);
    const removed = await mcpRemove(workspaceDir, server.name);
    if (removed.code !== 0) {
      console.error(`[caroline] failed to remove old ${server.name} registration:`, removed.stderr || removed.stdout);
      allClean = false;
      continue;
    }
    const added = await mcpAddHttp(workspaceDir, server.name, sharedUtilityUrl(server), "user");
    if (added.code !== 0) {
      console.error(`[caroline] failed to re-register ${server.name} as HTTP:`, added.stderr || added.stdout);
      allClean = false;
    }
  }
  if (allClean) writeFileSync(marker, new Date().toISOString(), "utf-8");
}

/**
 * Every default server's registered command is an absolute path into this
 * install's own mcp-servers/ directory -- if the app's packaging layout
 * changes between updates (e.g. a server switching from a copied dist/
 * folder to a single bundled file), an already-registered server keeps
 * pointing at wherever the OLD layout put it, which no longer exists post-
 * update. Confirmed the hard way: switching most servers to esbuild
 * bundling left every previously-installed user with "Connection closed"
 * on all of them, since the path node was told to launch had vanished.
 *
 * Only touches servers that are (a) still registered under the exact
 * command this install would generate and (b) whose target file is
 * missing -- a server the user genuinely removed via Settings just isn't
 * registered at all, so mcpGet fails and this leaves it alone.
 */
async function healStaleServerPaths(workspaceDir: string): Promise<void> {
  for (const server of defaultServers()) {
    const got = await mcpGet(workspaceDir, server.name);
    if (got.code !== 0) continue; // not registered -- never seeded, or the user removed it; leave it alone
    const argsLine = got.stdout.split("\n").find((l) => l.trim().startsWith("Args:"));
    const registeredPath = argsLine?.replace(/^\s*Args:\s*/, "").trim().split(/\s+/)[0];
    if (registeredPath && existsSync(registeredPath)) continue; // still valid
    console.log(`[caroline] ${server.name}'s registered command is stale (missing file), refreshing...`);
    await mcpRemove(workspaceDir, server.name);
    const r = await mcpAdd(workspaceDir, server.name, process.execPath, server.args, "user");
    if (r.code !== 0) {
      console.error(`[caroline] failed to refresh ${server.name}:`, r.stderr || r.stdout);
    }
  }
}
