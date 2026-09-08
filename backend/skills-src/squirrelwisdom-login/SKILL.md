---
name: squirrelwisdom-login
description: How and when to get the user logged into their SquirrelWisdom account, needed for Notes and other SquirrelWisdom-backed features. Use this the first time a conversation could plausibly need Notes, or whenever a SquirrelWisdom-backed tool call fails complaining about not being logged in.
---

# SquirrelWisdom login

Notes and other SquirrelWisdom-backed features need the user's SquirrelWisdom account to be
logged in on this machine.

## When to check

Call `ensure_squirrelwisdom_login` once, proactively:
- The first time a conversation could plausibly need Notes (e.g. the user asks you to
  remember/save something, or you're about to do your own periodic memory backup -- see the
  `vault-backups` skill) and it hasn't succeeded yet.
- Whenever a SquirrelWisdom-backed tool call fails complaining about not being logged in.

## Hard rule

NEVER ask the user to type their email or password into the chat itself. The tool opens a real
login form in Caroline's own window for that, and the credentials never pass through you or the
chat transcript. You will be nudged separately, as a new proactive message, once the user logs in
or cancels -- don't assume an outcome right after calling the tool.
