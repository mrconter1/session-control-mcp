"""HTTP MCP server for controlling Claude Code sessions.

    .venv\\Scripts\\python.exe -m session_control.server

Binds 127.0.0.1 only. These tools kill and spawn Claude Code processes, so
none of them should be added to the permission allowlist -- every call is meant
to prompt.

The server is a detached process: it cannot tell which session is calling it.
Callers pass ``claude_pid`` explicitly, which Claude finds by walking its own
shell's parent chain (its PowerShell tool runs as a child of claude.exe).
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from session_control import procs

HOST = os.environ.get("SESSION_CONTROL_HOST", "127.0.0.1")
PORT = int(os.environ.get("SESSION_CONTROL_PORT", "8767"))

server = MCPServer(
    "session",
    instructions=(
        "Controls Claude Code sessions: list, close, restart, and branch new "
        "ones. Destructive -- session_close and session_restart terminate real "
        "processes and lose any unsaved turn. Always call session_list first "
        "and confirm the target pid with the user before closing or "
        "restarting. To find the current session's pid, walk the parent chain "
        "from a PowerShell tool call: the shell's parent is its claude.exe."
    ),
)


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


@server.tool()
def session_list() -> dict[str, Any]:
    """List every running Claude Code session (pid, start time, cwd, terminal).

    claude.exe carries no session id on its command line, so pids cannot be
    mapped to transcript ids. Disambiguate by cwd, start time and terminal.
    """
    sessions = procs.list_sessions()
    return _ok(sessions=sessions, count=len(sessions))


@server.tool()
def session_new(
    cwd: str | None = None,
    resume: str | None = None,
    fork: bool = False,
    remote_control: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    """Open a new Claude Code session in its own terminal window.

    Pass resume=<session-id> with fork=true to branch a new session off an
    existing transcript, leaving the original untouched. Non-destructive.
    """
    try:
        return _ok(**procs.spawn_session(cwd, resume, fork, remote_control, name))
    except (ValueError, OSError) as exc:
        return _err(str(exc))


@server.tool()
def session_close(pid: int, force: bool = False) -> dict[str, Any]:
    """Terminate a Claude Code session by pid. DESTRUCTIVE.

    Refuses any pid that is not a live claude.exe. Confirm the target with the
    user first -- an in-flight turn is lost.
    """
    try:
        return _ok(**procs.close_session(pid, force))
    except procs.NotAClaudeProcess as exc:
        return _err(str(exc))


@server.tool()
def session_restart(
    claude_pid: int,
    session_id: str,
    cwd: str | None = None,
    remote_control: bool = False,
    delay_ms: int = 2500,
) -> dict[str, Any]:
    """Restart a session: open a replacement resuming session_id, then kill the old one.

    DESTRUCTIVE and asymmetric -- the replacement opens in a NEW terminal
    window; the old window is left at a shell prompt. Use this to pick up
    changed settings or a newly started MCP server without losing the thread.

    The kill is delayed and detached so this tool can return before its own
    caller dies.
    """
    try:
        spawned = procs.spawn_session(cwd, resume=session_id, fork=False, remote_control=remote_control)
        killer = procs.schedule_kill(claude_pid, delay_ms)
    except (procs.NotAClaudeProcess, ValueError, OSError) as exc:
        return _err(str(exc))
    return _ok(
        replacement=spawned,
        killing_pid=claude_pid,
        killer_pid=killer,
        delay_ms=delay_ms,
        note="Replacement opens in a new terminal; this session dies shortly.",
    )


@server.tool()
def session_info() -> dict[str, Any]:
    """Server health and environment."""
    return _ok(host=HOST, port=PORT, python=procs.python_version(), sessions=len(procs.list_sessions()))


def main() -> None:
    print(f"session-control-mcp listening on http://{HOST}:{PORT}/mcp")
    server.run(transport="streamable-http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
