"""
memory.py – ROF AI Demo: episode memory store
=============================================
Records one structured episode record after every agent run, persisting
results to a JSONL file so the learn phase has durable, cross-session
memory of what worked and what did not.

An "episode" captures everything needed to understand a single run:
  - the original goal / command
  - which tools were invoked and in what order
  - whether each step succeeded
  - a composite outcome quality score (0.0 – 1.0)
  - how many new entity attributes were written (snapshot delta)
  - whether any output artefact was produced
  - the full error message when things went wrong
  - a monotonically increasing cycle counter

The quality score uses the same four signals documented in agent.md:

  1. tool_success_rate  (weight 0.40) – fraction of steps that succeeded
  2. snapshot_delta     (weight 0.35) – new attributes written / cap of 10
  3. artefact_produced  (weight 0.15) – was any file saved to disk?
  4. keyword_coverage   (weight 0.10) – goal keywords found in snapshot values

A score ≥ 0.70 is "high quality"; < 0.40 triggers a retry recommendation.

Public API
----------
  EpisodeRecord          – frozen dataclass for a single run record
  EpisodeMemory          – append-only store backed by a .jsonl file
  score_outcome()        – compute composite quality score from raw signals
  consecutive_failures() – count how many recent episodes for a pattern failed
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Quality scoring weights (must sum to 1.0)
# ---------------------------------------------------------------------------
_W_TOOL_SUCCESS: float = 0.40
_W_SNAPSHOT_DELTA: float = 0.35
_W_ARTEFACT: float = 0.15
_W_KEYWORD: float = 0.10

# Snapshot-delta is capped at this many new attributes before scoring 1.0
_DELTA_CAP: int = 10

# Minimum composite score considered "high quality"
QUALITY_THRESHOLD_HIGH: float = 0.70

# Composite score below this triggers a retry recommendation in the log
QUALITY_THRESHOLD_LOW: float = 0.40

# How many consecutive failures for the same normalised goal pattern before
# a human-review warning is emitted
CONSECUTIVE_FAILURE_WARN: int = 3


# ---------------------------------------------------------------------------
# EpisodeRecord
# ---------------------------------------------------------------------------


@dataclass
class EpisodeRecord:
    """
    Immutable snapshot of one completed agent run.

    Fields
    ------
    cycle          : int     – monotonically increasing run counter (1-based)
    run_id         : str     – UUID from RunResult (first 8 chars shown in logs)
    timestamp      : float   – Unix epoch seconds (UTC) when the episode was closed
    command        : str     – the raw user command / goal that triggered the run
    goal_pattern   : str     – normalised goal pattern (lower-cased, numbers stripped)
    success        : bool    – overall RunResult.success flag
    step_count     : int     – total number of orchestrator steps executed
    steps_succeeded: int     – steps whose status was ACHIEVED
    steps_failed   : int     – steps whose status was FAILED
    tools_used     : list    – ordered list of tool names that were dispatched
    snapshot_delta : int     – number of new entity attributes written during this run
    artefact_paths : list    – file paths written by FileSaveTool (or AICodeGenTool)
    plan_ms        : int     – milliseconds spent in the planning stage
    exec_ms        : int     – milliseconds spent in the execution stage
    error          : str     – last error message (empty string when success)
    quality_score  : float   – composite outcome quality (0.0 – 1.0)
    recommendation : str     – "ok" | "retry" | "review" based on quality thresholds
    """

    cycle: int
    run_id: str
    timestamp: float
    command: str
    goal_pattern: str
    success: bool
    step_count: int
    steps_succeeded: int
    steps_failed: int
    tools_used: list = field(default_factory=list)
    snapshot_delta: int = 0
    artefact_paths: list = field(default_factory=list)
    plan_ms: int = 0
    exec_ms: int = 0
    error: str = ""
    quality_score: float = 0.0
    recommendation: str = "ok"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EpisodeRecord":
        return cls(
            cycle=d.get("cycle", 0),
            run_id=d.get("run_id", ""),
            timestamp=d.get("timestamp", 0.0),
            command=d.get("command", ""),
            goal_pattern=d.get("goal_pattern", ""),
            success=d.get("success", False),
            step_count=d.get("step_count", 0),
            steps_succeeded=d.get("steps_succeeded", 0),
            steps_failed=d.get("steps_failed", 0),
            tools_used=d.get("tools_used", []),
            snapshot_delta=d.get("snapshot_delta", 0),
            artefact_paths=d.get("artefact_paths", []),
            plan_ms=d.get("plan_ms", 0),
            exec_ms=d.get("exec_ms", 0),
            error=d.get("error", ""),
            quality_score=d.get("quality_score", 0.0),
            recommendation=d.get("recommendation", "ok"),
        )

    def __repr__(self) -> str:
        return (
            f"EpisodeRecord(cycle={self.cycle}, run_id={self.run_id!r}, "
            f"success={self.success}, quality={self.quality_score:.3f}, "
            f"rec={self.recommendation!r})"
        )


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------


def _normalise_goal(command: str) -> str:
    """
    Produce a stable, normalised pattern from *command* suitable for grouping
    similar goals together across different runs.

    Steps
    -----
    1. Lower-case.
    2. Strip leading/trailing whitespace.
    3. Replace runs of digits with ``<N>`` so "calculate 15 numbers" and
       "calculate 20 numbers" share the same pattern.
    4. Collapse multiple spaces to one.
    5. Truncate to 120 chars to keep keys compact.
    """
    pattern = command.lower().strip()
    pattern = re.sub(r"\d+", "<N>", pattern)
    pattern = re.sub(r"\s{2,}", " ", pattern)
    return pattern[:120]


def _count_snapshot_delta(pre_snapshot: dict, post_snapshot: dict) -> int:
    """
    Count how many entity attributes exist in *post_snapshot* that were
    absent from *pre_snapshot*.

    Both snapshots are expected to have the shape::

        {"entities": {"EntityName": {"attributes": {"key": "value", …}, …}, …}}

    Returns an integer ≥ 0.
    """

    def _attrs(snap: dict) -> set:
        result: set = set()
        for ent_name, ent_data in snap.get("entities", {}).items():
            for attr_key in ent_data.get("attributes", {}).keys():
                result.add(f"{ent_name}.{attr_key}")
        return result

    pre_attrs = _attrs(pre_snapshot)
    post_attrs = _attrs(post_snapshot)
    return len(post_attrs - pre_attrs)


def _extract_artefacts(snapshot: dict) -> list[str]:
    """
    Scan *snapshot* for entity attributes named ``saved_to`` or ``output_path``
    that look like local file paths.  Returns deduplicated list of path strings.
    """
    artefacts: list[str] = []
    seen: set[str] = set()
    for _ent_data in snapshot.get("entities", {}).values():
        attrs = _ent_data.get("attributes", {})
        for key in ("saved_to", "output_path", "file_path", "path"):
            val = attrs.get(key, "")
            if isinstance(val, str) and val and val not in seen:
                # Accept anything that looks like a file path (has an extension
                # or starts with / or contains a path separator).
                if "." in val.split("/")[-1] or "/" in val or "\\" in val:
                    artefacts.append(val)
                    seen.add(val)
    return artefacts


def _keyword_coverage(command: str, snapshot: dict) -> float:
    """
    Compute what fraction of the "important" words in *command* appear
    somewhere in the snapshot entity attribute values.

    "Important" words are those with ≥ 4 characters, excluding common
    stop words.  Returns 0.0 when there are no important words.
    """
    _STOP = {
        "that",
        "this",
        "with",
        "from",
        "have",
        "will",
        "been",
        "were",
        "they",
        "them",
        "then",
        "than",
        "when",
        "what",
        "which",
        "into",
        "also",
        "some",
        "each",
        "more",
        "make",
        "about",
        "using",
        "create",
        "write",
        "generate",
        "produce",
        "build",
        "ensure",
        "search",
        "find",
    }
    words = [w for w in re.findall(r"[a-z]{4,}", command.lower()) if w not in _STOP]
    if not words:
        return 0.5  # neutral when nothing to check

    # Collect all attribute values as one big lower-cased text blob
    all_values: list[str] = []
    for ent_data in snapshot.get("entities", {}).values():
        for val in ent_data.get("attributes", {}).values():
            if isinstance(val, str):
                all_values.append(val.lower())
    blob = " ".join(all_values)

    found = sum(1 for w in words if w in blob)
    return found / len(words)


def score_outcome(
    command: str,
    steps_succeeded: int,
    step_count: int,
    pre_snapshot: dict,
    post_snapshot: dict,
    tool_success: bool,
) -> tuple[float, str]:
    """
    Compute composite outcome quality score and recommendation label.

    Parameters
    ----------
    command         : str  – raw user command (used for keyword coverage)
    steps_succeeded : int  – number of steps that achieved their goal
    step_count      : int  – total steps attempted
    pre_snapshot    : dict – snapshot before execution (may be empty dict)
    post_snapshot   : dict – snapshot after execution
    tool_success    : bool – overall RunResult.success flag

    Returns
    -------
    (score, recommendation)
      score          : float 0.0 – 1.0
      recommendation : "ok" | "retry" | "review"
    """
    # --- Signal 1: tool success rate (0.0–1.0) ---
    if step_count > 0:
        success_rate = steps_succeeded / step_count
    else:
        success_rate = 1.0 if tool_success else 0.0

    # --- Signal 2: snapshot delta (normalised to 0.0–1.0) ---
    delta = _count_snapshot_delta(pre_snapshot, post_snapshot)
    delta_score = min(delta / _DELTA_CAP, 1.0)

    # --- Signal 3: artefact produced ---
    artefacts = _extract_artefacts(post_snapshot)
    artefact_score = 1.0 if artefacts else 0.0

    # --- Signal 4: keyword coverage ---
    keyword_score = _keyword_coverage(command, post_snapshot)

    # --- Composite ---
    composite = (
        _W_TOOL_SUCCESS * success_rate
        + _W_SNAPSHOT_DELTA * delta_score
        + _W_ARTEFACT * artefact_score
        + _W_KEYWORD * keyword_score
    )
    composite = max(0.0, min(1.0, composite))

    if composite >= QUALITY_THRESHOLD_HIGH:
        recommendation = "ok"
    elif composite >= QUALITY_THRESHOLD_LOW:
        recommendation = "retry"
    else:
        recommendation = "review"

    return composite, recommendation


# ---------------------------------------------------------------------------
# EpisodeMemory
# ---------------------------------------------------------------------------


class EpisodeMemory:
    """
    Append-only episode store backed by a JSONL file.

    One JSON object per line; each line is a serialised :class:`EpisodeRecord`.
    The in-memory ``_records`` list mirrors the file contents so queries never
    need to re-read the file.

    Parameters
    ----------
    path : Path
        Where to write/read episode records.  The file (and its parent
        directories) are created on first write if they do not exist.
    max_recent : int
        How many recent records to keep in memory for fast queries.
        Older records remain in the JSONL file but are not held in RAM.
        Default: 500.
    """

    def __init__(self, path: Path, max_recent: int = 500) -> None:
        self._path = path
        self._max_recent = max_recent
        self._records: list[EpisodeRecord] = []
        self._cycle: int = 0
        self._load_existing()

    # ------------------------------------------------------------------
    # Public write path
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        run_id: str,
        command: str,
        success: bool,
        steps: list,  # list of StepResult-like objects
        pre_snapshot: dict,
        post_snapshot: dict,
        plan_ms: int = 0,
        exec_ms: int = 0,
        error: str = "",
    ) -> EpisodeRecord:
        """
        Build an :class:`EpisodeRecord`, compute its quality score, append it
        to the JSONL file, and return it.

        Parameters
        ----------
        run_id        : str  – from RunResult.run_id
        command       : str  – original user prompt / goal
        success       : bool – RunResult.success
        steps         : list – list of StepResult objects (duck-typed)
        pre_snapshot  : dict – snapshot captured BEFORE orchestrator.run()
        post_snapshot : dict – RunResult.snapshot
        plan_ms       : int  – planning stage duration
        exec_ms       : int  – execution stage duration
        error         : str  – last error string (empty when success)
        """
        self._cycle += 1

        # Extract step metrics
        step_count = len(steps)
        steps_succeeded = sum(
            1
            for s in steps
            if getattr(s, "status", None) is not None
            and str(getattr(s, "status", "")).upper() in ("ACHIEVED", "GOALSTATUS.ACHIEVED")
        )
        steps_failed = step_count - steps_succeeded

        # Collect tools used (in order, deduped for readability but ordered)
        tools_used: list[str] = []
        seen_tools: set[str] = set()
        for s in steps:
            tool = getattr(s, "tool_name", None) or getattr(s, "tool", None) or ""
            if tool and tool not in seen_tools:
                tools_used.append(str(tool))
                seen_tools.add(str(tool))

        artefact_paths = _extract_artefacts(post_snapshot)
        goal_pattern = _normalise_goal(command)

        quality_score, recommendation = score_outcome(
            command=command,
            steps_succeeded=steps_succeeded,
            step_count=step_count,
            pre_snapshot=pre_snapshot,
            post_snapshot=post_snapshot,
            tool_success=success,
        )

        record = EpisodeRecord(
            cycle=self._cycle,
            run_id=run_id,
            timestamp=time.time(),
            command=command,
            goal_pattern=goal_pattern,
            success=success,
            step_count=step_count,
            steps_succeeded=steps_succeeded,
            steps_failed=steps_failed,
            tools_used=tools_used,
            snapshot_delta=_count_snapshot_delta(pre_snapshot, post_snapshot),
            artefact_paths=artefact_paths,
            plan_ms=plan_ms,
            exec_ms=exec_ms,
            error=error,
            quality_score=quality_score,
            recommendation=recommendation,
        )

        self._append(record)
        return record

    # ------------------------------------------------------------------
    # Public read path
    # ------------------------------------------------------------------

    @property
    def cycle(self) -> int:
        """Current (last completed) cycle number."""
        return self._cycle

    def recent(self, n: int = 10) -> list[EpisodeRecord]:
        """Return the *n* most recent episode records (newest last)."""
        return self._records[-n:]

    def consecutive_failures(self, goal_pattern: str) -> int:
        """
        Count how many of the most recent episodes for *goal_pattern*
        ended in failure (success=False), counting backwards from the
        most recent until a success is found.

        Returns 0 when the pattern has never been seen or the last episode
        for this pattern was successful.
        """
        count = 0
        for rec in reversed(self._records):
            if rec.goal_pattern != goal_pattern:
                continue
            if rec.success:
                break
            count += 1
        return count

    def summary(self) -> dict:
        """
        Return a compact dict summarising the episode store.

        Keys: total, succeeded, failed, avg_quality, last_cycle, path
        """
        total = len(self._records)
        succeeded = sum(1 for r in self._records if r.success)
        failed = total - succeeded
        avg_q = sum(r.quality_score for r in self._records) / total if total else 0.0
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "avg_quality": round(avg_q, 4),
            "last_cycle": self._cycle,
            "path": str(self._path),
        }

    def mission_satisfied(self, mission_goal: str, min_quality: float = 0.70) -> bool:
        """
        Return True when there is at least one recent high-quality episode
        whose goal pattern matches *mission_goal* closely enough to be
        considered a completion of the mission.

        Matching is intentionally loose: the normalised mission goal must
        appear as a substring of a recent episode's normalised goal pattern,
        or vice versa.

        Parameters
        ----------
        mission_goal : str   – the high-level mission string to check
        min_quality  : float – minimum composite quality score required
        """
        if not mission_goal.strip():
            return False
        norm_mission = _normalise_goal(mission_goal)
        for rec in reversed(self._records[-20:]):  # scan only the 20 most recent
            if not rec.success:
                continue
            if rec.quality_score < min_quality:
                continue
            if norm_mission in rec.goal_pattern or rec.goal_pattern in norm_mission:
                return True
        return False

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_existing(self) -> None:
        """
        Load existing records from the JSONL file into memory (up to
        ``_max_recent`` most recent).  Sets ``_cycle`` to the highest
        cycle number found.
        """
        if not self._path.exists():
            return
        records: list[EpisodeRecord] = []
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(EpisodeRecord.from_dict(json.loads(line)))
                except Exception:
                    pass  # skip malformed lines silently
        except OSError:
            return

        # Keep only the most recent _max_recent records in memory
        self._records = records[-self._max_recent :]
        if records:
            self._cycle = max(r.cycle for r in records)

    def _append(self, record: EpisodeRecord) -> None:
        """Append *record* to the in-memory list and the JSONL file."""
        # In-memory cap
        self._records.append(record)
        if len(self._records) > self._max_recent:
            self._records = self._records[-self._max_recent :]

        # Disk write
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            # Non-fatal: memory still has the record; log a warning if possible
            try:
                from console import warn  # type: ignore

                warn(f"EpisodeMemory: could not write to {self._path}: {exc}")
            except ImportError:
                pass

    def __len__(self) -> int:
        return len(self._records)

    def __repr__(self) -> str:
        return (
            f"EpisodeMemory(path={self._path!r}, records={len(self._records)}, cycle={self._cycle})"
        )
