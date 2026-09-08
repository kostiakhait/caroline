// Dev-time packaging step: turns this repo's own vendored MCP server
// sources (backend/mcp-servers-src/) into backend/mcp-servers/, so the
// shipped backend never has to reference anything outside this repo. Run
// before packaging a Caroline release; not part of the app's runtime path.
//
// Vendored, not a sibling-repo reference: this project was extracted from a
// larger private monorepo (silmarillion) where these same servers lived
// under a shared MCP/* directory reused by several projects. Caroline gets
// its own independent copy under mcp-servers-src/ so this repo is fully
// self-contained and buildable on its own -- these copies are free to
// diverge from whatever the original monorepo's versions do next.
//
// Most servers are bundled into a single .mjs file each via esbuild (all
// npm deps inlined, Node builtins left external automatically) -- this used
// to be a plain recursive copy of dist/+node_modules for every server,
// which put ~45,000 files (mostly duplicated node_modules) into a single
// Caroline release, making every zip-packing tool tried choke on it (the
// bottleneck wasn't compression, it was AV/filesystem overhead opening tens
// of thousands of tiny files). `browser` is the one exception still copied
// wholesale: playwright-core's dynamic/optional requires (chromium-bidi
// etc.) aren't something esbuild's static bundler can resolve.
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCE_MCP_DIR = join(__dirname, "..", "mcp-servers-src");
const OUT_DIR = join(__dirname, "..", "mcp-servers");

// name: source folder under mcp-servers-src/. hasNative: also copy sibling
// *.exe/*.dll from dist/ (each wrapper locates it via join(__dirname,
// "*.exe"), which still works post-bundling since __dirname resolves to
// wherever the bundled index.mjs itself ends up at runtime).
const BUNDLED_SERVERS = [
  { name: "notes" },
  { name: "sms" },
  { name: "time" },
  { name: "chain" },
  { name: "voice" },
  { name: "screen-video" },
  { name: "screenshot", hasNative: true },
  { name: "mouse", hasNative: true },
  { name: "keyboard", hasNative: true },
  { name: "inspect", hasNative: true },
  { name: "window-screenshot", hasNative: true },
  { name: "window-mouse", hasNative: true },
  { name: "window-keyboard", hasNative: true },
];
// email used to be here too (same "dynamic require" problem as browser --
// imapflow/nodemailer lazily require() Node builtins esbuild can't
// resolve), but it's no longer a spawned stdio server at all -- see
// Caroline/backend/src/email/index.ts, now an in-process SDK tool built
// straight into the backend bundle above, nothing left for this script to do.
const COPIED_SERVERS = ["browser"];

if (!existsSync(OUT_DIR)) mkdirSync(OUT_DIR, { recursive: true });

// A freshly-copied .exe/.dll can be transiently locked by AV real-time
// scanning right after the write -- confirmed here: rmSync on a directory
// containing one from the previous run's copy occasionally throws EBUSY.
// Not a real conflict, just a race; a short retry clears it every time.
async function rmDirWithRetry(dir) {
  const maxAttempts = 20;
  for (let attempt = 1; ; attempt++) {
    try {
      rmSync(dir, { recursive: true, force: true });
      return;
    } catch (err) {
      if (err.code !== "EBUSY" || attempt >= maxAttempts) throw err;
      await new Promise((resolve) => setTimeout(resolve, Math.min(2000, 300 * attempt)));
    }
  }
}

for (const { name, hasNative } of BUNDLED_SERVERS) {
  const srcDir = join(SOURCE_MCP_DIR, name);
  const entry = join(srcDir, "dist", "index.js");
  const outDir = join(OUT_DIR, name);

  if (!existsSync(entry)) {
    console.warn(`[bundle] SKIP ${name}: no dist/index.js -- run "npm run build" in MCP/${name} first`);
    continue;
  }

  console.log(`[bundle] ${name} ...`);
  // Overwrite in place rather than rm+recreate -- deleting outDir hit a
  // persistent (not just transient-AV-scan) EBUSY on this machine once for
  // no reproducible reason, and a stale extra file left behind here isn't a
  // real risk: it's just one bundled .mjs plus its native .exe/.dll siblings,
  // both always fully overwritten below.
  mkdirSync(outDir, { recursive: true });

  await esbuild.build({
    entryPoints: [entry],
    outfile: join(outDir, "index.mjs"),
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node20",
    // "main" fields on a couple of transitive deps resolve to CJS-only
    // builds; esbuild's default interop for those is fine here since
    // nothing in this bundle sits behind a top-level `require`.
    logLevel: "warning",
  });

  if (hasNative) {
    const srcDist = join(srcDir, "dist");
    // These are framework-dependent .NET builds, not self-contained --
    // .deps.json/.runtimeconfig.json are how the apphost (the .exe) finds
    // hostpolicy.dll and the right shared runtime at all. Missing them
    // doesn't fail loudly at build time, only at run time ("hostpolicy.dll
    // missing"), which is exactly the bug this bundler introduced by only
    // copying .exe/.dll -- confirmed live.
    const nativeFiles = readdirSync(srcDist).filter((f) =>
      f.endsWith(".exe") || f.endsWith(".dll") || f.endsWith(".deps.json") || f.endsWith(".runtimeconfig.json"));
    if (!nativeFiles.some((f) => f.endsWith(".exe"))) {
      console.warn(`[bundle] WARNING: ${name} expected a native .exe in dist/ but found none -- run its "dotnet build native" step`);
    }
    for (const f of nativeFiles) {
      cpSync(join(srcDist, f), join(outDir, f));
    }
  }
}

for (const name of COPIED_SERVERS) {
  const srcDir = join(SOURCE_MCP_DIR, name);
  const srcDist = join(srcDir, "dist");
  const srcNodeModules = join(srcDir, "node_modules");
  const outDir = join(OUT_DIR, name);

  if (!existsSync(srcDist)) {
    console.warn(`[bundle] SKIP ${name}: no dist/ -- run "npm run build" in MCP/${name} first`);
    continue;
  }

  console.log(`[bundle] ${name} (copied, not bundled -- see file header) ...`);
  if (existsSync(outDir)) await rmDirWithRetry(outDir);
  mkdirSync(outDir, { recursive: true });

  cpSync(srcDist, join(outDir, "dist"), { recursive: true });
  if (existsSync(srcNodeModules)) {
    cpSync(srcNodeModules, join(outDir, "node_modules"), { recursive: true });
  }
  cpSync(join(srcDir, "package.json"), join(outDir, "package.json"));
}

console.log(`[bundle] done -> ${OUT_DIR}`);
