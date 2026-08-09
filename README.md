# session-control-mcp

Control Claude Code sessions from inside a Claude Code session: list them, open
new ones, branch one off an existing transcript, close them, restart the current
one.

HTTP MCP server on `127.0.0.1:8767`. Installable through
[mcp-orchestrator](https://github.com/mrconter1/mcp-orchestrator), which will
start it hidden at logon and keep it running.

## Safety

These tools kill and spawn real processes. **Do not add them to the permission
allowlist.** Every call is meant to prompt.

Two guards, both tested:

- `session_close` and `session_restart` refuse any pid that is not a live
  `claude.exe`, so a stale or wrong pid fails closed instead of killing
  something unrelated.
- `session_new` rejects a non-existent working directory, and `fork` without
  `resume`, since there would be nothing to fork from.

This matters more than usual next to a task queue: forked agents run while
nobody is watching, and an agent that can terminate its own runtime is not
something to leave on a wildcard allow.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1
claude mcp add --transport http --scope user session http://127.0.0.1:8767/mcp
```

`SESSION_CONTROL_PORT` and `SESSION_CONTROL_HOST` override the defaults.

Start it **before** launching Claude Code. An HTTP MCP server that is not
listening when a session starts stays unavailable for that whole session
([#31198](https://github.com/anthropics/claude-code/issues/31198)). Restarting a
server that was already attached is fine; the client reconnects per call.

## Tools

| Tool | Destructive | Purpose |
| --- | --- | --- |
| `session_list` | no | Every running session: pid, start time, cwd, owning terminal |
| `session_info` | no | Server health |
| `session_new` | no | Open a session in a new terminal; `resume` plus `fork` branches off an existing one |
| `session_close` | **yes** | Terminate a session by pid |
| `session_restart` | **yes** | Open a replacement resuming the same transcript, then kill the old one |

## Three constraints worth knowing

**A tool cannot kill its own caller and still return.** Terminating the session
mid-call kills the turn before the result arrives. `session_restart` hands the
kill to a detached helper on a delay, so the tool responds first and the session
dies a couple of seconds later.

**Restart is asymmetric.** The replacement opens in a new terminal window and
the old window is left at a shell prompt. Claude Code is a TUI bound to its
terminal, and a detached server cannot re-attach a fresh process to a window it
does not own.

**Pids cannot be mapped to session ids.** `claude.exe` carries no arguments
linking a process to its transcript. Disambiguate by cwd, start time and
terminal, and note that several sessions started from the same directory in the
same Windows Terminal differ only by start time. Confirm the pid with the user
before closing anything.

To find the calling session's own pid, walk the parent chain from a shell tool
call: the shell's parent is the `claude.exe` that spawned it.

## Windows notes

Several traps are handled in `session_control/procs.py`, each of which cost a
debugging round: `wt.exe` is an App Execution Alias missing from PATH, Windows
Terminal parses `;` as its own separator, `cmd /c start` reads its first
unquoted token as the program name, and `DETACHED_PROCESS` gives a console app
no console so it dies invisibly. The session environment markers are also
scrubbed before spawning, or the child treats itself as a child session and
turns transcript saving off.

## Status

Verified: discovery across live sessions, both fail-closed guards at the module
and MCP layers, argument validation, all five tools over HTTP, and Claude Code
reporting the server connected.

Not exercised end to end: an actual close or restart, left untested rather than
killing a live session to prove a point.
