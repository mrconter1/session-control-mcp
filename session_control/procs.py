"""Process discovery, spawning and termination for Claude Code sessions.

Everything destructive in here is gated on the target actually being a
``claude.exe``. The server hands out PIDs from ``list_sessions`` and refuses to
touch anything else, so a wrong or stale PID fails closed instead of killing an
unrelated process.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil

CLAUDE_EXE = "claude.exe"

# Detach spawned terminals so they outlive this server.
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
# A console app needs a console of its own; DETACHED_PROCESS gives it none, so
# anything launched without a terminal wrapper has to ask for a new window.
NEW_CONSOLE = 0x00000010 | 0x00000200  # CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP


class NotAClaudeProcess(Exception):
    """Raised when a PID is not a live Claude Code process."""


def _proc(pid: int) -> psutil.Process:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess as exc:
        raise NotAClaudeProcess(f"no process with pid {pid}") from exc
    if proc.name().lower() != CLAUDE_EXE:
        raise NotAClaudeProcess(f"pid {pid} is {proc.name()}, not {CLAUDE_EXE} -- refusing")
    return proc


def _describe(proc: psutil.Process) -> dict[str, Any]:
    info: dict[str, Any] = {"pid": proc.pid}
    for key, getter in (
        ("started", lambda: proc.create_time()),
        ("cwd", lambda: proc.cwd()),
        ("parent_pid", lambda: proc.ppid()),
    ):
        try:
            info[key] = getter()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            info[key] = None
    if info.get("started"):
        import datetime

        info["started"] = datetime.datetime.fromtimestamp(info["started"]).isoformat(timespec="seconds")
    # Which terminal window owns it -- the only practical way to tell two
    # same-directory sessions apart, since claude.exe carries no session id
    # on its command line.
    try:
        parent = psutil.Process(proc.ppid())
        grandparent = psutil.Process(parent.ppid())
        info["terminal"] = f"{grandparent.name()} ({grandparent.pid})"
    except (psutil.AccessDenied, psutil.NoSuchProcess, ValueError):
        info["terminal"] = None
    return info


def list_sessions() -> list[dict[str, Any]]:
    """Every live claude.exe, newest first."""
    out = []
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == CLAUDE_EXE:
                out.append(_describe(proc))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return sorted(out, key=lambda s: s.get("started") or "", reverse=True)


def _terminal_command(inner: str, title: str, working_dir: str) -> tuple[list[str], int]:
    """Wrap a command so it opens in a visible, persistent terminal.

    Returns ``(argv, creationflags)``. Two Windows traps are avoided here:

    * Windows Terminal parses ``;`` as its own command separator, so ``inner``
      must never contain one -- the directory comes from wt's ``-d`` flag
      rather than from a ``Set-Location`` prefix.
    * ``cmd /c start`` reads its first *unquoted* token as the program to run,
      not as a window title, so ``start fork-test powershell ...`` tries to
      execute ``fork-test``. ``start`` is therefore not used at all; a window
      is requested with CREATE_NEW_CONSOLE and the directory via Popen's cwd.
    """
    wt = shutil.which("wt.exe") or shutil.which("wt")
    if wt:
        return ([wt, "new-tab", "--title", title, "-d", working_dir,
                 "powershell", "-NoExit", "-Command", inner], DETACHED)
    safe_title = title.replace("'", "''")
    titled = f"$host.UI.RawUI.WindowTitle='{safe_title}'; {inner}"
    return (["powershell.exe", "-NoExit", "-Command", titled], NEW_CONSOLE)


def spawn_session(
    cwd: str | None = None,
    resume: str | None = None,
    fork: bool = False,
    remote_control: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    """Open a new Claude Code session in its own terminal.

    ``resume`` + ``fork`` is how you branch a new session off an existing one:
    it resumes that transcript but assigns a fresh session id, leaving the
    original intact.
    """
    if fork and not resume:
        raise ValueError("fork requires resume -- there is nothing to fork from")
    working_dir = Path(cwd).expanduser() if cwd else Path.home()
    if not working_dir.is_dir():
        raise ValueError(f"not a directory: {working_dir}")

    argv = ["claude"]
    if resume:
        argv += ["--resume", resume]
    if fork:
        argv.append("--fork-session")
    if remote_control:
        argv.append("--remote-control")
    if name:
        argv += ["--name", name]

    inner = " ".join(argv)
    title = name or ("claude-fork" if fork else "claude")
    term_argv, flags = _terminal_command(inner, title, str(working_dir))
    proc = subprocess.Popen(  # noqa: S603 -- argv is built from validated parts
        term_argv,
        cwd=str(working_dir),
        creationflags=flags,
        close_fds=True,
    )
    return {"launcher_pid": proc.pid, "cwd": str(working_dir), "command": " ".join(argv)}


def close_session(pid: int, force: bool = False) -> dict[str, Any]:
    """Terminate one Claude Code session."""
    proc = _proc(pid)
    described = _describe(proc)
    proc.kill() if force else proc.terminate()
    try:
        proc.wait(timeout=5)
        exited = True
    except psutil.TimeoutExpired:
        exited = False
    return {**described, "exited": exited, "forced": force}


def schedule_kill(pid: int, delay_ms: int = 2000) -> int:
    """Kill ``pid`` after a delay, from a detached helper.

    A tool cannot terminate its own caller and still return a result -- the
    turn dies mid-call. So the kill is handed to a process that outlives this
    one, letting the tool respond first.
    """
    _proc(pid)  # validate before scheduling; fail closed on a bad pid
    script = f"Start-Sleep -Milliseconds {int(delay_ms)}; Stop-Process -Id {int(pid)} -Force"
    helper = subprocess.Popen(  # noqa: S603
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=DETACHED,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return helper.pid


def python_version() -> str:
    return sys.version.split()[0]
