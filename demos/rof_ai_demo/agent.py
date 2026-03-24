"""
agent.py – ROF AI Demo: file-watching agent mode
=================================================
Implements the "agent" mode for rof_ai_demo.

In agent mode the demo watches a plain-text file for new commands written by
an external actor (e.g. pasted into a OneDrive-synced teams.txt file).
Whenever the file contains a non-empty, previously-unseen command the agent
feeds it directly into the ROFSession as if the user had typed it in the
interactive REPL.  After consuming the command the watch file is cleared so
the external actor can write the next one.

All console output produced during a workflow run is captured in memory and
written to the log file in a single atomic write once the run has finished
(success or failure).  Nothing is written to the log file while a command is
still executing, so the remote viewer always sees a complete, consistent
snapshot of each run.

Public entry point
------------------
  run_agent(session, watch_file, log_file, poll_interval) -> None

CLI integration
---------------
  Use ``--agent`` to activate agent mode.
  Use ``--agent-watch``  to override the default watch-file path.
  Use ``--agent-log``    to override the default log-file path.
  Use ``--agent-poll``   to override the poll interval in seconds (default 2).
"""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path
from typing import Optional, TextIO

# ---------------------------------------------------------------------------
# Console helpers (imported from sibling module, same pattern as the rest of
# the demo).  Guarded so agent.py can be imported even before the rest of the
# demo package has been fully initialised.
# ---------------------------------------------------------------------------
try:
    from console import (  # type: ignore
        banner,
        bold,
        cyan,
        dim,
        err,
        green,
        info,
        print_headline,
        red,
        section,
        warn,
        yellow,
    )
except ImportError:  # pragma: no cover – fallback for standalone testing

    def banner(title: str, subtitle: str = "") -> None:  # type: ignore[misc]
        print(f"\n=== {title} ===\n{subtitle}")

    def bold(t: str) -> str:  # type: ignore[misc]
        return t

    def cyan(t: str) -> str:  # type: ignore[misc]
        return t

    def dim(t: str) -> str:  # type: ignore[misc]
        return t

    def err(text: str) -> None:  # type: ignore[misc]
        print(f"[ERR]  {text}")

    def green(t: str) -> str:  # type: ignore[misc]
        return t

    def info(text: str) -> None:  # type: ignore[misc]
        print(f"[INFO] {text}")

    def print_headline(*, newline: bool = True) -> None:  # type: ignore[misc]
        pass

    def red(t: str) -> str:  # type: ignore[misc]
        return t

    def section(title: str) -> None:  # type: ignore[misc]
        print(f"\n--- {title} ---")

    def warn(text: str) -> None:  # type: ignore[misc]
        print(f"[WARN] {text}")

    def yellow(t: str) -> str:  # type: ignore[misc]
        return t


# ===========================================================================
# _Capture – per-run in-memory stdout/stderr proxy
# ===========================================================================


class _Capture(io.TextIOBase):
    """
    A write-through proxy that forwards every ``write()`` call to both the
    *original* stream and an in-memory ``io.StringIO`` buffer.

    The buffer can be retrieved and reset at any time via ``take()``, which
    returns the accumulated text and clears the buffer atomically.  This lets
    the agent dump a complete, consistent run log to disk in one shot after
    ``session.run()`` returns.
    """

    def __init__(self, original: TextIO) -> None:
        self._orig = original
        self._buf = io.StringIO()
        self._lock = threading.Lock()

    # --- TextIOBase contract -----------------------------------------------

    @property
    def encoding(self):  # type: ignore[override]
        return getattr(self._orig, "encoding", "utf-8")

    @property
    def errors(self):  # type: ignore[override]
        return getattr(self._orig, "errors", "replace")

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def write(self, s: str) -> int:  # type: ignore[override]
        with self._lock:
            n = self._orig.write(s)
            self._buf.write(s)
        return n

    def flush(self) -> None:
        with self._lock:
            try:
                self._orig.flush()
            except Exception:
                pass

    # Delegate everything else (isatty, fileno, …) to the original stream.
    def __getattr__(self, name: str):
        return getattr(self._orig, name)

    # --- Buffer management -------------------------------------------------

    def take(self) -> str:
        """
        Return all text captured since the last ``take()`` (or since
        construction) and reset the internal buffer to empty.
        Thread-safe.
        """
        with self._lock:
            text = self._buf.getvalue()
            self._buf = io.StringIO()
            return text


