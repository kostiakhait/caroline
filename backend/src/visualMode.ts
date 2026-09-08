import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { getPersona } from "./persona.js";

interface VisualModeSettings {
  enabled?: boolean;
}

function settingsPath(workspaceDir: string): string {
  return join(workspaceDir, "visualMode.json");
}

function loadSettings(workspaceDir: string): VisualModeSettings {
  try {
    if (!existsSync(settingsPath(workspaceDir))) return {};
    return JSON.parse(readFileSync(settingsPath(workspaceDir), "utf-8"));
  } catch (err) {
    console.error("[caroline] [visualMode] loadSettings failed, defaulting to {}:", err);
    return {};
  }
}

/** Default true per explicit instruction ("по умолчанию он включен"). */
export function isVisualModeEnabled(workspaceDir: string): boolean {
  const enabled = loadSettings(workspaceDir).enabled ?? true;
  console.error(`[caroline] [visualMode] isVisualModeEnabled: ${enabled}`);
  return enabled;
}

export function setVisualModeEnabled(workspaceDir: string, enabled: boolean): void {
  console.error(`[caroline] [visualMode] setVisualModeEnabled: ${enabled}`);
  writeFileSync(settingsPath(workspaceDir), JSON.stringify({ enabled }, null, 2) + "\n", "utf-8");
}

export type VisualModelSource = "caroline" | "peter";

export interface VisualModel {
  source: VisualModelSource;
  variant: "A" | "B";
  /** Absolute path to the .xcfa model file. */
  modelPath: string;
}

/**
 * CAROLINE_MODELS_DIR is set by BackendProcess.cs (WPF shell) on every spawn --
 * models live as a SIBLING of the app dir (CarolineInstaller.AppPaths: Root\app\
 * + Root\art\models\), not inside it, because the app dir gets fully deleted and
 * recreated on every update (Program.cs's extraction step) and these models are
 * tens of GB each -- installed once by CarolineInstaller (ModelsInstaller.cs),
 * never re-downloaded on a routine app update. Falls back to the dev-tree-relative
 * guess ("../art/models" from backend/, i.e. Caroline/art/models) when the env
 * var is unset (an old BackendProcess.cs build) or doesn't actually exist --
 * covers a plain source-tree dev run, e.g. via `node dist/server.js` directly.
 */
function modelsDir(): string {
  const fromEnv = process.env.CAROLINE_MODELS_DIR;
  if (fromEnv && existsSync(fromEnv)) return fromEnv;
  return join(process.cwd(), "..", "art", "models");
}

/**
 * Which .xcfa model backs Visual Mode right now, or null if unavailable --
 * either the profile is "custom" (per explicit instruction: no model exists
 * for a user-authored identity, so Visual Mode is simply off for it) or the
 * resolved model file isn't actually present on disk (see modelsDir's doc
 * comment -- a real possibility until packaging exists).
 *
 * Day-parity (even day-of-month -> "A", odd -> "B") is resolved fresh every
 * call rather than cached -- deliberately cheap to call repeatedly. Per
 * explicit instruction, the *warmed* PreparedModel in the WPF shell itself
 * only re-resolves this once at Caroline's own startup, not on every reply --
 * that caching lives entirely on the C# side (VisualModeManager), not here.
 */
export function resolveVisualModel(workspaceDir: string): VisualModel | null {
  const persona = getPersona(workspaceDir);
  if (persona.profileKey !== "caroline" && persona.profileKey !== "peter") {
    console.error(`[caroline] [visualMode] resolveVisualModel: profileKey=${persona.profileKey} -> unavailable (custom profile)`);
    return null;
  }

  const variant: "A" | "B" = new Date().getDate() % 2 === 0 ? "A" : "B";
  const fileName = `${persona.profileKey === "caroline" ? "Caroline" : "Peter"}${variant}.xcfa`;
  const modelPath = join(modelsDir(), fileName);
  if (!existsSync(modelPath)) {
    console.error(`[caroline] [visualMode] resolveVisualModel: modelPath=${modelPath} not found -> unavailable`);
    return null;
  }

  console.error(`[caroline] [visualMode] resolveVisualModel: source=${persona.profileKey} variant=${variant} modelPath=${modelPath}`);
  return { source: persona.profileKey, variant, modelPath };
}

export function isVisualModeAvailable(workspaceDir: string): boolean {
  return resolveVisualModel(workspaceDir) !== null;
}
