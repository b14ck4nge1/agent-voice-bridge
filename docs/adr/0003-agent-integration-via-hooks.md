# ADR-0003: Integrate agents through PermissionRequest hooks

Status: Accepted. Date: 2026-10-20.

## Context

Claude Code and Codex both expose a `PermissionRequest` command hook with the same
decision shape (verified in Claude docs and the Codex schema files). Codex also
exposes `codex app-server`. Alternatives considered: Claude's
`--permission-prompt-tool` (used by linco-bridge), tmux keystroke injection (used by
pingme), and terminal scraping.

## Decision

* One stdlib-only hook executable, `avb-hook --agent claude-code|codex`, is the
  primary integration for both agents.
* It never returns `updatedInput` or `updatedPermissions`: an approval is for one
  unmodified call and adds no standing rules.
* On any failure it denies by default (`--on-failure ask` falls back to the agent's
  own prompt instead).
* `avb-codex` (an app-server client) is secondary, for headless Codex runs we start
  ourselves.

## Consequences

* Works in the interactive CLI. For Codex it should also work in the IDE extension,
  to be verified (CX-04).
* Claude Code's hook timeout (default 600 s; we set 330 s) bounds how long a call can
  take. `avb-hook` gives up at 300 s and cancels the request first.
* Clarifying questions (AskUserQuestion, requestUserInput) need separate work.