# ===========================================================================
# Command-file helpers
# ===========================================================================


def _read_command(watch_path: Path) -> Optional[str]:
    """
    Return the stripped content of *watch_path*, or ``None`` if the file is
    empty or cannot be read (e.g. locked by another process mid-write on
    Windows).
    """
    try:
        text = watch_path.read_text(encoding="utf-8", errors="replace").strip()
        return text if text else None
    except (OSError, PermissionError):
        return None


def _clear_watch_file(watch_path: Path) -> None:
    """
    Truncate *watch_path* to zero bytes so the external actor knows the
    command has been consumed.  Errors are reported but not fatal.
    """
    try:
        watch_path.write_text("", encoding="utf-8")
    except (OSError, PermissionError) as exc:
        warn(f"Agent: could not clear watch file: {exc}")


def _write_log(log_file: Path, text: str) -> None:
    """
    Overwrite *log_file* with *text* in one atomic ``write_text`` call.
    The file is always fully replaced so the remote viewer sees a clean,
    complete snapshot of the latest run rather than an ever-growing file.
    Errors are reported but not fatal.
    """
    try:
        log_file.write_text(text, encoding="utf-8", errors="replace")
    except (OSError, PermissionError) as exc:
        warn(f"Agent: could not write log file {log_file}: {exc}")


# ===========================================================================
# Public entry point
# ===========================================================================


def run_agent(
    session,  # ROFSession – typed as Any to avoid a circular import
    watch_file: Path,
    log_file: Path,
    poll_interval: float = 2.0,
) -> None:
    """
    Start the file-watching agent loop.

    Parameters
    ----------
    session       : ROFSession
        A fully initialised ROFSession (same object used by ``_repl``).
    watch_file    : Path
        The file to poll for incoming commands.
        When the file is non-empty and contains a command that hasn't been
        seen before, the command is executed and the file is cleared.
    log_file      : Path
        After each completed workflow run the full console output of that run
        is written to this file in one atomic write, replacing any previous
        content.  The file is only written once the run has finished so the
        remote viewer always sees a complete snapshot.
    poll_interval : float
        How often (in seconds) to check the watch file.  Default: 2.0 s.
    """

    # ── Ensure parent directories exist ──────────────────────────────────
    watch_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Create the watch file if it doesn't exist yet.
    if not watch_file.exists():
        try:
            watch_file.write_text("", encoding="utf-8")
        except OSError as exc:
            err(f"Agent: cannot create watch file {watch_file}: {exc}")
            return

    # ── Install the capture proxies on stdout / stderr ────────────────────
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    _cap_stdout = _Capture(_orig_stdout)
    _cap_stderr = _Capture(_orig_stderr)
    sys.stdout = _cap_stdout  # type: ignore[assignment]
    sys.stderr = _cap_stderr  # type: ignore[assignment]

    try:
        _agent_loop(session, watch_file, log_file, poll_interval, _cap_stdout, _cap_stderr)
    finally:
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr


# ---------------------------------------------------------------------------
# Internal loop
# ---------------------------------------------------------------------------


