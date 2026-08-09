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
def session_close(pid: int, force: bool = False, close_tab: bool = True) -> dict[str, Any]:
    """Terminate a Claude Code session by pid, and its terminal tab. DESTRUCTIVE.

    Refuses any pid that is not a live claude.exe. Confirm the target with the
    user first -- an in-flight turn is lost.

    close_tab also terminates the shell hosting the session, so the tab closes
    instead of lingering at a prompt. Set it False to keep a shell the user is
    still working in.
    """
    try:
        return _ok(**procs.close_session(pid, force, close_tab))
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
    """Restart a session: kill it, then open a replacement resuming session_id.

    DESTRUCTIVE. The old session is killed first and the replacement opens as a
    new tab a moment later. Use this to pick up changed settings or a newly
    started MCP server without losing the thread.

    Killing first is deliberate. A resumed session keeps its id, so overlapping
    the two means two workers claim one session id and Remote Control evicts
    one, leaving /rc broken in the new tab. Both steps run in a detached helper
    so this tool can return before its own caller dies.

    Whether the old tab closes depends on how it was started. A tab this server
    opened exits cleanly and closes itself; a tab started by hand returns to a
    shell prompt and stays.
    """
    try:
        result = procs.replace_session(
            claude_pid, session_id, cwd=cwd, remote_control=remote_control, delay_ms=delay_ms
        )
    except (procs.NotAClaudeProcess, ValueError, OSError) as exc:
        return _err(str(exc))
    return _ok(
        **result,
        delay_ms=delay_ms,
        note="This session dies shortly, then the replacement opens in a new tab.",
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
