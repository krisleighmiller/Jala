# Jala

`Jala` stands for `Just another LLM agent`.

`Jala` is a local, daemon-backed terminal agent with durable session memory, explicit approval for state-changing actions, and structured local inspection tools.

At a glance:

- `jala "<message>"` sends one conversational turn
- `jala-daemon` starts the local HTTP daemon
- session, approval, and history state are persisted on disk
- structured read-only tools are preferred for local inspection
- free-form shell execution is available as an approval-gated escape hatch

## Current Status

The core architecture is in place and working:

- the shell prompt stays free
- each turn is sent with a short CLI invocation
- a local daemon owns conversation and approval state
- named sessions survive daemon restarts
- local actions are inspectable through history records

`Jala` is already more than a one-shot AI CLI wrapper, but it is still intentionally narrow and early in its product evolution.

## What Jala Does Today

- persists conversation, approval, request, and execution state on the daemon side
- sends the caller's current working directory with every turn
- uses the OpenAI chat API for responses
- supports multiple sessions through `--session` / `-s`
- exposes structured read-only inspection tools for:
  - file reads (`read_file`)
  - directory listing (`list_directory`)
  - file name search (`search_files`)
  - file content search (`search_file_contents`)
  - file metadata (`file_metadata`)
  - process inspection by PID (`inspect_process`)
  - process listing (`list_processes`)
  - read-only git inspection (`git_inspect`)
- reserves free-form shell execution as an approval-gated escape hatch
- returns approval blocks for state-changing actions
- exposes a small local HTTP API used by the CLI
- supports filtered local history retrieval for debugging and inspection
- supports optional bearer-token auth when binding the daemon to a non-loopback host

## What Jala Does Not Do Yet

- stream partial model responses
- stream command output
- provide a rich interactive REPL or TUI
- manage long-running jobs
- provide session lifecycle commands like list, rename, or delete
- offer multiple candidate commands/actions in one response
- support registered user-defined tools
- support multiple model providers and per-user provider/model selection

## Requirements

- Python 3.10+
- Linux or macOS
- `OPENAI_API_KEY`
- dependencies from `requirements.txt`

## Install

Create a virtual environment if you want one, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment Setup

The CLI and daemon automatically load environment variables from a `.env` file in the current working tree if one is present. You can also export them directly in your shell.

### Example `.env`

```env
OPENAI_API_KEY=your_openai_api_key_here

# Optional daemon settings
API_HOST=127.0.0.1
API_PORT=8000
OPENAI_TIMEOUT_SECONDS=60

# Required if binding to a non-loopback host
# API_AUTH_TOKEN=replace_me

# TLS — set both to enable HTTPS on the daemon and client
# API_TLS_CERT=/path/to/cert.pem
# API_TLS_KEY=/path/to/key.pem
# API_TLS_VERIFY=1   # set to 0 only with self-signed certs during development

# Optional runtime limits
# JALA_MAX_OUTPUT_BYTES=65536
# JALA_MAX_REQUEST_BYTES=1048576
# JALA_MAX_FILE_READ_BYTES=262144
# JALA_MAX_CONCURRENT_REQUESTS=16
```

### Shell-based setup

```bash
export OPENAI_API_KEY="your_openai_api_key_here"
export API_HOST="127.0.0.1"
export API_PORT="8000"
export OPENAI_TIMEOUT_SECONDS="60"
```

### Variables

- `OPENAI_API_KEY`: required for model responses
- `API_HOST`: bind host for `jala-daemon`, defaults to `127.0.0.1`
- `API_PORT`: daemon port, defaults to `8000`
- `OPENAI_TIMEOUT_SECONDS`: timeout for model calls, defaults to `60`
- `API_AUTH_TOKEN`: required when binding the daemon to a non-loopback host; clients send it as `Authorization: Bearer <token>`
- `API_TLS_CERT`: path to a PEM certificate file; set together with `API_TLS_KEY` to enable HTTPS on the daemon
- `API_TLS_KEY`: path to the PEM private key for the certificate
- `API_TLS_VERIFY`: set to `0` to skip certificate verification in the client (useful only with self-signed certs during development; always leave at `1` in production)
- `JALA_MAX_OUTPUT_BYTES`: max captured stdout+stderr per command stream, defaults to `65536`
- `JALA_MAX_REQUEST_BYTES`: max accepted HTTP request body size, defaults to `1048576`
- `JALA_MAX_FILE_READ_BYTES`: max bytes returned by the `read_file` tool, defaults to `262144`
- `JALA_MAX_CONCURRENT_REQUESTS`: max in-flight daemon request handlers, defaults to `16`

## Usage

Start the daemon in one terminal:

```bash
./jala-daemon
```

Then send messages from another shell:

```bash
./jala "summarize this repository"
./jala "what did I ask you to do before?"
./jala -s work "summarize this repository"
```

