"""
agent.py – ROF AI Demo: full observe → decide → act → learn agent loop
=======================================================================
Implements "agent mode" for rof_ai_demo.

The loop
--------
The agent operates in a continuous four-phase cycle:

  1. OBSERVE  – poll the watch file for incoming commands; run a proactive
                environment tick on every ``observe_interval`` seconds:
                check artefact health, evaluate the mission-goal predicate,
                detect consecutive failures, write a heartbeat file.

  2. DECIDE   – if there is a pending command, run it through the full
                Planner (NL → RelateLang AST).  This happens implicitly
                inside ``session.run()``.

  3. ACT      – execute the plan via the Orchestrator (or
                ConfidentOrchestrator when rof_routing is present).
                Retries failed steps; falls back to the LLM when all
                retries are exhausted.

  4. LEARN    – score the outcome (tool success rate, snapshot delta,
                artefact presence, keyword coverage), persist an episode
                record to ``agent_episodes.jsonl``, log a one-line
                quality summary, and warn when consecutive failures or
                low-quality scores are detected.

Termination conditions
----------------------
The loop exits when ANY of the following are true:

  * ``KeyboardInterrupt`` (Ctrl-C)
  * ``--agent-max-cycles N`` is set and N cycles have been completed
  * ``--agent-goal GOAL`` is set and :meth:`EpisodeMemory.mission_satisfied`
    returns True (the mission goal has been accomplished with sufficient
    quality)

Public entry point
------------------
  run_agent(session, watch_file, log_file, poll_interval, …) → None

New parameters vs. the old single-loop version
-----------------------------------------------
  episode_file     : Path  – JSONL file for episode records
  output_dir       : Path  – used for heartbeat + agent_state.json
  mission_goal     : str   – high-level goal; checked on every observe tick
  max_cycles       : int   – stop after this many completed runs (0 = unlimited)
  observe_interval : float – how often (s) to run a proactive observe tick
                             even when the watch file is empty (0 = disabled)
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

# ---------------------------------------------------------------------------
# Console helpers – imported from sibling module; guarded so agent.py can be
# imported even before the full demo package is on sys.path.
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

    def bold(s: str) -> str:  # type: ignore[misc]
        return s

    def cyan(s: str) -> str:  # type: ignore[misc]
        return s

    def dim(s: str) -> str:  # type: ignore[misc]
        return s

    def err(s: str) -> None:  # type: ignore[misc]
        print(f"[ERR] {s}")

    def green(s: str) -> str:  # type: ignore[misc]
        return s

    def info(s: str) -> None:  # type: ignore[misc]
        print(f"[INFO] {s}")

    def print_headline() -> None:  # type: ignore[misc]
        pass

    def red(s: str) -> str:  # type: ignore[misc]
        return s

    def section(s: str) -> None:  # type: ignore[misc]
        print(f"\n── {s} ──")

    def warn(s: str) -> None:  # type: ignore[misc]
        print(f"[WARN] {s}")

    def yellow(s: str) -> str:  # type: ignore[misc]
        return s


# ===========================================================================
# stdout/stderr capture proxy  (unchanged from original)
# ===========================================================================


class _Capture(io.RawIOBase):
    """
    A writable stream that tees every ``write()`` call to *original* (the
    real stdout/stderr) and also accumulates bytes in an internal buffer so
    the caller can take a snapshot at any time via ``take()``.

    Used to suppress pipeline-internal scaffolding from the log file while
    still showing it on the terminal.
    """

    # make the proxy look like a valid TextIOWrapper
    mode = "w"

    def __init__(self, original: TextIO) -> None:
        super().__init__()
        self._original = original
        self._buf: list[str] = []

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._original, "errors", "replace") or "replace"

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def write(self, s: object) -> int:  # type: ignore[override]
        text = str(s)
        self._original.write(text)
        self._buf.append(text)
        return len(text)

    def flush(self) -> None:
        try:
            self._original.flush()
        except Exception:
            pass

    # Proxy every attribute access not defined here to the original stream so
    # that code which calls e.g. sys.stdout.fileno() or .isatty() still works.
    def __getattr__(self, name: str):
        return getattr(self._original, name)

    def take(self) -> str:
        """
        Return everything accumulated since the last ``take()`` call and
        clear the internal buffer.
        """
        result = "".join(self._buf)
        self._buf.clear()
        return result


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
    Always replaces the full file so the remote viewer sees a clean,
    complete snapshot of the latest run.  Errors are reported but not fatal.
    """
    try:
        log_file.write_text(text, encoding="utf-8", errors="replace")
    except (OSError, PermissionError) as exc:
        warn(f"Agent: could not write log file {log_file}: {exc}")


