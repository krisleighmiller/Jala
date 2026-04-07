# Interactive Terminal Agent Architecture

## Purpose

This document describes the architecture currently implemented for `Jala` and clarifies the near-term direction for the project.

`Jala` is no longer just a one-shot AI CLI wrapper. It is a daemon-backed local terminal agent with:

- a short-lived CLI client
- a persistent local HTTP daemon
- disk-backed session and approval state
- structured read-only tools
- approval-gated state-changing execution

This document is primarily descriptive of the current implementation, with a final section that captures the most likely next architectural steps.

## Product Identity

The current product identity is best summarized as:

> A local, stateful terminal agent with explicit approvals, durable memory, and inspectable actions.

That distinguishes `Jala` from tools that only:

- translate natural language into a shell command
- wrap a single prompt/response interaction
- provide interactive terminal help without durable workflow state

## Naming And Branding

The current user-facing runtime surface is:

- `Jala`: product name
- `jala`: CLI entrypoint
- `jala-daemon`: local daemon entrypoint

Internal Python packages currently live under:

- `api`
- `core`

## Current Architecture

The shipped system is composed of:

- `jala`: thin CLI client
- `jala-daemon`: local HTTP daemon launcher
- `api/api_server.py`: request handling, persistence, tool routing, approvals, and history retrieval
- `core/neutral_terminal.py`: OpenAI integration and bounded local process execution
- `core/env_config.py`: `.env` discovery and loading

At a high level:

```/dev/null/architecture.mmd#L1-9
flowchart LR
    user[User At Shell Prompt] --> cli[jala CLI]
    cli --> api[Local HTTP Daemon]
    api --> db[SQLite State]
    api --> llm[OpenAI Chat Completion]
    llm --> api
    api --> tools[Structured Read-only Tools]
    api --> approval[Approval-gated Shell Execution]
    approval --> api
    api --> cli
```

## Interaction Model

The core interaction model is:

- the user's shell prompt remains free
- each conversational turn is sent with a one-command CLI invocation
- the daemon owns session, event, and approval state
- sessions survive daemon restarts
- state-changing actions are never executed inline without approval

This means `Jala` behaves more like a local agent runtime than a transient terminal helper.

## CLI Surface

The CLI intentionally stays narrow.

### Supported commands

- `jala "<message>"`
- `jala -s <session_id> "<message>"`
- `jala --session <session_id> "<message>"`
- `jala approve <approval_id>`
- `jala deny <approval_id>`
- `jala history`
- `jala history --session_id <id> --event_type <type> --start_time <time> --end_time <time>`
- `jala-daemon`

### Current CLI behavior

`jala` is a transport-oriented client. It does not independently execute proposed actions. Its main jobs are to:

- load environment variables
- parse chat, approval, denial, and history commands
- capture the caller's current working directory
- send requests to the daemon
- print the daemon response and exit

The default session id is `default`, and named sessions are supported with `--session` and `-s`.

### Current intentional limitations

The CLI does not yet provide:

- REPL mode
- streaming token output
- interactive candidate selection
- session lifecycle commands like list, rename, or delete
- shell hotkey integration

Those are roadmap possibilities, not current behavior.

## Local HTTP Service

The daemon exposes a deliberately small API surface over local HTTP.

### Implemented endpoints

- `GET /`
- `GET /health`
- `GET /history`
- `POST /chat`
- `POST /approve`
- `POST /deny`

### Request and response conventions

`POST /chat` expects:

```/dev/null/chat-request.json#L1-5
{
  "message": "summarize this repository",
  "cwd": "/home/user/projects/jala",
  "session_id": "default"
}
```

`POST /approve` and `POST /deny` expect:

```/dev/null/approval-request.json#L1-3
{
  "approval_id": "abcd1234"
}
```

Successful POST responses currently return:

```/dev/null/response.json#L1-3
{
  "response": "..."
}
```

### Local auth model

The daemon is local-first. If bound to loopback, it can run without auth. If configured to bind to a non-loopback host, an auth token is expected.

Current environment support includes:

- `API_HOST`
- `API_PORT`
- `API_AUTH_TOKEN`

This is a practical local security layer, not a full hardened multi-user platform.

## Persistence Model

The daemon persists state in SQLite.

### Database location

State is stored in:

- `JALA_STATE_DIR/jala.db` when `JALA_STATE_DIR` is set
- otherwise `$XDG_DATA_HOME/jala/jala.db`
- otherwise `~/.local/share/jala/jala.db`

### Persisted categories

The implementation persists these categories of state:

- session history
- request history
- approvals
- command execution records
- session events used to reconstruct or inspect history

### What persistence is for

Persistence exists so that:

- sessions survive daemon restarts
- approvals survive daemon restarts
- users can approve from a different shell later
- command execution history is inspectable
- event history can be queried after the fact