Each `jala` invocation:

- sends your message to `POST /chat`
- includes your current working directory
- uses the default session id unless `--session` / `-s` is provided
- prints the daemon response and exits

## Tool and Approval Model

`Jala` currently separates local actions into two categories:

### Structured read-only tools

These are intended for common inspection tasks and do not require an approval round-trip:

| Tool | Purpose | Shell equivalent |
|---|---|---|
| `read_file` | Read a file's contents | `cat`, `head`, `tail` |
| `list_directory` | List directory contents | `ls` |
| `search_files` | Find files by name or glob | `find -name` |
| `search_file_contents` | Search text or regex inside files | `grep`, `rg` |
| `file_metadata` | Get size, permissions, type, mtime | `stat`, `file` |
| `inspect_process` | Inspect a process by PID | `ps -p <pid>` |
| `list_processes` | List all running processes | `ps aux`, `pgrep` |
| `git_inspect` | Run a read-only git subcommand | `git status/diff/log/…` |

This gives the model safer and more predictable primitives for local inspection. The model is instructed to prefer these tools over `run_shell_command` for any task they can serve.

### Approval-gated shell execution

For actions that require free-form shell execution, the daemon returns an approval block instead of running the command immediately:

```text
[APPROVAL_REQUIRED]
Tools: [run_shell_command]
Approve: jala approve <approval_id>
Deny: jala deny <approval_id>
```

Approve or deny the action with:

```bash
./jala approve <approval_id>
./jala deny <approval_id>
```

Approved actions run in the original working directory captured when the request was created, even if you approve from a different shell later.

## History and Inspection

The daemon records recent request, approval, execution, and event history for local inspection.

You can view history with:

```bash
./jala history
./jala history --session_id work
./jala history --session_id work --event_type user
./jala history --start_time 2025-01-01T00:00:00 --end_time 2025-01-02T00:00:00
```

This is primarily a local debugging and trust-building surface, not yet a full audit platform.

## API Surface

The local daemon currently exposes:

- `GET /`
- `GET /health`
- `GET /history`
- `POST /chat`
- `POST /approve`
- `POST /deny`

`GET /history` supports optional query parameters for:

- `session_id`
- `event_type`
- `start_time`
- `end_time`

Example `POST /chat` request payload:

```json
{
  "message": "summarize this repository",
  "cwd": "/home/user/projects/jala",
  "session_id": "default"
}
```

Example `POST /approve` or `POST /deny` payload:

```json
{
  "approval_id": "abcd1234"
}
```

Successful POST requests currently return:

```json
{
  "response": "..."
}
```

## Internal Layout

- `jala`: thin CLI client
- `jala-daemon`: daemon launcher
- `api/api_server.py`: local HTTP server, persistence, tools, approvals, and history retrieval
- `core/neutral_terminal.py`: OpenAI client integration plus bounded local command execution helpers
- `core/env_config.py`: `.env` loading

The user-facing brand is `Jala`, and the internal Python package names currently use `api` and `core`.

## Testing

The repository includes `pytest` coverage for the current flow, including:

- daemon health checks
- CLI help and error handling
- session continuity across turns
- working-directory propagation
- approval state transitions and command recording
- structured read-only tool execution
- filtered history retrieval and auth handling
- `.env` loading

Run the tests with:

```bash
pytest
```

## Roadmap Direction

The current architecture is the foundation, not the final product shape.

The most likely next steps are:

- expand structured tools and reduce reliance on free-form shell execution
- add command explanation and more educational UX
- support multiple candidate actions or commands when useful
- add a REPL frontend on top of the existing daemon-backed session model
- support registered user-defined tools with schema and policy
- improve session and approval UX
- support multiple model providers and more production-ready credential/configuration handling
- add streaming and long-running task handling over time

The intent is to grow `Jala` into a more capable terminal-aware agent without losing the things that currently differentiate it:

- local daemon ownership of state
- explicit approval for mutations
- durable session memory
- structured local inspection
- inspectable action history

Longer term, that also includes support for multiple providers such as OpenAI, Anthropic, Gemini, and potentially other compatible backends, while keeping the daemon, tool, approval, and persistence model stable.

## Troubleshooting

- Missing API key: set `OPENAI_API_KEY` in `.env` or your shell before starting the daemon.
- Daemon not running: `jala` expects `jala-daemon` to be reachable and exits with a connection error if it is not.
- Remote binding requires auth and TLS: if you set `API_HOST` to a non-loopback host, you must also set `API_AUTH_TOKEN`. Set `API_TLS_CERT` and `API_TLS_KEY` to enable HTTPS directly on the daemon. Without TLS, credentials travel in cleartext.
- Lost conversation history: confirm the daemon is using the same user data directory across restarts.
- Large reads or outputs are truncated intentionally according to runtime limits.
