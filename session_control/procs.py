"""Process discovery, spawning and termination for Claude Code sessions.

Everything destructive in here is gated on the target actually being a
``claude.exe``. The server hands out PIDs from ``list_sessions`` and refuses to
touch anything else, so a wrong or stale PID fails closed instead of killing an
unrelated process.
"""

from __future__ import annotations

import os
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
# For a helper that must run unseen: a console, just not a visible one. The same
# rule applies as above -- DETACHED_PROCESS gives powershell.exe no console at
# all, so it reports a pid, exits 0 and never executes a line. Measured: a
# detached helper's side effect never happened; the same script under
# CREATE_NO_WINDOW ran every time.
HELPER = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


# Markers Claude Code exports inside a live session. This server is itself
# started from a session, so without scrubbing they reach the claude we spawn --
# which then treats itself as a *child* session and silently turns transcript
# saving off, leaving the new session impossible to resume later.
SESSION_ENV_MARKERS = (
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_PID",
    "CLAUDECODE",
)


def _clean_env() -> dict[str, str]:
    """A copy of the environment with this session's markers removed."""
    env = os.environ.copy()
    for key in SESSION_ENV_MARKERS:
        env.pop(key, None)
    return env


# Shells that host a session inside a terminal tab. Killing the session's shell
# is what makes the tab close. WindowsTerminal.exe is deliberately absent: it
# owns *every* tab, so terminating it would take the whole window down.
SHELL_HOSTS = frozenset({"powershell.exe", "pwsh.exe", "cmd.exe"})


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


def _find_wt() -> str | None:
    """Locate wt.exe.

    ``shutil.which`` alone is not enough: Windows Terminal ships as an App
    Execution Alias in %LOCALAPPDATA%\\Microsoft\\WindowsApps, and that
    directory is missing from PATH in some environments -- including the one
    this server runs in, which is why spawns silently fell back to opening a
    separate console window instead of a tab.
    """
    found = shutil.which("wt.exe") or shutil.which("wt")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Microsoft" / "WindowsApps" / "wt.exe"
        if candidate.exists():
            return str(candidate)
    return None


def _exit_zero(inner: str) -> str:
    """Wrap a command so its shell exits 0 however the command ends.

    Windows Terminal's default ``closeOnExit`` is ``graceful``: the tab closes
    only when the process exits with code 0. A session that is killed (or that
    exits non-zero for any reason) would otherwise leave the tab sitting on
    ``[process exited with code ...]``.

    ``try{...}finally{...}`` is used rather than ``...; exit 0`` because a
    command string handed to wt must not contain a semicolon -- wt would read
    it as its own subcommand separator.
    """
    return f"try{{{inner}}}finally{{exit 0}}"


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
    wt = _find_wt()
    if wt:
        # -w 0 targets the current Windows Terminal window, so the session
        # arrives as another tab rather than in a window of its own.
        # No -NoExit, and _exit_zero so the shell reports success: Windows
        # Terminal's default closeOnExit is "graceful", which closes the tab
        # only on exit code 0.
        return ([wt, "-w", "0", "new-tab", "--title", title, "-d", working_dir,
                 "powershell", "-Command", _exit_zero(inner)], DETACHED)
    safe_title = title.replace("'", "''")
    titled = f"$host.UI.RawUI.WindowTitle='{safe_title}'; {_exit_zero(inner)}"
    return (["powershell.exe", "-Command", titled], NEW_CONSOLE)


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
        env=_clean_env(),
        creationflags=flags,
        close_fds=True,
    )
    return {"launcher_pid": proc.pid, "cwd": str(working_dir), "command": " ".join(argv)}


