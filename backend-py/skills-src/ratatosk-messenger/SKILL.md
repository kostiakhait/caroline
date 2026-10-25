---
name: ratatosk-messenger
description: Managing the user's Ratatosk messenger (SquirrelWisdom's own chat app) on their behalf, and optionally Caroline's own separate Ratatosk account. Use this whenever the user asks you to check, read, or send Ratatosk messages, or mentions a contact/conversation there.
---

# Ratatosk messenger

Ratatosk is SquirrelWisdom's own messenger. The `ratatosk_*` tools (`ratatosk_identity_status`,
`ensure_ratatosk_own_account`, `ratatosk_list_conversations`, `ratatosk_get_messages`,
`ratatosk_send_message`, `ratatosk_start_chat_with`) all take `as: "owner" | "caroline"` so the
same tools drive both identities.

## Two identities -- always know which is which

Call `ratatosk_identity_status` if you're not sure which identity applies, or before acting when
it matters who a message is "from" -- confusing the user's own account with your own separate one
is a real failure mode to avoid.

- **`as: "owner"`** -- acts as the user's own SquirrelWisdom session (needs them logged in, see
  the `squirrelwisdom-login` skill). Sending here is genuinely indistinguishable from the user
  typing it themselves. You have the user's own standing, broad authorization for this
  channel specifically -- no per-message confirmation needed, unlike most outbound
  communication. This does NOT waive the general rule against inventing facts: only say things
  in a message you actually know to be true.
- **`as: "caroline"`** -- acts as your own, separate Ratatosk account (a real mailbox + a real
  SquirrelWisdom account, both auto-generated, unique to this install). Doesn't exist until you
  call `ensure_ratatosk_own_account` (idempotent -- safe to call any time, it's a no-op if you
  already have one).

## The owner-DM control channel

If you have your own account (`as: "caroline"`), a direct-message conversation with the OWNER
(resolved dynamically from their own logged-in account, never hardcoded) works as an always-on,
headless second channel into you -- independent of whichever WPF tabs happen to be open. A
message that arrives there is the owner giving you an instruction exactly the same way a chat
tab message is; reply there with `ratatosk_send_message as:"caroline"`, not in any visible chat
window (nothing renders this conversation as a tab).

## Access scope

You have access to ALL of the owner's conversations when acting `as:"owner"` (confirmed with the
user -- not restricted to an allowlist). Use `ratatosk_list_conversations` to see what's there,
`ratatosk_start_chat_with` to find or open a DM with a specific email.
