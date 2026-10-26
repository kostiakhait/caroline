---
name: vault-backups
description: How to save secrets and periodic working-memory backups to the "Caroline:Vault" Notes folder. Use this whenever you need to store a secret/password/API key/token, or when a periodic/shutdown memory-backup reminder fires.
---

# Vault backups

If the Notes tool is available and logged in (see the `squirrelwisdom-login` skill), it's your
secure storage. This is the same Notes-backed vault mechanism the rest of this codebase uses (see
MCP/_shared/vault) but under its own folder, "Caroline:Vault" -- kept separate from other tools'
vault contents so your own state is portable on its own.

## Secrets

Never write secrets/passwords/API keys/tokens to local files, chat history, or Skills files.
Always save them as notes in the folder "Caroline:Vault" (create it if it doesn't exist), one note
per secret, title starting with `vault:`.

## Periodic/shutdown memory backup -- done by the backend, not by you

Since 2026-10-26 there is no backup reminder and nothing for you to do: the backend itself copies, by code and
only what changed, every hour and when the app closes, into the "Caroline:Vault" folder:
- the scheduled reminders -> note `vault:backup`;
- every Skills/lessons-learned/<topic>/SKILL.md -> note `vault:skill:<topic>`;
- the files of workspace/dehydrated/ and workspace/uploads/ -> attachments of note `vault:dehydrated-files`
  (named `<dir>__<file name>`).
The persona and the other settings are mirrored separately ("Caroline:Settings").

After a login on a fresh install the backend restores what is missing (reminders, lesson skills, files) by itself.
If a resumed conversation references a dehydrated file that is gone from the local disk, look for it as an
attachment on `vault:dehydrated-files` before telling the user it is unrecoverable.
