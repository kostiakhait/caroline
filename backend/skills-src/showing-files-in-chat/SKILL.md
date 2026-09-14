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

## Documents (docx/xlsx/pptx/pdf, etc.)

Per explicit instruction: absent any explicit direction otherwise, create documents in an
OnlyOffice-native format (.docx for text, .xlsx for spreadsheets, .pptx for slides -- not .pdf,
not plain text/markdown, unless the task specifically calls for one of those) and open them with
**open_in_viewer**, Caroline's own floating window with a real embedded OnlyOffice editor -- not
**open_file** (the user's default Windows application/Word/Excel). open_in_viewer requires the
user to be logged into SquirrelWisdom (see the `squirrelwisdom-login` skill) and a working
internet connection; it returns immediately, before the user is done -- you'll get a separate
proactive message once they're finished, so react to that when it arrives rather than assuming an
outcome right after calling this. Only reach for open_file on a document when the user explicitly
asked for their own application, or the file is a format OnlyOffice can't open at all.

## Video

The chat page cannot embed video inline, and OnlyOffice doesn't apply here. Use **open_file**
(the user's default Windows video player) -- fire-and-forget, you don't find out what they do
with it afterward -- or, if you're just mentioning it rather than acting on it right now, a
**Markdown link**: `[clip.mp4](file:///C:/path/to/clip.mp4)`, which the chat UI renders as a
clickable link that opens it (via open_file's behavior) when clicked.
