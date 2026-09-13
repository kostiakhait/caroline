---
name: showing-files-in-chat
description: How to actually show or hand off a local image/video/document to the user from within the chat. Use this whenever you need to display, open, or hand the user a file.
---

# Showing files in the chat

## Images

Any local image -- your own shipped reference photos, something you generated, something you
found or were handed -- can be shown inline with plain Markdown image syntax, using the file's
real path exactly as it exists on disk:

`![caption](C:\real\absolute\path\to\the\file.png)`

No prefix, no special folder, no trick of any kind is needed -- an ordinary absolute Windows path
just works. Do NOT invent workarounds like writing a path that starts with `assets/` and then
`../`s its way back out to the real location -- that never worked, never will, and just renders
as a broken image with nothing shown to the user. If a path won't display, it's very likely
wrong/mistyped/doesn't exist -- double check it rather than trying a creative rewrite of it.

## Video and documents (docx/xlsx/pptx/pdf, etc.)

The chat page cannot embed these inline. Use one of the options below, or say plainly that you
can't show it if none apply.

1. **open_file** -- opens it immediately in the user's default Windows application (video player,
   PDF reader, Office, etc.), exactly like double-clicking it in File Explorer. Fire-and-forget:
   you don't find out what the user does with it afterward.

2. **open_in_viewer** -- opens it in Caroline's own floating window instead. Video just displays;
   documents (docx/xlsx/pptx/pdf) open for real editing via an embedded OnlyOffice editor, which
   requires the user to be logged into SquirrelWisdom (see the `squirrelwisdom-login` skill) and a
   working internet connection. Returns immediately, before the user is done -- you'll get a
   separate proactive message once they're finished, so react to that when it arrives rather than
   assuming an outcome right after calling this.

3. **Markdown link** -- if you're just mentioning a file rather than acting on it right now, write
   a `file:///` link, e.g. `[invoice.pdf](file:///C:/path/to/invoice.pdf)` -- the chat UI renders
   that as a clickable link that opens the file (via open_file's behavior) when clicked.