This persistence is part of the architecture, not a convenience cache.

## Session Model

`Jala` supports multiple named conversation sessions.

### Current session behavior

- default session id: `default`
- named sessions via `-s` / `--session`
- session continuity across daemon restarts
- per-session conversational history retained on the daemon side
- history filtering by session through `jala history` and `GET /history`

### What sessions are today

Today, sessions are conversational sessions, not durable shell process sessions.

That means the system currently preserves:

- message history
- tool-call results
- approvals and associated action state

But it does not yet preserve:

- an interactive shell process
- job control
- live terminal streams
- a persistent per-session subprocess environment

So `Jala` is already session-aware conversationally, but not yet terminal-session-native in the shell sense.

## Request Flow

A normal request lifecycle works like this:

1. The user runs `jala "<message>"` from some working directory.
2. The CLI loads environment values and sends `message`, `cwd`, and `session_id` to the daemon.
3. The daemon records the request.
4. The daemon appends the current working directory and user message into session context.
5. The daemon calls the model with session history and registered tools.
6. If the model returns plain text, the daemon stores the assistant reply and returns it.
7. If the model returns structured read-only tool calls, the daemon executes them, appends tool outputs, and may continue the same turn.
8. If the model returns any approval-required action, the daemon stores a pending approval and returns an approval block.
9. A later `jala approve <approval_id>` or `jala deny <approval_id>` resolves that pending action.

This preserves the fast one-command flow while still enforcing explicit user approval for mutations.

## Working Directory Semantics

Every chat request includes the caller's current working directory.

The daemon records that as contextual information in the session so follow-up turns can reason about location-sensitive requests.

That same stored directory is used for execution semantics:

- structured read-only tools resolve relative paths against the original request `cwd`
- approved shell actions execute in the original request `cwd`
- an approval can be completed from another directory because the daemon remembers the original one

This is one of the key terminal-aware properties already present in the system.

## Tool Model

The current tool model has two layers:

- structured read-only tools
- approval-gated free-form shell execution

This is an important architectural evolution from a pure "one shell tool" design.

### Structured read-only tools

The daemon currently exposes structured inspection tools for:

- `read_file`
- `search_files`
- `inspect_process`

These run without an approval round-trip because they are classified as inspection-only operations.

Their main role is to let the model answer common local questions without falling back to unrestricted shell commands.

Examples of supported read-only tasks include:

- reading a file from the caller's project
- searching for files by pattern
- inspecting a running process by pid

### Free-form shell execution

The daemon also exposes:

- `run_shell_command`

Unlike the read-only tools, free-form shell execution is approval-gated at runtime.

This is a deliberate policy choice. Even though there is still parser and helper logic related to read-only command classification, the current runtime approval rule is simpler and stronger:

- structured inspection tools may run immediately
- free-form shell execution requires explicit approval

### Why this split matters

This split gives `Jala` a safer and more predictable execution model:

- common inspection tasks do not need unnecessary approval
- mutating shell actions are gated
- structured tool schemas reduce ambiguity for simple reads
- shell remains available as a flexible escape hatch

Architecturally, this is one of the most important differences between `Jala` and simpler natural-language command generators.

## Approval Model

Approval is a first-class workflow state.

When the model proposes an approval-required action, the daemon returns a block like:

```/dev/null/approval-block.txt#L1-4
[APPROVAL_REQUIRED]
Tools: [run_shell_command]
Approve: jala approve <approval_id>
Deny: jala deny <approval_id>
```

### Approval state

Approvals are persisted and move through states such as:

- `pending`
- `executing`
- `executed`
- `denied`
- `failed`

### Current approval properties

- approval ids are explicit and user-visible
- approvals survive daemon restarts
- approved actions run in the original request directory
- denied actions are recorded
- failed actions are recorded
- interrupted execution is handled conservatively rather than retried blindly

This is much closer to an agent execution workflow than a simple interactive yes/no shell prompt.

## History And Observability

The daemon includes a lightweight but useful local observability surface.

### Current history support

- `GET /history`
- `jala history`
- optional filtering by:
  - `session_id`
  - `event_type`
  - `start_time`
  - `end_time`

### What is currently inspectable

The history surface includes recent:

- requests
- approvals
- command executions
- matching session events

Recorded command execution data includes values such as:

- command text
- working directory
- exit code
- duration
- captured output
- timestamp

### What this is and is not

This is currently:

- a debugging and inspection surface
- a trust-building aid
- a basis for future session and audit improvements

It is not yet:

- a full observability platform
- a comprehensive audit export system
- a retention-managed execution ledger
- a security analytics layer

## Model Integration

`core/neutral_terminal.py` currently owns model access and local process execution helpers.

