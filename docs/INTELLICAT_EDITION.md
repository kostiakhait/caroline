# The IntelliCat edition (SquirrelWisdom only)

The IntelliCat console answers through SquirrelWisdom only. In its build the **Claude and OpenAI answer sources do
not exist**: not hidden, not switched off, but removed, together with everything that serves them (engines, sign-in,
model choice, their pages and buttons). The source tree stays one tree; the default (Windows / Linux desktop) build is
unchanged.

## How it works

Code that belongs to the Claude and OpenAI sources is marked, and `tools/edition_build.py` removes it from a build output
(a conditional compilation done by a text pass, since Python and the page scripts have no preprocessor):

| Marker | Effect |
| --- | --- |
| `edition:cut <why>` ... `edition:end` | the lines between (and the markers) are removed in the IntelliCat edition |
| `#ic| <code>` / `//ic| <code>` / `<!--ic| <markup> -->` | a line that exists only in the IntelliCat edition: the prefix is removed there; everywhere else it is an ordinary comment |
| `tools/edition_cut_files.txt` | whole files and directories removed from the build (the engines, `claude_ladder`, `cli_control`, `openai_*`, the GTK shell) |

The markers use each language's comment syntax, so the default build runs the same files untouched.

```
make dist-intellicat          # dist-linux, then tools/edition_build.py on a copy: dist-intellicat/
tools/edition_check.sh [py]   # build into /tmp, compile, look for undefined names, import the backend
```

`tools/edition_check.sh` fails when a cut leaves a name undefined (also behind the chat-session mixins' star imports,
which pyflakes alone cannot see: `tools/edition_flakes.py`), when the backend does not import, or when one of the
removed modules is imported anyway.

## What the edition does

* A tab has one answer source, SW: `durability.load_chat_mode` is `"sw"`, there is no choice to make and no REST/WebSocket
  operation to make it (`/api/mode`, `/api/openai*`, `chat_mode_*`, `auth_*`, `openai_*`, `model_override_*`,
  `own_anthropic_key_*`, `mcp_*` do not exist).
* The run loop of a tab builds no agent session; it reports healthy and idles (what the `sw` branch did already).
* Escalation inside the SW cascade is the next SW tier (as before); nothing hands a task to "full Claude".
* The settings page has no Claude account, Anthropic key, models, OpenAI or MCP sections; the start wizard has no
  subscriptions step (account -> mailboxes -> messengers).
* No process of the Claude binary or of Codex is ever started. The runtime of the edition does not need the bundled
  `claude_agent_sdk/_bundled/claude` (231 MB): delete it after `pip install` (the Python part of the package stays,
  see below).

## What is deliberately still there

* The **`claude_agent_sdk` Python package** (2 MB without its binary): the plugins' tool decorator and the message types
  (`AssistantMessage`, `TextBlock`, ...) are the data model of the whole backend, SW turns included. Replacing them is a
  refactor of its own.
* **Claude Code's session-file layout** (`~/.claude/projects/<slug>/<id>.jsonl`) and the `"claude"` key of a tab's session
  pointer: the format SW turns are stored in. Renaming is a data migration.
* Code that nothing reaches any more but that sits among live code is removed in rounds
  (`edition:cut claude-session code no longer reached`); comments that mention Claude are not edited.

## Rules for changes

* New code that serves only the Claude or the OpenAI source goes in a marked region (or in a listed file).
* A `#ic|` replacement must have the indentation of the code it stands for.
* Run `tools/edition_check.sh` before committing a change that touches a marked place.
