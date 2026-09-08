import { getSecret, setSecret, listSecrets } from "mcp-vault-client";

const SERVICE_PREFIX = "email:";

export interface Credentials {
  user: string;
  password: string;
  imapHost: string;
  imapPort?: number;
  smtpHost?: string;
  smtpPort?: number;
}

/** Loads one saved account's credentials from the vault by its email address. */
export async function loadCredentials(user: string): Promise<Credentials | null> {
  const data = await getSecret(SERVICE_PREFIX + user);
  return (data as unknown as Credentials) ?? null;
}

/** Saves (or overwrites) one account's credentials in the vault. */
export async function saveCredentials(creds: Credentials): Promise<void> {
  await setSecret(SERVICE_PREFIX + creds.user, creds as unknown as Record<string, unknown>);
}

/** Lists every email account saved in the vault (user + imapHost only, never passwords). */
export async function listAccounts(): Promise<{ user: string; imapHost: string }[]> {
  const services = await listSecrets();
  const accounts: { user: string; imapHost: string }[] = [];
  for (const service of services) {
    if (!service.startsWith(SERVICE_PREFIX)) continue;
    const user = service.slice(SERVICE_PREFIX.length);
    const creds = await getSecret(service);
    if (creds) accounts.push({ user, imapHost: (creds as unknown as Credentials).imapHost });
  }
  return accounts;
}
