# Caroline technical documentation

Index of the technical documentation. The reference documents below describe what the code does
today; the `*_PLAN.md` files next to them are design plans and decision records (some of them
partly or fully implemented), listed at the end.

Read in this order if you are new to the repository:

| Document | What it covers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The layers, the processes, the ports, how a message travels through the system |
| [BACKEND.md](BACKEND.md) | `backend-py`: entry points, HTTP/WebSocket API, control ops, module map |
| [CHAT_SESSION.md](CHAT_SESSION.md) | `ChatSession`: one turn from submit to reply, resilience, compaction, durability |
| [ENGINES.md](ENGINES.md) | The three answer sources (Claude, OpenAI/Codex, SW cascade), the model ladder, tool groups |
| [PLUGINS.md](PLUGINS.md) | The plugin / tool system, the operations contract, how to add a tool |
| [MEMORY_AND_STATE.md](MEMORY_AND_STATE.md) | Every kind of memory, the workspace directory, what is persisted where |
| [MICROAGENTS.md](MICROAGENTS.md) | Client-side microagents: runtime, roster, how to add one |
| [MESSENGERS.md](MESSENGERS.md) | Messenger channels, sidecars, the companion Android app, Akinakes bus |
| [SHELLS_AND_INSTALLER.md](SHELLS_AND_INSTALLER.md) | WPF shell, Linux shell, supervisor, installer, Visual Mode |
| [BUILD_AND_RELEASE.md](BUILD_AND_RELEASE.md) | Makefile targets, packaging, the signed release manifest, updates |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables, ports, files, secrets |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Running, testing, debugging, conventions, pitfalls found in production |

Related top-level files: `README.md` (what Caroline is and its external requirements),
`PROJECT.md` (the long narrative overview with incident history), `BUILD.md` (build pipeline notes).

## Plans and decision records (already in this directory)

`AGENTIC_MEMORY.md`, `CLIENT_AGENT_EVOLUTION_PLAN.md`, `CONTINUOUS_NARRATION_PLAN.md`,
`INTELLICAT_EDITION.md`, `LINUX_PORT_PLAN.md`, `MESSENGER_INTEGRATIONS_PLAN.md`,
`MICROAGENTS_EVOLUTION_ARCHITECTURE.md`, `MICROAGENTS_PLAN.md`, `RELEASE_1_0_0_PLAN.md`,
`STAKES_AWARE_LADDER_PLAN.md`, `SW_MODE_TESTING_PLAN.md`, `SW_TOOL_ADVISOR_AND_TIER_STATS_PLAN.md`,
`TOOL_ADVISOR_PLAN.md`, `UPDATE_EVERYWHERE_PLAN.md`, `WORKING_MEMORY_PLAN.md`.

## About these documents

They were written by reading the repository (module docstrings, entry points, the Makefile, the
shells and the installer), not by running every path. Where a statement rests on a docstring or a
plan rather than on code that was traced, it says so. The code comments in this repository are
dated and incident-driven; when a document and the code disagree, the code wins, and the document
should be fixed.
