# session-control-mcp

Control Claude Code sessions from inside a Claude Code session: list them,
open new ones, branch one off an existing transcript, close them, restart the
current one.

## Safety

These tools kill and spawn real processes. **Do not add them to the permission
allowlist** - every call is meant to prompt. `~/.claude/settings.json`
deliberately has no `mcp__session__*` entry.

Two guards, both tested:

- `session_close` and `session_restart` refuse any pid that is not a live
  `claude.exe`. A stale or wrong pid fails closed instead of killing something
  unrelated.
- `session_new` rejects a non-existent working directory, and `fork` without
  `resume` (there would be nothing to fork from).

This matters more than usual alongside `queue-mcp`: forked agents run while
nobody is watching, and an agent that can terminate its own runtime is not
something to leave on a wildcard allow.

## Running it

```powershell
cd C:\Users\rasmus.lindahl\Repos\session-control-mcp
.\run.ps1
```

Binds `127.0.0.1:8767` (`SESSION_CONTROL_PORT` / `SESSION_CONTROL_HOST` to
override). Start it **before** launching Claude Code - an HTTP MCP server that
is not listening at startup stays unavailable for the whole session
([#31198](https://github.com/anthropics/claude-code/issues/31198)).

Registered at user scope:

```
claude mcp add --transport http --scope user session http://127.0.0.1:8767/mcp
```

## Tools

| Tool | Destructive | Purpose |
| --- | --- | --- |
| `session_list` | no | Every running session: pid, start time, cwd, owning terminal |
| `session_info` | no | Server health |
| `session_new` | no | Open a session in a new terminal; `resume` + `fork` branches off an existing one |
| `session_close` | **yes** | Terminate a session by pid |
| `session_restart` | **yes** | Open a replacement resuming the same transcript, then kill the old one |

## The two constraints worth knowing

**A tool cannot kill its own caller and still return.** Terminating the
session mid-call would kill the turn before the result arrives. `session_restart`
therefore hands the kill to a detached helper on a delay, so the tool responds
first and the session dies a couple of seconds later.

**Restart is asymmetric.** The replacement opens in a *new* terminal window;
the old window is left sitting at a shell prompt. Claude Code is a TUI bound to
its terminal, and a detached server cannot re-attach a fresh process to a
window it does not own.

**Pids cannot be mapped to session ids.** `claude.exe` carries no arguments, so
there is nothing linking a process to its transcript. Disambiguate by cwd,
start time and terminal - and note that several sessions started from the same
directory in the same Windows Terminal differ only by start time. Always
confirm the pid with the user before closing anything.

## Finding the current session's pid

The server is detached and cannot tell who is calling. Claude finds its own
`claude.exe` by walking the parent chain from a PowerShell tool call - the
shell's parent is the session that spawned it:

```powershell
$p = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
(Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)").ProcessId
```

## Remote Control on startup

Not handled here - it is a native setting. `~/.claude/settings.json` now has
`"remoteControlAtStartup": true`, which starts the Remote Control bridge for
every session. `session_new` also takes `remote_control=true` to pass
`--remote-control` for a one-off.

## Status

Verified: discovery across three live sessions, both fail-closed guards at the
module *and* MCP layer, argument validation, all five tools over HTTP, and
Claude Code reporting the server connected.

Not exercised end to end: an actual close or restart. Both were deliberately
left untested rather than killing a live session to prove a point.
