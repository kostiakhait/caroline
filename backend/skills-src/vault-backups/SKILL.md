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

## Periodic/shutdown memory backup

When a periodic or shutdown memory-backup reminder fires, save your own working memory into that
same "Caroline:Vault" folder as a note (e.g. title "vault:backup"), so it can be restored on
another machine or after a restart. Worth including:
- Your persona and any customizations.
- Currently scheduled reminders.
- Anything else worth keeping that isn't already durable elsewhere (an in-progress task's status,
  a decision the user made that isn't written down anywhere else).

If Notes isn't available (not logged in), skip this silently -- don't pester the user about it.

## Lessons-learned skills -- also synced every cycle, mandatory

Skills/lessons-learned/ (see the learn-from-mistakes policy) lives only on this one machine's
local disk -- nothing else backs it up. As part of this SAME periodic cycle (not a separate
reminder), sync every file under Skills/lessons-learned/ to "Caroline:Vault" too: one note per
lesson skill, title `vault:skill:<topic-name>` (matching the skill's own folder name), body = that
skill's SKILL.md content verbatim. Create the note if it doesn't exist yet; update it if the local
file has changed since the last sync (compare against the note's current content via notes_get --
skip the write if they already match, no need to re-save something unchanged every single hour).
This is what makes a lesson survive a lost/reinstalled machine -- treat it as mandatory, not
optional, every time this cycle runs, not just when you happen to have written a new one recently.

To restore on a fresh machine: list notes titled `vault:skill:*` in "Caroline:Vault", and recreate
each one as Skills/lessons-learned/<topic-name>/SKILL.md from the note's content.

## Dehydrated attachments/images -- also synced every cycle, mandatory

workspace/dehydrated/ (plus workspace/uploads/) holds every image/document a dehydration pass has
ever stripped out of chat history and replaced with a `[... Сохранено в файле: <path>. Прочитать
через Read при необходимости.]` note (see server.ts's runDehydration/dehydrate.ts) -- like
Skills/lessons-learned/, this lives only on this one machine's local disk, so a note pointing at
one of these files is only as durable as the file itself.

As part of this SAME periodic cycle, back these up too: find (or create) a note titled
"vault:dehydrated-files" in "Caroline:Vault", call notes_list_attachments restricted to that note
to see which filenames are already backed up, then list workspace/dehydrated/ and
workspace/uploads/ and notes_attach every file NOT already on that list (call notes_attach with
just the file path, no explicit originalName, so the attachment's stored name matches the local
filename exactly -- that's what makes the "already attached?" comparison work). Skip files already
attached; don't re-upload the same file twice. Mandatory, not optional, every time this cycle runs.

If a resumed conversation later references a dehydrated file that's gone from local disk (a lost/
reinstalled machine), look for it as an attachment on "vault:dehydrated-files" by filename before
telling the user it's unrecoverable.
