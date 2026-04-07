# True Terminal Agent Plan

## Purpose

This document describes where `Jala` is today and what should come next to evolve it from a daemon-backed conversational terminal assistant into a more capable, trustworthy, and teachable terminal-aware agent.

The core architectural direction is already settled:

- keep the user's shell prompt free
- send each turn with a short CLI invocation
- let a local daemon own durable state, approvals, and execution records
- preserve explicit user approval for state-changing actions

The work ahead is therefore evolutionary rather than foundational. The right question is no longer "what should the interaction model be?" but "how do we deepen capability without losing safety, clarity, or focus?"

## Current State

`Jala` is already a functioning local terminal agent with a clear execution model.

### What is implemented today

- one-command CLI turns through `jala`
- a persistent local daemon started by `jala-daemon`
- named conversational sessions via `--session` and `-s`
- propagation of the caller's current working directory on every turn
- SQLite-backed persistence for sessions, approvals, requests, command executions, and session events
- structured read-only tools for:
  - `read_file`
  - `list_directory`
  - `search_files`
  - `search_file_contents`
  - `file_metadata`
  - `inspect_process`
  - `list_processes`
  - `git_inspect`
- approval-gated free-form shell execution through `run_shell_command`
- persisted approval status transitions such as `pending`, `executing`, `executed`, `denied`, and `failed`
- a local history surface via `GET /history` and `jala history`
- filtered history retrieval by session, event type, and time range
- bounded request sizes, bounded command output capture, and bounded concurrent request handling
- optional bearer-token auth when binding the daemon to a non-loopback host
- focused automated coverage around the current request, approval, and history flows

### Honest current description

The shortest accurate description of the current system is:

- the daemon-backed architecture is correct
- the conversational session model is durable and useful
- the approval model is real, persisted, and operationally meaningful
- structured read-only inspection is now part of the product, not just shell heuristics
- free-form shell execution exists as an escape hatch rather than the only primitive
- observability is present, but still lightweight
- the UX is still intentionally minimal

This means `Jala` is no longer just a prototype experiment, but it is also not yet a fully mature terminal runtime.

## Architectural Principles To Preserve

The next phase should preserve the aspects that most clearly differentiate `Jala`.

### 1. Local daemon ownership of state

The daemon should remain the source of truth for:

- conversation history
- pending and completed approvals
- execution records
- session event history
- future task or tool metadata
- per-user runtime configuration metadata

This keeps short-lived CLI invocations simple and makes the system more durable and inspectable.

### 2. Explicit approvals for mutations

State-changing actions should continue to require user approval by default.

That principle is central to:

- user trust
- auditability
- recoverability after crashes or restarts
- keeping `Jala` distinct from assistants that are too eager to act

### 3. Prefer structured tools over free-form shell

Whenever a task can be served by a structured local capability, that should be preferred to generic shell execution.

This improves:

- safety
- predictability
- model reliability
- argument validation
- future policy enforcement

### 4. Keep the shell prompt free

The one-command request flow is a real product advantage. Future improvements should build on it rather than collapse back into requiring an always-on full-screen interface for basic use.

### 5. Optimize for trust and learnability, not just raw power

`Jala` should help users do work, but also help them understand what is happening. This is especially important for newer terminal users.

### 6. Avoid unnecessary model calls

The model should be used when semantic interpretation is actually needed, not as a default step for every possible action.

This matters for:

- cost
- latency
- reliability
- auditability
- deterministic behavior for explicit user intent

If the user has already expressed a complete, machine-readable action, `Jala` should prefer deterministic backend handling over an LLM round-trip.

### 7. Stay provider-agnostic where practical

`Jala` should not hard-wire its future to a single model vendor if the surrounding architecture can remain neutral.

This matters for:

- cost flexibility
- availability and uptime
- regional availability
- user preference
- educational and low-budget accessibility
- the ability to use the best model for a given task

The daemon, tool model, approval model, and persistence model should remain stable even if the backing LLM provider changes.

## What Is Still Missing

Despite the progress, `Jala` still has important gaps.

### 1. No richer interactive mode yet

Today the product works well for short turns, but it does not yet provide:

- a conversational REPL
- richer turn-by-turn refinement in one terminal session
- live clarification loops before or during approval
- a more ergonomic experience for longer task sequences

The daemon already supports persistent sessions, so the missing piece is primarily a better frontend experience.

### 2. No explicit alternative-action UX