### Current model behavior

- reads `OPENAI_API_KEY`
- defaults to `gpt-4o`
- uses the OpenAI Python client
- sends accumulated session history on each call
- disables parallel tool calls
- supports bounded tool-call loops within a single turn

This lets the daemon complete multi-step read-only reasoning in one request when needed.

## Bounded Local Execution

Local execution is designed to be bounded rather than unstructured.

### Current bounded behavior

- command output is size-limited
- request body size is limited
- file reads are size-limited
- daemon request concurrency is bounded
- command execution uses timeout-aware process handling

Relevant environment knobs include:

- `JALA_MAX_OUTPUT_BYTES`
- `JALA_MAX_REQUEST_BYTES`
- `JALA_MAX_FILE_READ_BYTES`
- `JALA_MAX_CONCURRENT_REQUESTS`
- `OPENAI_TIMEOUT_SECONDS`

These controls help the daemon behave like a local service rather than an unconstrained shell runner.

## Environment Loading

Both CLI and daemon load environment variables via `core/env_config.py`.

Current behavior:

- search upward from the current working tree for `.env`
- load the first matching `.env`
- only fill values that are not already set in the shell environment

This makes project-local configuration practical without overriding explicitly exported values.

## What Is Implemented Today

At the architectural level, `Jala` currently provides:

- daemon-backed chat
- named sessions
- persistent session and approval state
- local HTTP API
- working-directory-aware requests
- structured read-only local tools
- approval-gated shell execution
- lightweight local history retrieval
- bounded command output capture
- filtered history queries
- optional auth for non-loopback binding

That is already a substantial local agent architecture, even if it is still intentionally narrow.

## What Is Not Yet Implemented

The current system still does not provide:

- streaming partial responses
- streaming command output to the client
- long-running job management
- interactive subprocess handoff
- REPL mode
- rich TUI mode
- session list / rename / delete commands
- multiple candidate action selection
- beginner-oriented explanation mode for alternative commands
- user-defined registered tools
- richer policy controls for custom tool classes

These are the most important gaps between the current implementation and the broader terminal-agent vision.

## Architectural Direction

The core architecture does not need to be replaced. The next stage is evolutionary.

### Direction 1: richer session UX

Likely future improvements include:

- REPL mode on top of the daemon-backed session model
- better session discovery and management
- improved pending-approval browsing by session

This would improve usability without abandoning the current CLI-plus-daemon foundation.

### Direction 2: richer action presentation

A strong next-step feature is support for multiple candidate actions or commands for one user request.

That would let `Jala` do things like:

- propose a recommended action
- show safer or simpler alternatives
- explain why one option is preferred
- let the user choose among options before approval

This is especially valuable for users who are learning shell workflows and would fit well within the existing approval architecture.

### Direction 3: richer command explanation

Another likely addition is explicit explanation and teaching support, such as:

- describing a proposed command
- explaining flags and shell syntax
- showing a safer variant
- showing a more beginner-friendly variant

That would complement the approval system and make `Jala` more educational and trustworthy.

### Direction 4: user-defined registered tools

A particularly promising future direction is controlled extensibility through user-defined tools.

The right architectural version for `Jala` is not arbitrary unrestricted function loading, but registered tools with declared policy such as:

- name and description
- argument schema
- read-only vs mutating classification
- approval requirement
- timeout and output limits

This would allow project-specific and user-specific workflows while preserving the current approval and bounded-execution model.

### Direction 5: better terminal-runtime behavior

Longer term, the daemon should become more terminal-native by adding support for:

- streaming command output
- long-running task tracking
- background job handles
- better representation of execution lifecycle

That is the main remaining gap between the current architecture and a fuller terminal-aware runtime.

## Non-Goals Of The Current Design

The present architecture does not try to:

- replace the user's shell
- intercept arbitrary shell input
- silently auto-run broad classes of mutating commands
- become a multi-user remote execution service
- trade away explicit approval for convenience

Those constraints are part of what gives the current design its clarity.

## Summary

`Jala` currently ships as a short-lived CLI paired with a persistent local daemon that owns conversation, approval, and history state.

The key architectural properties already in the code are:

- one-command conversational turns
- named sessions and durable memory
- working-directory-aware requests
- SQLite-backed persistence
- structured read-only tools for common local inspection
- approval-gated free-form shell execution
- explicit approval lifecycle state
- lightweight local history and event inspection
- bounded request and execution behavior

The next stage should build on that base, not replace it.

The most valuable additions are likely to be:

- REPL-oriented session UX
- richer explanation and teaching flows
- optional multiple candidate actions
- registered user-defined tools
- more terminal-native handling of long-running work

Those would deepen `Jala`'s identity as a local, stateful, approval-aware terminal agent.