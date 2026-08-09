# session-control-mcp

Control Claude Code sessions from inside a session: list them, open new ones,
branch off a transcript, close them, restart the current one.

## Install

Via [mcp-orchestrator](https://github.com/mrconter1/mcp-orchestrator), which
also keeps it running:

```
mcp_install("session")
```

Or by hand:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1
claude mcp add --transport http --scope user session http://127.0.0.1:8767/mcp
```

## Tools

| Tool | Destructive | Purpose |
| --- | --- | --- |
| `session_list` | no | Every running session: pid, start time, cwd, terminal |
| `session_info` | no | Server health |
| `session_new` | no | Open a session; `resume` plus `fork` branches off an existing one |
| `session_close` | **yes** | Terminate a session by pid |
| `session_restart` | **yes** | Replace a session, resuming the same transcript |

## Safety

**Do not put these on the permission allowlist.** Every call is meant to
prompt. They kill and spawn real processes, and a forked agent runs while
nobody is watching.

Two guards, both tested: destructive tools refuse any pid that is not a live
`claude.exe`, and `session_new` rejects a bad directory or a fork with nothing
to fork from.

## Worth knowing

- **A tool cannot kill its own caller and still return.** `session_restart`
  hands the kill to a detached helper on a delay, so the tool answers first.
- **Restart is asymmetric.** The replacement opens in a new terminal; the old
  window is left at a shell prompt. Claude Code is a TUI bound to its terminal.
- **Pids cannot be mapped to session ids.** `claude.exe` carries nothing
  linking a process to its transcript. Disambiguate by cwd, start time and
  terminal, and confirm with the user before closing anything.
- **Start it before Claude Code.** A server that is not listening when a
  session starts stays unavailable for that whole session
  ([#31198](https://github.com/anthropics/claude-code/issues/31198)).
  Restarting a server that was already attached is fine.

Windows specifics, each of which cost a debugging round, are handled in
`session_control/procs.py`: the `wt.exe` alias missing from PATH, Windows
Terminal treating `;` as its own separator, `cmd /c start` eating its first
token, `DETACHED_PROCESS` leaving a console app with no console, and session
environment markers leaking into spawned sessions and disabling transcripts.

Config: `SESSION_CONTROL_PORT`, `SESSION_CONTROL_HOST`.

## Status

Verified: discovery, both fail-closed guards, argument validation, all five
tools over HTTP. Not exercised end to end: an actual close or restart, left
untested rather than killing a live session to prove a point.