Right now the system tends toward proposing a single action path.

That leaves room to improve:

- trust
- learnability
- beginner friendliness
- ambiguity handling

In many terminal tasks there is more than one valid command. Newer users in particular benefit from seeing multiple options with explanations.

### 3. No custom user-defined tool registration

The current built-in tools are useful, but serious day-to-day use will eventually require user- or team-specific capabilities.

What is missing is a safe extension model for things like:

- project-specific inspection commands
- build or test helpers
- local operational utilities
- organization-specific wrappers
- personal workflow shortcuts

### 4. No command explanation or teaching mode

The system can execute approved actions, but it does not yet have a first-class way to help the user learn from them.

Useful missing behaviors include:

- explain this command
- break down each flag
- show a safer alternative
- show a more beginner-friendly version
- explain why this option was chosen over another

### 5. No streaming or long-running task model

The current flow is still mostly synchronous.

It does not yet support:

- streaming output to the caller
- task handles for longer-running jobs
- job status retrieval
- follow or tail behavior for running commands
- durable tracking of background work

### 6. Session UX is still minimal

Sessions are functional, but not yet very user-friendly. The product still lacks conveniences such as:

- list sessions
- inspect session summaries
- rename sessions
- delete sessions
- list pending approvals by session
- resume recent work more ergonomically

### 7. No production-grade configuration and credential UX

The current environment loading model is useful for development, but it is not yet a complete long-term configuration strategy for a polished end-user tool.

Important missing pieces include:

- a clearly documented config precedence model
- a per-user config file location for regular use
- separation between development-time `.env` behavior and recommended production usage
- stricter handling of secret-bearing config files
- future support for OS-native secure secret storage
- support for multiple provider credentials in one install
- a user-friendly way to choose a default provider and model

### 8. No deterministic pre-agent routing layer

Today the system has deterministic handling for some top-level CLI commands such as approval and history operations, but it does not yet appear to have a broader explicit routing layer for machine-readable action requests.

That means there is still no formal backend path for requests like:

- exact command execution requests
- explicit command explanation requests
- future structured action verbs such as delete, move, or copy
- future admin commands like session listing or approval inspection

These cases should not require model interpretation if the user's intent is already explicit and fully specified.

### 9. No multi-provider model abstraction yet

Today the implementation is centered on a single provider path.

What is still missing includes:

- a provider-neutral model interface inside the daemon
- configuration for multiple providers and multiple API keys
- model capability metadata such as tool-use support, context limits, streaming support, and cost characteristics
- provider fallback or graceful degradation behavior
- a strategy for choosing lower-cost defaults for beginner and low-budget users
- support for at least major likely providers such as OpenAI, Anthropic, Gemini, and potentially DeepSeek-compatible backends

Without that layer, future provider support will be harder to add cleanly.

## Comparison-Informed Direction

Looking at adjacent tools in this space suggests a useful direction for `Jala`.

### What to preserve from `Jala` itself

`Jala`'s strongest differentiators are:

- daemon-backed persistence
- explicit approval workflow
- structured history and event records
- local-agent architecture
- separation between read-only inspection and approval-gated mutation

These should remain the center of gravity.

### What to borrow selectively from command-suggestion tools

One especially valuable idea is offering multiple candidate actions when a task is ambiguous or educationally useful.

That feature helps with:

- trust
- user control
- beginner learning
- comparing safety and complexity
- reducing overcommitment to one LLM-chosen command

But it should be integrated into `Jala`'s approval and session model, not replace it.

### What to borrow selectively from broader AI CLI assistants

Two ideas stand out as high value:

- a REPL or conversational interactive mode
- user-defined tools or functions

Both fit `Jala` well, but they should be implemented in a way that reinforces structured execution, policy, and auditability rather than allowing unconstrained extension.

### What to strengthen beyond both styles

`Jala` should also lean harder into two areas many adjacent tools under-emphasize:

- deterministic routing for explicit actions
- production-ready config and secret handling

These are not flashy features, but they matter directly to usability, cost, and trust.

## Target End State

The desired end state for `Jala` is:

- callable from any directory with a short command
- aware of project context and prior conversation state
- able to inspect the local environment through structured read-only tools
- able to reason across multiple tool results in one turn
- able to propose one or more possible actions when appropriate
- able to explain proposed actions clearly
- able to execute only what the user explicitly approves
- able to skip model calls when user intent is already explicit and machine-readable
- able to record what was proposed, approved, denied, executed, and returned
- able to support longer workflows without abandoning the CLI-plus-daemon model
- able to support a production-grade per-user configuration model
- able to switch among multiple supported model providers and credentials