def close_session(pid: int, force: bool = False, close_tab: bool = True) -> dict[str, Any]:
    """Terminate one Claude Code session, and by default its terminal tab.

    Killing ``claude.exe`` alone leaves the shell that launched it sitting at a
    prompt, so the tab stays open. With ``close_tab`` the hosting shell is
    terminated too and the tab goes away. Only a shell in ``SHELL_HOSTS`` is
    ever touched, so a session launched from something else is left alone.

    Pass ``close_tab=False`` to keep the shell -- useful when the session was
    started from a terminal the user is still working in.
    """
    proc = _proc(pid)
    described = _describe(proc)

    host = None
    if close_tab:
        try:
            candidate = psutil.Process(proc.ppid())
            if candidate.name().lower() in SHELL_HOSTS:
                host = candidate
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            host = None

    proc.kill() if force else proc.terminate()
    try:
        proc.wait(timeout=5)
        exited = True
    except psutil.TimeoutExpired:
        exited = False

    # Do NOT kill the host shell to close the tab. Terminating it gives it a
    # non-zero exit code, and closeOnExit=graceful then keeps the tab open on
    # "[process exited with code ...]" -- the opposite of what is wanted. A
    # shell spawned by this server ends with exit 0 by itself once its session
    # dies, and Windows Terminal closes the tab on that.
    host_exit = None
    if host is not None:
        try:
            host_exit = host.wait(timeout=5)
        except psutil.TimeoutExpired:
            # Not one of ours (a shell started with -NoExit, say). Leave it be
            # unless asked to clean it up; killing it will not close the tab.
            if close_tab:
                try:
                    host.terminate()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            host_exit = None

    return {
        **described,
        "exited": exited,
        "forced": force,
        "host_exit_code": host_exit,
        "tab_closed": host_exit == 0,
    }


def replace_session(
    pid: int,
    session_id: str,
    cwd: str | None = None,
    remote_control: bool = False,
    delay_ms: int = 2500,
) -> dict[str, Any]:
    """Kill a session, then open its replacement, from one detached helper.

    The order matters and was learned the hard way. Spawning first left the old
    and new sessions alive at the same time, and because a resumed session
    keeps its id, two workers claimed one session id: Remote Control evicted
    one of them and ``/rc`` failed in the new tab with code 4090.

    The cost is a window where neither session exists. If the spawn fails the
    transcript is still on disk and ``claude --resume <id>`` brings it back,
    which is the better trade.
    """
    _proc(pid)  # validate before scheduling; fail closed on a bad pid
    working_dir = Path(cwd).expanduser() if cwd else Path.home()
    if not working_dir.is_dir():
        working_dir = Path.home()

    argv = ["claude", "--resume", session_id]
    if remote_control:
        argv.append("--remote-control")
    inner = " ".join(argv)

    steps = [
        f"Start-Sleep -Milliseconds {int(delay_ms)}",
        f"Stop-Process -Id {int(pid)} -Force -ErrorAction SilentlyContinue",
        # Give the old worker's socket time to close before the replacement
        # claims the same session id, or the eviction happens the other way.
        "Start-Sleep -Milliseconds 1200",
    ]

    wt = _find_wt()
    if wt:
        steps.append(
            f"& {_ps_quote(wt)} -w 0 new-tab --title claude -d {_ps_quote(str(working_dir))} "
            f"powershell -Command {_ps_quote(_exit_zero(inner))}"
        )
    else:
        steps.append(
            f"Start-Process powershell.exe -WorkingDirectory {_ps_quote(str(working_dir))} "
            f"-ArgumentList '-Command',{_ps_quote(_exit_zero(inner))}"
        )

    helper = subprocess.Popen(  # noqa: S603 -- every interpolated value is quoted
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "; ".join(steps)],
        cwd=str(working_dir),
        env=_clean_env(),
        creationflags=HELPER,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "helper_pid": helper.pid,
        "killing_pid": pid,
        "cwd": str(working_dir),
        "command": inner,
        "order": "kill the old session, pause, then open the replacement",
    }


def _ps_quote(value: str) -> str:
    """Single-quote a value for PowerShell, doubling any quote inside it."""
    return "'" + str(value).replace("'", "''") + "'"


def schedule_kill(pid: int, delay_ms: int = 2000) -> int:
    """Kill ``pid`` after a delay, from a detached helper.

    A tool cannot terminate its own caller and still return a result, the turn
    dies mid-call. So the kill is handed to a process that outlives this one,
    letting the tool respond first.

    The hosting shell is deliberately left alone. Terminating it gives it a
    non-zero exit code, and Windows Terminal's default ``closeOnExit=graceful``
    then keeps the tab open showing "[process exited with code ...]", which is
    worse than the prompt it would otherwise show. A shell this server spawned
    exits 0 by itself once its session dies, and the tab closes on that.
    """
    _proc(pid)  # validate before scheduling; fail closed on a bad pid
    script = f"Start-Sleep -Milliseconds {int(delay_ms)}; Stop-Process -Id {int(pid)} -Force"
    helper = subprocess.Popen(  # noqa: S603
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=HELPER,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return helper.pid


def python_version() -> str:
    return sys.version.split()[0]