def _agent_loop(
    session,
    watch_file: Path,
    log_file: Path,
    poll_interval: float,
    cap_stdout: _Capture,
    cap_stderr: _Capture,
) -> None:
    """Core polling loop – called from :func:`run_agent`."""

    banner(
        "Agent Mode",
        (
            f"watch : {watch_file}  │  "
            f"log   : {log_file}  │  "
            f"poll  : {poll_interval}s  │  "
            "Ctrl-C to stop"
        ),
    )

    info(f"Agent watch file : {bold(cyan(str(watch_file)))}")
    info(f"Agent log  file  : {bold(cyan(str(log_file)))}")
    info(f"Poll interval    : {bold(str(poll_interval))} s")
    info(
        f"Status           : {green('active')} — write a command into the watch file to execute it"
    )
    print()

    # Discard any output produced during the banner / info lines above; we
    # only want to capture the output of actual workflow runs.
    cap_stdout.take()
    cap_stderr.take()

    # Set of command strings already executed in this session so we never
    # run the same command twice even if the watch file is not cleared in
    # time before the next poll tick.
    seen_commands: set[str] = set()

    # Track the last modification time so we only parse the file when it
    # actually changes – avoids redundant UTF-8 reads on every tick.
    last_mtime: float = 0.0

    try:
        while True:
            time.sleep(poll_interval)

            # ── Check whether the file has been modified ─────────────────
            try:
                current_mtime = watch_file.stat().st_mtime
            except OSError:
                # File was deleted – re-create it and keep waiting.
                try:
                    watch_file.write_text("", encoding="utf-8")
                except OSError:
                    pass
                last_mtime = 0.0
                continue

            if current_mtime == last_mtime:
                continue  # nothing changed

            last_mtime = current_mtime

            # ── Read the command ──────────────────────────────────────────
            command = _read_command(watch_file)
            if not command:
                continue

            # ── Deduplicate ───────────────────────────────────────────────
            if command in seen_commands:
                _clear_watch_file(watch_file)
                last_mtime = 0.0
                warn(
                    f"Agent: command already executed, skipping: "
                    f"{dim(command[:80] + ('…' if len(command) > 80 else ''))}"
                )
                # Discard the warning from the capture buffer; it's noise in
                # the log and the viewer already has the previous run's output.
                cap_stdout.take()
                cap_stderr.take()
                continue

            # ── Accept the command ────────────────────────────────────────
            seen_commands.add(command)

            section("Agent – incoming command")
            print(
                f"  {bold(cyan('CMD'))}  "
                f"{yellow(command[:120] + ('…' if len(command) > 120 else command[120:]))}"
            )
            print()

            # Clear the file BEFORE execution so the external actor can
            # write the next command while the current one is running.
            _clear_watch_file(watch_file)
            last_mtime = 0.0

            # Discard everything printed so far (banner, "incoming command"
            # header) from the buffer – we start capturing cleanly from here.
            cap_stdout.take()
            cap_stderr.take()

            # ── Execute ───────────────────────────────────────────────────
            try:
                session.run(command)
            except KeyboardInterrupt:
                warn("Agent: run interrupted by Ctrl-C.")
            except Exception as exc:
                err(f"Agent: run failed: {exc}")
                import traceback as _tb

                _tb.print_exc()

            # Print the headline stats (goes to terminal AND capture buffer).
            print_headline()
            print()

            # ── Flush captured output to the log file ─────────────────────
            # take() is called AFTER print_headline() so the stats line is
            # included in the log.  The file is fully overwritten so the
            # viewer always sees only the latest run.
            run_output = cap_stdout.take() + cap_stderr.take()
            _write_log(log_file, run_output)

            section("Agent – waiting for next command")
            info(
                f"  Executed so far: {bold(str(len(seen_commands)))} command(s).  "
                f"Write to {dim(str(watch_file))} to continue."
            )
            print()

            # Discard the "waiting" message from the buffer.
            cap_stdout.take()
            cap_stderr.take()

    except KeyboardInterrupt:
        print()
        section("Agent – shutting down")
        info(f"  Total commands executed: {bold(str(len(seen_commands)))}")
        print()

    finally:
        session.save_routing_memory()
        session.close_mcp()
        session.close_audit()
        print(f"  {dim('Agent stopped.')}  {dim(chr(0x1F916))}")