A concise product statement for that future is:

> `Jala` is a local, stateful terminal agent that can inspect your environment, remember context, propose safe actions, explain them, execute only what you explicitly approve, and avoid unnecessary model calls when the request is already explicit.

## Recommended Roadmap

The next phase should deepen user value without weakening the current safety and architecture model.

### Phase 1: Improve tool semantics and structured capabilities

Status: **complete**.

Goal: reduce reliance on free-form shell execution by expanding clear structured capabilities.

Work completed:

- structured tools now cover the most common read-only inspection tasks:
  - `read_file` — replaces `cat`, `head`, `tail`
  - `list_directory` — replaces `ls`
  - `search_files` — replaces `find -name`
  - `search_file_contents` — replaces `grep`, `rg`; bounded by per-file size and file-count caps
  - `file_metadata` — replaces `stat`, `file`
  - `inspect_process` — replaces `ps -p <pid>`
  - `list_processes` — replaces `ps aux`, `pgrep`
  - `git_inspect` — replaces common `git` inspection subcommands with an allowlist
- argument validation and error messaging improved and consistent across all tools
- system prompt expanded with explicit per-tool guidance and fallback rules
- `run_shell_command` retained as the approval-gated escape hatch only
- each mutating tool call gets its own approval record so every approval is atomic
- `_tool_succeeded()` helper centralises the tool-result protocol check
- `/health` and `/` now enforce auth when `API_AUTH_TOKEN` is configured
- TLS warning emitted at startup when binding to a non-loopback host
- SQLite connections use WAL mode and `busy_timeout` to reduce lock contention
- `/chat` errors categorised into persistence failures, tool-loop exhaustion, and AI backend errors
- `jala history` with unrecognised args now exits with an error instead of falling through to chat

Expected outcome (achieved):

- safer execution paths
- better model consistency
- fewer unnecessary shell calls
- clearer boundaries between inspection and mutation

### Phase 2: Add command explanation and educational UX

Status: not yet implemented.

Goal: make `Jala` more useful for learning and trust-building.

Work:

- support asking for explanations of a proposed or executed command
- let users ask what each flag does
- let users ask for safer, simpler, or more portable variants
- support a beginner-friendly explanation style in replies
- record explanatory responses in session history like any other turn

Expected outcome:

- stronger trust in the system
- better onboarding for less experienced users
- improved usefulness even when the user decides not to execute anything

This phase is especially important if `Jala` is meant to help users grow more confident with Linux and shell workflows.

### Phase 3: Add alternative action proposals

Status: not yet implemented.

Goal: let the daemon present several plausible actions instead of forcing one answer in ambiguous cases.

Work:

- support returning 2-4 candidate commands or plans when appropriate
- annotate each option with:
  - what it does
  - whether it is read-only or mutating
  - relative simplicity or risk
  - why it may be preferred
- integrate alternatives into the approval model
- allow the user to choose one option, request another, or ask follow-up questions

Expected outcome:

- better user control
- better educational value
- safer handling of ambiguous prompts
- a clearer distinction between "assistant recommendation" and "user-approved action"

This should be implemented as an optional or context-sensitive behavior, not as the only response style.

### Phase 4: Introduce an interactive REPL mode

Status: not yet implemented.

Goal: make longer conversations and iterative task refinement much more ergonomic.

Work:

- add a REPL frontend on top of the existing daemon session model
- let users continue an existing named session interactively
- support asking follow-up questions before approval
- preserve the one-command flow for simple usage while offering a richer interactive mode when desired

Expected outcome:

- better experience for multi-step work
- more natural iterative refinement
- stronger leverage from the existing persistent session architecture

The REPL should be viewed as an additional frontend, not a replacement for the existing transport-style CLI.

### Phase 5: Add deterministic action routing and model bypass

Status: not yet implemented.

Goal: avoid unnecessary model calls when the user has already provided a complete, machine-readable action.

Work:

- add a routing layer before the agent/model call
- distinguish between:
  - deterministic CLI/admin commands
  - deterministic action requests
  - agent requests that require semantic interpretation
- support direct backend handling for explicit requests such as:
  - exact command execution
  - exact command explanation
  - future explicit approval-intent execution paths
  - future structured admin and lifecycle actions
