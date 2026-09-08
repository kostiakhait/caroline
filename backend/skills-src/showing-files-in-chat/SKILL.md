---
name: showing-files-in-chat
description: How to actually show or hand off a local image/video/document to the user from within the chat -- the chat page itself cannot embed arbitrary local files inline. Use this whenever you need to display, open, or hand the user a file that isn't one of your own shipped reference photos.
---

# Showing files in the chat

The chat page can only render your own shipped reference photos inline (via Markdown image
syntax). For any other file -- something the user attaches, something you find or create -- you
cannot embed it directly in your reply. Don't just describe the file in text as a substitute for
showing it; use one of the options below, or say plainly that you can't show it if none apply.

## Options

1. **open_file** -- opens it immediately in the user's default Windows application (image viewer,
   video player, PDF reader, Office, etc.), exactly like double-clicking it in File Explorer.
   Fire-and-forget: you don't find out what the user does with it afterward.

2. **open_in_viewer** -- opens it in Caroline's own floating window instead. Images/video just
   display; documents (docx/xlsx/pptx/pdf) open for real editing via an embedded OnlyOffice
   editor, which requires the user to be logged into SquirrelWisdom (see the
   `squirrelwisdom-login` skill) and a working internet connection. Returns immediately, before
   the user is done -- you'll get a separate proactive message once they're finished, so react to
   that when it arrives rather than assuming an outcome right after calling this.

3. **Markdown link** -- if you're just mentioning a file rather than acting on it right now, write
   a `file:///` link, e.g. `[invoice.pdf](file:///C:/path/to/invoice.pdf)` -- the chat UI renders
   that as a clickable link that opens the file (via open_file's behavior) when clicked.