# ===========================================================================
# Public entry point
# ===========================================================================


def run_agent(
    session,  # ROFSession – typed as Any to avoid circular import
    watch_file: Path,
    log_file: Path,
    poll_interval: float = 2.0,
    log_format: str = "text",  # "text" or "markdown"
    # ── New parameters for the full agent loop ──────────────────────────
    episode_file: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    mission_goal: str = "",
    max_cycles: int = 0,  # 0 = unlimited
    observe_interval: float = 0.0,  # 0 = disabled (reactive only)
) -> None:
    """
    Start the full observe → decide → act → learn agent loop.

    Parameters
    ----------
    session         : ROFSession
        A fully initialised ROFSession (same object used by ``_repl``).
    watch_file      : Path
        The file polled for incoming commands.  When non-empty and containing
        a previously-unseen command the agent executes it and clears the file.
    log_file        : Path
        After each completed run the result is rendered by
        ``output_layout.render_result()`` and written here in one atomic
        write, replacing any previous content.
    poll_interval   : float
        How often (seconds) to check the watch file.  Default: 2.0 s.
    log_format      : str
        ``"text"`` (default) – plain text.
        ``"markdown"``       – GitHub-Flavoured Markdown.
    episode_file    : Path | None
        JSONL file for episode memory records.  Defaults to
        ``<output_dir>/agent_episodes.jsonl``.
    output_dir      : Path | None
        Directory for heartbeat + agent_state.json.  Inferred from
        ``log_file.parent`` when not supplied.
    mission_goal    : str
        High-level natural-language mission.  When non-empty the agent
        checks :meth:`EpisodeMemory.mission_satisfied` on every proactive
        observe tick and stops the loop when the mission is complete.
    max_cycles      : int
        Stop after this many successfully completed act phases.  0 = run
        until Ctrl-C or mission satisfied.
    observe_interval : float
        Seconds between proactive observation ticks (artefact health,
        mission check, heartbeat).  0 disables proactive observation so
        the agent only reacts to watch-file writes.
    """
    # ── Normalise / validate parameters ──────────────────────────────────
    log_format = log_format.strip().lower()
    if log_format not in ("text", "markdown"):
        warn(f"Agent: unknown log_format {log_format!r}; falling back to 'text'.")
        log_format = "text"

    _out_dir: Path = output_dir if output_dir is not None else log_file.parent
    _episode_file: Path = (
        episode_file if episode_file is not None else _out_dir / "agent_episodes.jsonl"
    )

    # ── Ensure parent directories exist ──────────────────────────────────
    watch_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _out_dir.mkdir(parents=True, exist_ok=True)

    if not watch_file.exists():
        try:
            watch_file.write_text("", encoding="utf-8")
        except OSError as exc:
            err(f"Agent: cannot create watch file {watch_file}: {exc}")
            return

    # ── Episode memory ────────────────────────────────────────────────────
    try:
        from memory import EpisodeMemory  # type: ignore

        episode_memory = EpisodeMemory(path=_episode_file)
        _has_memory = True
    except ImportError:
        episode_memory = None
        _has_memory = False
        warn("Agent: memory.py not found; episode recording disabled.")

    # ── Proactive observation layer ───────────────────────────────────────
    try:
        from observe import load_agent_state, observe, save_agent_state  # type: ignore

        _has_observe = True
    except ImportError:
        observe = None  # type: ignore[assignment]
        save_agent_state = None  # type: ignore[assignment]
        load_agent_state = None  # type: ignore[assignment]
        _has_observe = False
        if observe_interval > 0 or mission_goal:
            warn("Agent: observe.py not found; proactive observation disabled.")

    # ── Resume cycle count from previous agent_state.json (if any) ───────
    _resumed_cycle = 0
    if _has_observe and load_agent_state is not None:
        prior_state = load_agent_state(_out_dir)
        if prior_state:
            _resumed_cycle = prior_state.get("cycle", 0)
            _prior_goal = prior_state.get("mission_goal", "")
            if _resumed_cycle:
                info(
                    f"Agent: resuming from cycle {bold(str(_resumed_cycle))} "
                    f"(prior mission: {dim(_prior_goal[:60] or '(none)')})"
                )

    # ── Install stdout/stderr capture proxies ─────────────────────────────
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    _cap_stdout = _Capture(_orig_stdout)
    _cap_stderr = _Capture(_orig_stderr)
    sys.stdout = _cap_stdout  # type: ignore[assignment]
    sys.stderr = _cap_stderr  # type: ignore[assignment]

    try:
        _agent_loop(
            session=session,
            watch_file=watch_file,
            log_file=log_file,
            poll_interval=poll_interval,
            log_format=log_format,
            cap_stdout=_cap_stdout,
            cap_stderr=_cap_stderr,
            episode_memory=episode_memory,
            has_memory=_has_memory,
            observe_fn=observe,
            save_state_fn=save_agent_state,
            has_observe=_has_observe,
            out_dir=_out_dir,
            mission_goal=mission_goal,
            max_cycles=max_cycles,
            observe_interval=observe_interval,
        )
    finally:
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr


# ===========================================================================
# Internal loop
# ===========================================================================


def _agent_loop(
    session,
    watch_file: Path,
    log_file: Path,
    poll_interval: float,
    log_format: str,
    cap_stdout: _Capture,
    cap_stderr: _Capture,
    # ── Agent additions ───────────────────────────────────────────────────
    episode_memory,  # EpisodeMemory | None
    has_memory: bool,
    observe_fn,  # observe() | None
    save_state_fn,  # save_agent_state() | None
    has_observe: bool,
    out_dir: Path,
    mission_goal: str,
    max_cycles: int,
    observe_interval: float,
) -> None:
    """
    Core loop – called from :func:`run_agent`.

    Phases per iteration
    --------------------
    OBSERVE  → read watch file; proactive tick when observe_interval fires
    DECIDE   → implicit in session.run() (Planner: NL → RelateLang AST)
    ACT      → session.run() executes the plan and returns RunResult
    LEARN    → session.evaluate_outcome() scores + records the episode
    """
    render_mode = "agent_md" if log_format == "markdown" else "agent"
    format_label = "markdown (.md)" if log_format == "markdown" else "plain text"

    _cycle_label = f"  max={max_cycles}" if max_cycles > 0 else "  max=∞"
    _goal_label = f"  mission={mission_goal[:50]!r}" if mission_goal else "  mission=(none)"
    _obs_label = (
        f"  observe_interval={observe_interval}s"
        if observe_interval > 0
        else "  observe=reactive-only"
    )

    banner(
        "Agent Mode  –  observe → decide → act → learn",
        (
            f"watch : {watch_file}  │  "
            f"log   : {log_file}  │  "
            f"poll  : {poll_interval}s  │  "
            f"format: {format_label}  │  "
            "Ctrl-C to stop"
        ),
    )

    info(f"Agent watch file   : {bold(cyan(str(watch_file)))}")
    info(f"Agent log  file    : {bold(cyan(str(log_file)))}")
    info(
        f"Episode memory     : {bold(cyan(str(out_dir / 'agent_episodes.jsonl'))) if has_memory else dim('disabled')}"
    )
    info(f"Log format         : {bold(format_label)}")
    info(f"Poll interval      : {bold(str(poll_interval))} s")
    info(f"Cycle limit        : {bold(_cycle_label.strip())}")
    info(
        f"Mission goal       : {bold(cyan(mission_goal[:60])) if mission_goal else dim('(none – run until Ctrl-C or max-cycles)')}"
    )
    info(
        f"Observe interval   : {bold(str(observe_interval) + ' s') if observe_interval > 0 else dim('disabled (reactive only)')}"
    )
    info(
        f"Status             : {green('active')} — "
        "write a command into the watch file to execute it"
    )
    print()

    # Discard banner/info output from the capture buffer
    cap_stdout.take()
    cap_stderr.take()

    # ── Loop state ────────────────────────────────────────────────────────
    seen_commands: set[str] = set()  # deduplication within this session
    last_mtime: float = 0.0  # watch-file mtime on last poll
    completed_cycles: int = 0  # successful act phases this session
    done: bool = False  # mission-complete flag

    # Proactive observe tick timing
    last_observe_tick: float = time.monotonic()

    # Artefacts from the most recent run (for health checks)
    last_artefacts: list[str] = []

    # Most recent command (for consecutive-failure lookups)
    last_command: str = ""

    # ── Restore episode count offset if resuming ──────────────────────────
    if has_memory and episode_memory is not None:
        _prior_cycles = episode_memory.cycle
        if _prior_cycles:
            info(f"Agent: {_prior_cycles} episode(s) already in memory from prior sessions.")
            cap_stdout.take()
            cap_stderr.take()

    try:
        # =================================================================
        # MAIN LOOP  –  while not done
        # =================================================================
        while not done:
            time.sleep(poll_interval)

            # =============================================================
            # PHASE 1 – OBSERVE
            # =============================================================
            now = time.monotonic()
            _proactive_tick = observe_interval > 0 and (now - last_observe_tick) >= observe_interval

            if _proactive_tick or mission_goal:
                # Run a full proactive observation tick
                if has_observe and observe_fn is not None and episode_memory is not None:
                    obs = observe_fn(
                        watch_file=watch_file,
                        output_dir=out_dir,
                        episode_memory=episode_memory,
                        mission_goal=mission_goal,
                        last_artefacts=last_artefacts,
                        last_command=last_command,
                    )
                    last_observe_tick = now

                    # Discard any console output from the tick itself
                    cap_stdout.take()
                    cap_stderr.take()

                    # Mission complete? → exit the loop
                    if obs.done:
                        done = True
                        section("Agent – mission goal satisfied")
                        info(f"  {green('Mission accomplished')}:  {bold(cyan(mission_goal[:80]))}")
                        info(f"  Completed cycles this session : {bold(str(completed_cycles))}")
                        print()
                        cap_stdout.take()
                        cap_stderr.take()
                        break

                    # Human-review warning already printed by observe(); we
                    # just flush the buffer so it doesn't leak into the log.
                    cap_stdout.take()
                    cap_stderr.take()

                elif _proactive_tick:
                    # observe.py unavailable – still update the tick timer
                    last_observe_tick = now

            # =============================================================
            # Watch-file mtime check
            # =============================================================
            try:
                current_mtime = watch_file.stat().st_mtime
            except OSError:
                # File was deleted – re-create and keep waiting
                try:
                    watch_file.write_text("", encoding="utf-8")
                except OSError:
                    pass
                last_mtime = 0.0
                continue

            if current_mtime == last_mtime:
                continue  # nothing changed

            last_mtime = current_mtime

            # =============================================================
            # Read the command
            # =============================================================
            command = _read_command(watch_file)
            if not command:
                continue

            # =============================================================
            # Deduplication
            # =============================================================
            if command in seen_commands:
                _clear_watch_file(watch_file)
                last_mtime = 0.0
                warn(
                    f"Agent: command already executed this session, skipping: "
                    f"{dim(command[:80] + ('…' if len(command) > 80 else ''))}"
                )
                cap_stdout.take()
                cap_stderr.take()
                continue

            # =============================================================
            # Accept the command
            # =============================================================
            seen_commands.add(command)
            last_command = command

            section("Agent – OBSERVE  |  incoming command")
            print(
                f"  {bold(cyan('CMD'))}  "
                f"{yellow(command[:120] + ('…' if len(command) > 120 else command[120:]))}"
            )
            if max_cycles > 0:
                print(
                    f"  {dim('cycle')}  {bold(str(completed_cycles + 1))}{dim(f' / {max_cycles}')}"
                )
            print()

            # Clear the watch file BEFORE execution so the external actor
            # can write the next command while this one is running.
            _clear_watch_file(watch_file)
            last_mtime = 0.0

            # Discard everything printed so far from the capture buffer
            cap_stdout.take()
            cap_stderr.take()

            # =============================================================
            # PHASE 2 – DECIDE  (implicit inside session.run)
            # Capture the pre-run snapshot for the learn phase delta
            # =============================================================
            pre_snapshot: dict = {}
            try:
                pre_snapshot = session.current_snapshot
            except AttributeError:
                pass  # older ROFSession without the property

            # =============================================================
            # PHASE 3 – ACT
            # =============================================================
            section("Agent – DECIDE + ACT  |  running workflow")

            result = None
            plan_ms = 0
            exec_ms = 0
            run_success = False
            try:
                result, plan_ms, exec_ms = session.run(command)
                run_success = result.success
            except KeyboardInterrupt:
                warn("Agent: run interrupted by Ctrl-C.")
                cap_stdout.take()
                cap_stderr.take()
                raise  # let the outer handler catch it
            except Exception as exc:
                err(f"Agent: run failed: {exc}")
                import traceback as _tb

                _tb.print_exc()

            print_headline()
            print()

            # =============================================================
            # PHASE 4 – LEARN
            # =============================================================
            section("Agent – LEARN  |  scoring outcome")

            episode = None
            if has_memory and episode_memory is not None and result is not None:
                try:
                    episode = session.evaluate_outcome(
                        command=command,
                        result=result,
                        pre_snapshot=pre_snapshot,
                        plan_ms=plan_ms,
                        exec_ms=exec_ms,
                        episode_memory=episode_memory,
                    )
                    # Update artefact list for the next observe tick
                    last_artefacts = list(episode.artefact_paths)
                except Exception as exc:
                    warn(f"Agent: learn phase error: {exc}")

            # Flush learn output from the capture buffer
            cap_stdout.take()
            cap_stderr.take()

            # =============================================================
            # Persist agent state (mission_goal + cycle count + done flag)
            # =============================================================
            completed_cycles += 1
            _last_quality = episode.quality_score if episode else (1.0 if run_success else 0.0)

            if has_observe and save_state_fn is not None and episode_memory is not None:
                try:
                    save_state_fn(
                        output_dir=out_dir,
                        mission_goal=mission_goal,
                        cycle=episode_memory.cycle,
                        done=done,
                        last_command=command,
                        last_quality=_last_quality,
                    )
                except Exception as exc:
                    warn(f"Agent: could not save agent state: {exc}")
                cap_stdout.take()
                cap_stderr.take()

            # =============================================================
            # Write the log file  (structured RunResult → clean text/md)
            # =============================================================
            if result is not None:
                from output_layout import render_result  # type: ignore

                log_text = render_result(
                    result.snapshot,
                    mode=render_mode,
                    command=command,
                    success=run_success,
                    plan_ms=plan_ms,
                    exec_ms=exec_ms,
                )
                _write_log(log_file, log_text)

            # =============================================================
            # Cycle-limit check
            # =============================================================
            if max_cycles > 0 and completed_cycles >= max_cycles:
                done = True
                section("Agent – cycle limit reached")
                info(
                    f"  Completed {bold(str(completed_cycles))} of "
                    f"{bold(str(max_cycles))} requested cycles.  Stopping."
                )
                print()
                cap_stdout.take()
                cap_stderr.take()
                break

            # =============================================================
            # Wait-for-next-command banner
            # =============================================================
            section("Agent – OBSERVE  |  waiting for next command")
            _ep_summary = ""
            if has_memory and episode_memory is not None:
                s = episode_memory.summary()
                _avg_q_str = f"{s['avg_quality']:.3f}"
                _ep_summary = (
                    f"  episodes={bold(str(s['total']))}  "
                    f"ok={green(str(s['succeeded']))}  "
                    f"fail={red(str(s['failed']))}  "
                    f"avg_q={bold(_avg_q_str)}"
                )
            info(
                f"  Cycles this session: {bold(str(completed_cycles))}"
                + (f"  │  {_ep_summary.strip()}" if _ep_summary else "")
            )
            if mission_goal:
                info(f"  Mission  : {dim(mission_goal[:80])}")
            info(f"  Write to {dim(str(watch_file))} to continue.")
            print()

            cap_stdout.take()
            cap_stderr.take()

    except KeyboardInterrupt:
        print()
        section("Agent – shutting down")
        info(f"  Cycles completed this session : {bold(str(completed_cycles))}")
        if has_memory and episode_memory is not None:
            s = episode_memory.summary()
            info(
                f"  Episode store                 : "
                f"{s['total']} total  "
                f"{s['succeeded']} ok  "
                f"{s['failed']} failed  "
                f"avg quality {s['avg_quality']:.3f}"
            )
            info(f"  Episode file                  : {dim(str(episode_memory._path))}")
        print()

    finally:
        # ── Persist routing memory, close MCP + audit ─────────────────────
        session.save_routing_memory()
        session.close_mcp()
        session.close_audit()

        # ── Final agent-state write ───────────────────────────────────────
        if has_observe and save_state_fn is not None and episode_memory is not None:
            try:
                _last_ep = episode_memory.recent(1)
                _final_q = _last_ep[0].quality_score if _last_ep else 0.0
                _final_cmd = _last_ep[0].command if _last_ep else ""
                save_state_fn(
                    output_dir=out_dir,
                    mission_goal=mission_goal,
                    cycle=episode_memory.cycle,
                    done=done,
                    last_command=_final_cmd,
                    last_quality=_final_q,
                )
            except Exception:
                pass

        print(f"  {dim('Agent stopped.')}  {dim(chr(0x1F916))}")