- send deterministic actions through the same approval, policy, persistence, and audit machinery as agent-proposed actions
- keep the model path for open-ended, ambiguous, or reasoning-heavy requests
- avoid inventing a broad ad hoc shell replacement language; only bypass the model when intent is explicit

Expected outcome:

- lower cost
- lower latency
- fewer avoidable model failures
- clearer auditability for explicit actions
- a cleaner separation between "interpret what the user wants" and "carry out what the user already specified"

Longer term, this phase should also encourage internal structured action representations where possible, such as `delete_files(paths=[...])`, instead of always reducing explicit requests to a shell string.

### Phase 6: Add registered user-defined tools

Status: not yet implemented.

Goal: allow safe extension of `Jala` for local and team workflows.

Work:

- define a registration model for custom tools
- require each tool to declare:
  - name
  - description
  - argument schema
  - execution target
  - read-only vs mutating classification
  - timeout
  - output limits
  - approval policy
- store tool metadata in a durable and inspectable way
- present custom tools to the model alongside built-in ones
- keep policy enforcement on the daemon side

Expected outcome:

- much better real-world usefulness
- project-specific adaptability
- extension without abandoning structured execution

This should not be implemented as unrestricted arbitrary code that the model can invoke freely. The point is extensibility with policy, not extensibility without boundaries.

### Phase 7: Strengthen session and approval UX

Status: partly implemented in the backend, minimal in the frontend.

Goal: make the persisted state model easy to use day to day.

Work:

- list sessions
- summarize recent sessions
- show pending approvals by session
- surface recent command executions more clearly
- add rename and delete workflows for sessions
- make approval records easier to inspect before acting

Expected outcome:

- stronger everyday usability
- better multi-project workflow support
- more value from the persistence architecture already in place

### Phase 8: Add production configuration and credential management

Status: not yet implemented as a complete product-facing model.

Goal: make `Jala` practical and predictable for regular personal use and eventual production-grade distribution.

Work:

- define a clear precedence model for configuration sources
- keep environment variables supported
- introduce a per-user config location such as `~/.config/jala/`
- treat project-local `.env` loading as a development convenience rather than the primary recommended production secret store
- define how daemon auth tokens and model credentials are stored and loaded
- support multiple provider credentials in one user config
- let the user choose a default provider and model, while still allowing per-request or per-session overrides later
- ensure secret-bearing files are created with restrictive permissions
- document migration from dev-style `.env` usage to per-user configuration
- consider optional OS keychain integration in a later stage
- keep setup simple enough for low-budget and beginner users on older hardware

Expected outcome:

- better production UX
- less surprising configuration behavior
- safer secret handling
- clearer separation between project config and user config
- a stronger foundation for future login/config CLI workflows

### Phase 9: Add multi-provider model support

Status: not yet implemented.

Goal: support multiple LLM backends without forcing changes to the rest of `Jala`'s architecture.

Work:

- define a provider abstraction around chat, tool use, streaming, and error handling
- add provider-specific adapters for likely targets such as:
  - OpenAI
  - Anthropic
  - Gemini
  - DeepSeek-compatible APIs where appropriate
- define model capability metadata so `Jala` can understand:
  - which models support tool calling
  - which support streaming well
  - context window tradeoffs
  - cost tradeoffs
  - latency tradeoffs
  - authentication requirements and supported credential types
- allow provider selection by config, and later by session or request
- support multiple provider credentials in one install
- define fallback behavior when a preferred provider is unavailable
- prefer architecture that can later support local-model backends without rewriting the daemon contract
- document a sane default provider strategy for:
  - best quality
  - best cost
  - best beginner friendliness

Expected outcome:

- more provider flexibility
- less vendor lock-in
- better resilience
- better affordability options
- a cleaner path to future local or hybrid backends
- clearer rules for how credentials and provider capabilities are represented

This phase should keep the daemon, approval, persistence, and tool semantics stable while swapping only the provider-facing integration layer.

### Phase 10: Add long-running task and streaming support

Status: not yet implemented.

Goal: make `Jala` feel more like a real terminal assistant for non-trivial operations.

Work:

- stream model responses where practical
- stream command output for approved tasks
- distinguish short-lived tasks from long-running jobs
- assign task ids or handles for follow-up inspection
- support checking status, fetching output, and cancelling where appropriate

Expected outcome:

- less artificial synchrony
- better support for builds, tests, logs, and other real workflows
- a path toward richer terminal-runtime behavior

