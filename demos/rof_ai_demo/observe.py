"""
observe.py – ROF AI Demo: proactive observation layer
=====================================================
Implements the Observe phase of the agent loop.

In the basic file-watching agent the "observe" phase is purely reactive:
the agent sleeps until an external actor writes a command to the watch file.
This module adds a second, proactive observation tier that fires on a
configurable interval independently of any incoming command.

What a proactive observation tick does
---------------------------------------
1. **Watch-file check** – if the file is non-empty, an external command is
   waiting; the observation short-circuits and returns immediately so the
   act phase can consume it.
2. **Artefact health** – verify that the output files written by previous
   runs still exist on disk.  Missing artefacts are logged as warnings.
3. **Mission-goal evaluation** – if a high-level mission goal was provided
   via ``--agent-goal``, ask :meth:`EpisodeMemory.mission_satisfied` whether
   the mission has been accomplished.  When it returns True the agent sets
   ``done = True`` and exits cleanly.
4. **Consecutive-failure guard** – scan recent episodes for the current goal
   pattern; if three or more consecutive failures are detected, emit a
   human-review warning.
5. **Heartbeat** – write a lightweight JSON heartbeat record to
   ``<output_dir>/agent_heartbeat.json`` so external monitors can confirm
   the agent is alive and see the latest cycle count + quality score.

The module is intentionally side-effect-free apart from:
  * writing the heartbeat file
  * printing to the console via the shared ``console`` helpers
  * calling ``EpisodeMemory.mission_satisfied()`` (read-only)

All functions are pure helpers or operate on explicit arguments – there is
no global mutable state in this module.

Public API
----------
  ObservationResult      – dataclass returned by ``observe()``
  observe()              – run one full observation tick
  write_heartbeat()      – write agent_heartbeat.json unconditionally
  check_artefact_health()– check whether previous run output files exist
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Console helpers – imported lazily so this module can be unit-tested
# standalone without the full demo package on sys.path.
# ---------------------------------------------------------------------------
try:
    from console import bold, cyan, dim, err, green, info, warn, yellow  # type: ignore
except ImportError:  # pragma: no cover

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

    def warn(s: str) -> None:  # type: ignore[misc]
        print(f"[WARN] {s}")

    def yellow(s: str) -> str:  # type: ignore[misc]
        return s


# ---------------------------------------------------------------------------
# ObservationResult
# ---------------------------------------------------------------------------


@dataclass
class ObservationResult:
    """
    Value object returned by :func:`observe`.

    Attributes
    ----------
    has_command      : bool – the watch file contained a non-empty command
    done             : bool – the mission goal has been satisfied; exit the loop
    mission_satisfied: bool – same as ``done`` when a mission goal is set
    artefacts_ok     : bool – all expected artefact paths still exist on disk
    missing_artefacts: list – paths that were expected but are now absent
    consecutive_fails: int  – consecutive failed episodes for the current pattern
    review_needed    : bool – True when consecutive_fails >= warning threshold
    heartbeat_written: bool – True when the heartbeat file was written this tick
    tick_ts          : float – Unix timestamp of this observation tick
    notes            : list  – human-readable notes collected during the tick
    """

    has_command: bool = False
    done: bool = False
    mission_satisfied: bool = False
    artefacts_ok: bool = True
    missing_artefacts: list = field(default_factory=list)
    consecutive_fails: int = 0
    review_needed: bool = False
    heartbeat_written: bool = False
    tick_ts: float = field(default_factory=time.time)
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Artefact health check
# ---------------------------------------------------------------------------


def check_artefact_health(
    artefact_paths: list[str],
) -> tuple[bool, list[str]]:
    """
    Verify that every path in *artefact_paths* still exists on disk.

    Parameters
    ----------
    artefact_paths : list[str]
        Absolute or relative file paths produced by previous runs.

    Returns
    -------
    (all_ok, missing)
        all_ok  : bool      – True when every path exists
        missing : list[str] – paths that no longer exist
    """
    missing: list[str] = []
    for p in artefact_paths:
        if p and not Path(p).exists():
            missing.append(p)
    return len(missing) == 0, missing


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

_HEARTBEAT_FILENAME = "agent_heartbeat.json"


def write_heartbeat(
    output_dir: Path,
    cycle: int,
    last_quality: float,
    last_command: str,
    last_success: Optional[bool],
    mission_goal: str,
    done: bool,
) -> bool:
    """
    Write a lightweight JSON heartbeat record to
    ``<output_dir>/agent_heartbeat.json``.

    The file is written atomically (write to a temp file, rename) to avoid
    partial reads by external monitors.

    Parameters
    ----------
    output_dir    : Path  – directory where the heartbeat file lives
    cycle         : int   – current episode cycle number
    last_quality  : float – quality score of the most recent episode
    last_command  : str   – the most recent command that was executed
    last_success  : bool  – success flag of the most recent run (None if none yet)
    mission_goal  : str   – the high-level mission goal (empty string if not set)
    done          : bool  – whether the mission has been declared complete

    Returns
    -------
    bool – True when the file was written successfully.
    """
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cycle": cycle,
        "last_quality": round(last_quality, 4),
        "last_command": last_command[:200] if last_command else "",
        "last_success": last_success,
        "mission_goal": mission_goal[:200] if mission_goal else "",
        "done": done,
    }
    target = output_dir / _HEARTBEAT_FILENAME
    tmp = output_dir / f"{_HEARTBEAT_FILENAME}.tmp"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError as exc:
        warn(f"Observe: could not write heartbeat to {target}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Core observation tick
# ---------------------------------------------------------------------------


def observe(
    *,
    watch_file: Path,
    output_dir: Path,
    episode_memory,  # EpisodeMemory – typed as Any to avoid circular import
    mission_goal: str = "",
    mission_min_quality: float = 0.70,
    last_artefacts: Optional[list[str]] = None,
    last_command: str = "",
    consecutive_failure_threshold: int = 3,
) -> ObservationResult:
    """
    Run one complete observation tick and return an :class:`ObservationResult`.

    This function is the entry point for the Observe phase.  It is designed
    to be called at the top of every agent loop iteration, *before* deciding
    whether to act.

    Parameters
    ----------
    watch_file     : Path
        The file polled for incoming commands.  Non-empty → has_command=True.
    output_dir     : Path
        Where heartbeat and other agent state files are written.
    episode_memory : EpisodeMemory
        The live episode store.  Used to check mission satisfaction and
        consecutive failure counts.
    mission_goal   : str
        High-level mission statement.  When non-empty, the observation tick
        checks whether the mission has been completed.  An empty string
        disables mission-completion detection.
    mission_min_quality : float
        Minimum composite quality score required to declare mission complete.
        Default: 0.70.
    last_artefacts : list[str] | None
        Paths of output files produced by the most recent run.  Health-checked
        on every tick.
    last_command   : str
        The most recent command executed (used for consecutive-failure lookup
        and heartbeat).
    consecutive_failure_threshold : int
        How many consecutive failures for the same goal pattern before
        ``review_needed`` is set to True.  Default: 3.

    Returns
    -------
    ObservationResult
        A value object summarising everything the agent needs to know before
        deciding what to do next.
    """
    result = ObservationResult(tick_ts=time.time())

    # ── 1. Watch-file check ───────────────────────────────────────────────
    try:
        content = watch_file.read_text(encoding="utf-8").strip()
        if content:
            result.has_command = True
            result.notes.append(f"Watch file has pending command ({len(content)} chars).")
            # Short-circuit: the act phase will handle it; skip the rest.
            # We still write the heartbeat below so monitors stay current.
            _write_heartbeat_from_memory(
                output_dir=output_dir,
                episode_memory=episode_memory,
                mission_goal=mission_goal,
                done=False,
            )
            result.heartbeat_written = True
            return result
    except OSError:
        # File missing or unreadable – treat as empty watch file.
        result.notes.append("Watch file unreadable; treating as empty.")

    # ── 2. Artefact health ────────────────────────────────────────────────
    artefacts = last_artefacts or []
    if artefacts:
        all_ok, missing = check_artefact_health(artefacts)
        result.artefacts_ok = all_ok
        result.missing_artefacts = missing
        if missing:
            for p in missing:
                warn(f"Observe: expected artefact missing from disk: {dim(p)}")
            result.notes.append(f"Missing artefacts: {missing}")
        else:
            result.notes.append(f"Artefact health OK ({len(artefacts)} file(s)).")

    # ── 3. Mission-goal evaluation ────────────────────────────────────────
    if mission_goal.strip():
        satisfied = episode_memory.mission_satisfied(mission_goal, min_quality=mission_min_quality)
        result.mission_satisfied = satisfied
        if satisfied:
            result.done = True
            result.notes.append(
                f"Mission goal satisfied: {mission_goal[:80]!r}  "
                f"(quality ≥ {mission_min_quality:.2f})"
            )
            info(
                f"Observe: {green('mission satisfied')} — "
                f"{bold(cyan(mission_goal[:60]))}  "
                f"(quality ≥ {mission_min_quality:.2f})"
            )
        else:
            result.notes.append(f"Mission goal not yet satisfied: {mission_goal[:80]!r}")

    # ── 4. Consecutive-failure guard ──────────────────────────────────────
    if last_command:
        # Import lazily to avoid circular import with memory.py
        try:
            from memory import _normalise_goal  # type: ignore
        except ImportError:
            # Fallback: simple lower-case normalisation
            def _normalise_goal(cmd: str) -> str:  # type: ignore[misc]
                import re as _re

                p = cmd.lower().strip()
                p = _re.sub(r"\d+", "<N>", p)
                return p[:120]

        pattern = _normalise_goal(last_command)
        fails = episode_memory.consecutive_failures(pattern)
        result.consecutive_fails = fails
        if fails >= consecutive_failure_threshold:
            result.review_needed = True
            _pat_repr = repr(pattern[:60])
            warn(
                f"Observe: {yellow(str(fails))} consecutive failure(s) "
                f"for goal pattern {dim(_pat_repr)} — human review recommended."
            )
            result.notes.append(
                f"Consecutive failures ({fails}) >= threshold ({consecutive_failure_threshold}) "
                f"for pattern: {_pat_repr}"
            )
        elif fails > 0:
            result.notes.append(f"Consecutive failures for current pattern: {fails}.")

    # ── 5. Heartbeat ──────────────────────────────────────────────────────
    last_ep = episode_memory.recent(1)
    last_quality = last_ep[0].quality_score if last_ep else 0.0
    last_success: Optional[bool] = last_ep[0].success if last_ep else None

    written = write_heartbeat(
        output_dir=output_dir,
        cycle=episode_memory.cycle,
        last_quality=last_quality,
        last_command=last_command,
        last_success=last_success,
        mission_goal=mission_goal,
        done=result.done,
    )
    result.heartbeat_written = written

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_heartbeat_from_memory(
    output_dir: Path,
    episode_memory,
    mission_goal: str,
    done: bool,
) -> None:
    """Convenience wrapper used by the short-circuit path inside observe()."""
    last_ep = episode_memory.recent(1)
    last_quality = last_ep[0].quality_score if last_ep else 0.0
    last_command = last_ep[0].command if last_ep else ""
    last_success: Optional[bool] = last_ep[0].success if last_ep else None
    write_heartbeat(
        output_dir=output_dir,
        cycle=episode_memory.cycle,
        last_quality=last_quality,
        last_command=last_command,
        last_success=last_success,
        mission_goal=mission_goal,
        done=done,
    )


# ---------------------------------------------------------------------------
# Agent-state persistence
# ---------------------------------------------------------------------------

_AGENT_STATE_FILENAME = "agent_state.json"


def save_agent_state(
    output_dir: Path,
    mission_goal: str,
    cycle: int,
    done: bool,
    last_command: str = "",
    last_quality: float = 0.0,
) -> bool:
    """
    Persist the current high-level agent state to
    ``<output_dir>/agent_state.json``.

    This file is the authoritative record of the agent's progress toward
    its mission goal.  It is written after every act-learn cycle so that
    a restarted agent can resume from where it left off.

    Parameters
    ----------
    output_dir   : Path  – directory to write into
    mission_goal : str   – the high-level mission (empty when none is set)
    cycle        : int   – current episode cycle count
    done         : bool  – whether the mission has been declared complete
    last_command : str   – the most recently executed command
    last_quality : float – quality score of the most recent episode

    Returns
    -------
    bool – True when the file was written successfully.
    """
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mission_goal": mission_goal,
        "cycle": cycle,
        "done": done,
        "last_command": last_command[:200] if last_command else "",
        "last_quality": round(last_quality, 4),
    }
    target = output_dir / _AGENT_STATE_FILENAME
    tmp = output_dir / f"{_AGENT_STATE_FILENAME}.tmp"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError as exc:
        warn(f"Observe: could not write agent state to {target}: {exc}")
        return False


def load_agent_state(output_dir: Path) -> dict:
    """
    Load the agent state file from *output_dir* and return it as a plain dict.

    Returns an empty dict when the file does not exist or cannot be parsed.
    Useful for a restarted agent to resume its cycle count and mission goal.
    """
    target = output_dir / _AGENT_STATE_FILENAME
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
