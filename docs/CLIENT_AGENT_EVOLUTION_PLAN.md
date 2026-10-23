# Client agents off the server: evolution of Caroline's `reply:` agents

- **Author:** Claude (Claude Code), for Konstantin Khait
- **Version:** 0.1 (draft for approval)
- **Date:** 2026-10-22
- **Company:** Partners Solutions
- **Project:** Caroline / Camerlengo (reforce)

## Rule

Camerlengo holds only universal agents (today: the `memory:` domain). A client
agent -- here Caroline's eight `reply:` roles -- and everything about it lives in
the client. The server may manage evolution (which genome is champion, open
tournaments, win counts, promotion/cull), but it never holds the agent itself.

## What is wrong today

1. **The server holds copies of Caroline's agents.** reforce
   `apps/microagents/{PermissionGuard,PermissionIdentify,RefusalGuard,ReplyReview,ActionSummary,
   ReplyExpected,BackgroundRelayWorth,ContentSummary,ReplyTranslate}.py` and their entries in
   `apps/microagents/EvolvableRoles.py` (seed rules text, schema, render, ladder, description).
2. **The server's copy wins over Caroline's own.** `evolution:resolveCalls` returns the server's
   seed text whenever there is no champion clone; Caroline runs it instead of its own. Seen
   2026-10-22: `turn.permission.guard` ran the server's old text (with the "EXACT WORDING" rule),
   not Caroline's current one, so a user's direct order to reply was blocked for wording approval.
   The same applies to `reply.review` and the other six roles -- Caroline-side prompt edits to
   them have had no effect.
3. **Mutations are bred from the server's copy.** `mutate_needed` hands the client the server's
   seed text as the parent; challengers (e.g. tournament #6 of the guard) descend from it.
4. **Old server-executed commands** `reply:*` (`API/Api2ReplyCommands.py`) still run the server
   copies.

## Target

**Client (Caroline)** owns, per role: the agent itself (schema, verify, render, seed rules text,
seed ladder, description), building the mutator's input, computing a new clone's ladder, and
running every model call (agent, mutator, selector). It stores no genome locally.

**Server (Camerlengo)** owns, per (api_path, scope, role), role-agnostic evolution data: settings
(frequencies, weights, `champion_clone_id`), clones with their texts and ladders, tournaments, the
dice roll, promotion/cull, and the role's seed genome **as registered by the client** (never a
copy from reforce's own code). It knows nothing about a client role beyond its name and the texts
it was given, and always hands out the current genome: the seed while no clone has won, else the
champion clone.

## Decisions (2026-10-22)

- **Genomes live on the server** with the settings and tournament counts (revised: first decided
  "client clones on the client"). The client stores no genome; the server always gives it the
  current one. "Seed" means the client's own seed text, registered by the client.
- **Evolution scope = the user's SW account** (revised; first decided per installation). Scope
  `<caller key identity>:<SW account>`, derived by the server from the caller's session, never from
  a field the client could set to someone else's. Shared by every machine the account runs Caroline
  on; survives reinstalling.
- **No backup to Notes, no merge** -- the server is the one source of truth.
- **The server is the genome's one source of truth; the client only initializes it.** Every
  resolve carries the agent's version per role. The server answers `seed_needed` only when it has
  no seed for the role in that scope, or one from an OLDER agent version; the client then
  registers its seed (`evolution:registerSeed`) and resolves again. An edit of the seed text in the
  client's code without a version bump never reaches the server. Re-initialization from a newer
  version resets that role's evolution in the scope (open tournaments culled, clones retired,
  champion back to the seed). A client on an older version gets the server's genome as it is.
- **Server unreachable:** the client runs its own seed from code (it never has anything else).
- **Existing clones/tournaments of the eight roles are reset** (step 2).
- **The old server `reply:*` commands stay for now** -- old Caroline versions still call them; they
  are removed later, together with the server copies of the agent modules they run. Until then only
  the roles' entries in `EvolvableRoles.py` go, so the server stops handing out its copies.

## Protocol changes

- All evolution commands take the caller's SW `session`; scope as above. A role with no settings
  row in the account's scope takes the tunables of the key's own row (`caroline-client-v2`) as a
  template, with no champion; the account's row is created on its first mint.
- `evolution:resolveCalls`, for a role the server has no entry for:
  - no seed yet, or the caller's agent version is newer -> `{mode: "seed_needed"}`;
  - otherwise the current genome: the registered seed while no clone has won, else the champion
    clone -- name, text, ladder;
  - `mutate_needed` -> no `mutatorInput`, only `championClone` (the current genome); the client
    builds the mutator input from it;
  - `tournament` -> champion as above, challenger with name, text and ladder.
- `evolution:registerSeed` (new): role, seed text, ladder JSON, agent version; initializes the
  seed, or re-initializes it from a newer version (with the reset above); otherwise changes nothing.
- `evolution:mintClone`: the client sends the clone's text and its ladder JSON (it computes the
  ladder from `ladder_change`); the server stores both as given.
- `evolution:recordOutcome`: only for a tournament in the caller's own scope.

## Steps

1. **reforce (`microagents` branch):** the protocol above in `Api2Evolution.py` /
   `Api2EvolutionCommands.py`; remove the eight `reply:` roles from `EvolvableRoles.py`; update
   tests. Deploy through git, with separate permission.
2. **Existing evolution data for the eight roles:** close open tournaments (`challenger_culled`),
   mark clones `retired`, reset `champion_clone_id` to NULL; rows stay for history.
3. **Caroline:** `run_reply_agent` sends the session and its agents' versions; registers its seed on
   `seed_needed`; runs the genome the server gives; builds the mutator input from the champion it
   is given; sends ladder JSON in `mintClone`; runs its own seed when the server is unreachable;
   logs which genome ran (seed / clone name) on every call.
4. **Order:** Caroline first (it must already accept the new shapes), then the server.
5. **Check on the test box:** guard on E2 runs Caroline's text (a direct order to reply is not
   blocked for wording); a forced mutation (replication frequency raised for one role on a test
   scope) mints a clone from Caroline's seed and runs a tournament round end to end.

## Out of scope

The `memory:` domain stays on the server, evolution included. The draft-loss fix for the
permission note (the blocked call's recipient/subject/text go into the note) is separate.