### Phase 11: Revisit policy and security boundaries

Status: basic local-use baseline exists.

Goal: move from "reasonable for local development" toward "deliberate execution platform."

Work:

- define a clearer policy engine for what can auto-run
- improve audit and export behavior for execution history
- revisit retention rules for persisted state
- consider sandboxing or constrained execution options
- keep optional auth for non-loopback binding, and improve remote-use guidance if that ever expands

Expected outcome:

- stronger safety guarantees
- clearer operator expectations
- a firmer foundation for advanced automation

## Credential Strategy Notes

`Jala` should aim to be as beginner-friendly and economy-friendly as possible, but it should also be realistic about provider authentication models.

### API keys

API keys are the most practical near-term integration path because they are the most commonly supported mechanism for programmatic access across providers.

That makes them the default implementation target for:

- OpenAI-style APIs
- Anthropic APIs
- Gemini APIs
- DeepSeek-style APIs and compatible gateways
- many future hosted model providers

### OAuth-style consumer credentials

Using a user's consumer chat account credentials, such as a web-login-style ChatGPT or Gemini account session, is generally not the right primary design target for `Jala`.

Reasons include:

- those credentials are usually not intended for third-party local agent apps
- official support for general-purpose local CLI access may be absent, limited, or unstable
- terms of service may differ from API access
- reliability and maintainability are much worse than supported API integrations
- it creates more security and support complexity

So, as a planning assumption:

- official API access should be the primary supported path
- consumer-account OAuth should not be assumed to exist
- if an official provider later exposes a stable and documented OAuth flow for third-party local apps, it can be evaluated then
- provider adapters should explicitly model whether they support API keys, OAuth, or other credential types rather than assuming one universal auth method

### Beginner and low-budget friendliness

To stay friendly to users with limited money or older hardware, `Jala` should eventually support:

- lower-cost hosted providers
- explicit cost-aware defaults
- clear documentation about likely pricing tradeoffs
- provider selection that does not require code changes
- future local-model support where practical, while acknowledging that local inference on older hardware may still be limited

The goal should be to let users choose among quality, cost, and hardware constraints without changing the core `Jala` workflow.

## Immediate Priorities

The most valuable next additions are not all equal. The recommended near-term priorities are:

1. improve structured tools and reduce shell reliance further
2. add command explanation and educational responses
3. add alternative action proposals
4. add deterministic action routing and model bypass
5. add an interactive REPL mode
6. add registered user-defined tools
7. define production configuration and credential management
8. prepare the provider abstraction needed for multi-provider support

That ordering balances:

- user value
- architectural coherence
- differentiation
- implementation risk
- ongoing operating cost

## Suggested Success Criteria

`Jala` should be considered materially closer to its target state once it can demonstrate:

- reliable one-command invocation from arbitrary directories
- multi-step read-only inspection through structured tools
- consistent approval gating for mutating actions
- durable records for proposals, approvals, denials, executions, and outcomes
- explanatory command UX that helps users understand recommended actions
- optional presentation of multiple action paths where ambiguity or teaching value is high
- deterministic backend handling for explicit action requests that do not need semantic interpretation
- at least one richer interactive frontend without abandoning the daemon-backed model
- a safe, schema-driven extension path for user-defined tools
- a clear and production-appropriate per-user configuration and credential model
- a provider-neutral model layer that can support multiple backends cleanly

## Non-Goals For The Next Phase

The next phase should not try to:

- replace the user's shell
- silently auto-run broad classes of mutations
- sprawl into a generic "does everything" AI assistant
- overcomplicate the CLI before backend semantics are stronger
- add remote multi-user infrastructure prematurely
- weaken the central approval and audit story for the sake of convenience
- invent a large custom shell DSL just to avoid model calls
- assume unsupported consumer-account OAuth flows will be available from providers

## Conclusion

`Jala` already has the right architecture: a short-lived CLI paired with a persistent local daemon that owns conversation state, approvals, and execution records.

The next stage is about deepening that architecture in the right ways:

- more structured tools
- better educational UX
- multiple action options when useful
- deterministic routing for explicit requests
- a richer interactive mode
- registered custom tools
- production-ready configuration and credential handling
- multi-provider model support
- stronger task and policy semantics over time

If those are added carefully, `Jala` can differentiate itself clearly as a local, stateful, approval-centered terminal agent rather than merely another shell-oriented LLM wrapper.
