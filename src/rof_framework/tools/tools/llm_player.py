"""
tools/tools/llm_player.py
LLMPlayerTool – LLM interaction / script player.
"""

from __future__ import annotations

import copy, csv, hashlib, io, json, logging, math, os, queue, re, shlex, shutil
import subprocess, sys, tempfile, textwrap, threading, time, uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.tools.tools.code_runner import CodeRunnerTool

logger = logging.getLogger("rof.tools")

__all__ = ["LLMPlayerTool"]

# LLMPlayerTool
# Drives any interactive subprocess through its stdin/stdout pipe, using the
# LLM to respond to every input prompt.
# ANSI colour helpers (used by LLMPlayerTool console output)
_USE_COLOUR_TOOLS = (
    sys.stdout.isatty() and os.name != "nt" or (os.name == "nt" and os.environ.get("WT_SESSION"))
)


def _tc(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR_TOOLS else text


def _t_cyan(t: str) -> str:
    return _tc(t, "96")


def _t_green(t: str) -> str:
    return _tc(t, "92")


def _t_bold(t: str) -> str:
    return _tc(t, "1")


def _t_dim(t: str) -> str:
    return _tc(t, "2")


def _t_section(title: str) -> None:
    print(f"\n{_t_dim('-' * 50)}")
    print(f"  {_t_cyan(title)}")
    print(_t_dim("-" * 50))


def _t_info(text: str) -> None:
    print(f"  {_t_dim('     ')}  {text}")


def _t_yellow(t: str) -> str:
    return _tc(t, "93")


def _t_red(t: str) -> str:
    return _tc(t, "91")


def _t_step(label: str, text: str = "") -> None:
    print(f"  {_t_bold(_t_green('[' + label + ']'))}  {text}")


def _t_warn(text: str) -> None:
    print(f"  {_t_yellow('[WARN]')}  {text}")


class LLMPlayerTool(ToolProvider):
    """
    Drives any interactive program (Python, Lua, JS) through its stdin/stdout
    pipe, using the LLM to respond to every input prompt.

    How it works
    ------------
    1. Start the script as a subprocess with stdin/stdout piped.
    2. A background thread reads stdout char-by-char into a queue.
    3. When no new characters arrive for `idle_wait` seconds we assume the
       program is blocked waiting for input.
    4. Show the LLM everything the program has printed and ask what to type.
    5. Write the LLM's answer back to the process stdin.
    6. Repeat until the process exits or `max_turns` is reached.
    7. Save the full transcript to a .txt file in output_dir.

    Parameters
    ----------
    llm : LLMProvider
        The LLM used to decide what to type at each prompt.
    output_dir : Path, optional
        Directory where the transcript .txt file is written.
    idle_wait : float
        Seconds of stdout silence before assuming the program is waiting for input.
    timeout_per_turn : float
        Maximum seconds to wait for the LLM to respond per turn.
    max_turns : int
        Hard cap on the number of input turns.
    system_prompt : str, optional
        Override the default system prompt sent to the LLM.  When omitted a
        generic prompt is used that instructs the LLM to answer input prompts
        with a single line and nothing else.

    Trigger keywords
    ----------------
    "run interactively" / "run with llm" / "let llm drive"
    "automate program" / "llm player" / "simulate input"
    "play interactively" / "play and record" / "run and record"
    """

    _DEFAULT_SYSTEM = (
        "You are controlling an interactive command-line program by typing responses to its prompts. "
        "Read the program's output carefully and decide what to type next. "
        "If the program is waiting for you to press ENTER to continue (e.g. 'Press ENTER…'), "
        "reply with just the word ENTER. "
        "Otherwise reply with ONLY the exact text the program is asking for — one line, "
        "no explanation, no surrounding quotes, no extra punctuation."
    )

    def __init__(
        self,
        llm: "LLMProvider",
        output_dir: Optional[Path] = None,
        idle_wait: float = 0.8,
        timeout_per_turn: float = 15.0,
        max_turns: int = 30,
        system_prompt: Optional[str] = None,
    ):
        self._llm = llm
        self._output_dir = output_dir or Path(tempfile.gettempdir()) / "rof_codegen"
        self._idle_wait = idle_wait
        self._timeout_per_turn = timeout_per_turn
        self._max_turns = max_turns
        self._system_prompt = system_prompt or self._DEFAULT_SYSTEM
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "LLMPlayerTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "run interactively",
            "run with llm",
            "let llm drive",
            "automate program",
            "llm player",
            "simulate input",
            "play interactively",
            "play and record",
            "run and record",
            # kept for backwards-compatibility
            "play game",
            "let llm play",
            "simulate player",
        ]

    # ------------------------------------------------------------------
    def execute(self, request: ToolRequest) -> ToolResponse:
        script_path, lang = self._find_script(request.input)
        if not script_path:
            graph_summary: dict = {}
            for ent_name, ent_data in request.input.items():
                if isinstance(ent_data, dict):
                    graph_summary[ent_name] = {
                        k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
                        for k, v in ent_data.items()
                        if not k.startswith("__")
                    }
            return ToolResponse(
                success=False,
                error=(
                    "LLMPlayerTool: no script found in entity context. "
                    "Make sure AICodeGenTool ran first and its output includes 'saved_to'. "
                    f"Entity graph received: {graph_summary}"
                ),
            )

        # Extract optional per-run overrides from the entity graph.
        # Any entity may carry:
        #   • "system_prompt"  – override the LLM system prompt for this run
        #   • "instructions"   – extra guidance appended to the system prompt
        #   • "max_turns"      – integer cap on input turns
        system_prompt = self._system_prompt
        max_turns = self._max_turns
        for ent_data in request.input.values():
            if not isinstance(ent_data, dict):
                continue
            if ent_data.get("system_prompt"):
                system_prompt = str(ent_data["system_prompt"])
            if ent_data.get("instructions"):
                system_prompt = system_prompt + "\n\n" + str(ent_data["instructions"])
            if ent_data.get("max_turns"):
                try:
                    max_turns = int(ent_data["max_turns"])
                except (ValueError, TypeError):
                    pass

        _t_section("LLMPlayerTool  ->  running program")
        _t_info(f"Script   : {script_path}")
        _t_info(f"Lang     : {lang}")
        _t_info(f"Max turns: {max_turns}")
        print()

        cmd = self._build_cmd(lang, script_path)
        if cmd is None:
            return ToolResponse(success=False, error=f"No runtime found for lang={lang!r}.")

        transcript: list[dict] = []
        log_lines: list[str] = []

        try:
            returncode = self._play(cmd, transcript, log_lines, system_prompt, max_turns)
        except Exception as exc:
            return ToolResponse(success=False, error=f"LLMPlayerTool error: {exc}")

        # ── Save transcript ──────────────────────────────────────────
        ts_text = "\n".join(log_lines)
        ts_name = f"rof_transcript_{int(time.time())}.txt"
        ts_path = self._output_dir / ts_name
        ts_path.write_text(ts_text, encoding="utf-8")

        _t_section(f"Transcript saved  [{ts_name}]")
        for line in log_lines:
            print(f"  {_t_dim(line)}")
        print()
        _t_info(f"Saved to: {_t_bold(str(ts_path))}")
        print()

        return ToolResponse(
            success=True,
            output={
                "transcript": transcript,
                "transcript_file": str(ts_path),
                "turns": len(transcript),
                "script": script_path,
                "returncode": returncode,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_script(self, entity_graph: dict) -> tuple:
        """Search entity attributes for a saved script path.

        Two-pass strategy:
          Pass 1 – look for an explicit 'saved_to' attribute written back
                   by AICodeGenTool (the normal case after the fix).
          Pass 2 – fallback: scan every string attribute value for a path
                   that ends in a known source extension and exists on disk.
                   This handles edge cases where the output format differs.
        """
        lang_fallback = "python"

        # Pass 1: explicit saved_to attribute
        for entity_data in entity_graph.values():
            if not isinstance(entity_data, dict):
                continue
            saved = entity_data.get("saved_to")
            if saved and Path(saved).exists():
                lang = entity_data.get("language", lang_fallback)
                return str(saved), str(lang).lower()
            if entity_data.get("language"):
                lang_fallback = str(entity_data["language"]).lower()

        # Pass 2: scan all string values for file paths
        _src_exts = {".py": "python", ".lua": "lua", ".js": "javascript"}
        for entity_data in entity_graph.values():
            if not isinstance(entity_data, dict):
                continue
            for v in entity_data.values():
                if not isinstance(v, str):
                    continue
                p = Path(v)
                if p.suffix in _src_exts and p.exists():
                    return str(p), _src_exts[p.suffix]

        return None, lang_fallback

    def _build_cmd(self, lang: str, path: str) -> Optional[list]:
        import shutil

        if lang in ("python", "py"):
            return [sys.executable, path]
        if lang == "lua":
            for b in ("lua", "lua5.4", "lua5.3", "luajit"):
                if shutil.which(b):
                    return [b, path]
        if lang in ("javascript", "js"):
            for b in ("node", "nodejs"):
                if shutil.which(b):
                    return [b, path]
        return None

    def _play(
        self,
        cmd: list,
        transcript: list,
        log_lines: list,
        system_prompt: Optional[str] = None,
        max_turns: Optional[int] = None,
    ) -> int:
        """Drive the subprocess interactively. Returns the process exit code."""
        if max_turns is None:
            max_turns = self._max_turns
        if system_prompt is None:
            system_prompt = self._system_prompt

        # Force UTF-8 encoding for subprocess (especially important on Windows)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=0,
            env=env,
        )

        out_q: queue.Queue = queue.Queue()

        def _reader() -> None:
            try:
                while True:
                    ch = proc.stdout.read(1)  # type: ignore[union-attr]
                    if ch == "":
                        break
                    out_q.put(ch)
            except Exception:
                pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        turns = 0
        while turns < max_turns:
            buf: list[str] = []
            deadline = time.time() + self._idle_wait

            while time.time() < deadline:
                try:
                    ch = out_q.get(timeout=0.05)
                    buf.append(ch)
                    deadline = time.time() + self._idle_wait
                except queue.Empty:
                    if proc.poll() is not None:
                        while not out_q.empty():
                            try:
                                buf.append(out_q.get_nowait())
                            except queue.Empty:
                                break
                        break

            game_output = "".join(buf).rstrip()

            if game_output:
                print(f"  {_t_cyan('[OUT]')}  {game_output}")
                log_lines.append(f"[OUT]  {game_output}")

            if proc.poll() is not None:
                break

            if not game_output:
                time.sleep(0.1)
                turns += 1
                continue

            # Show the LLM what the program printed and ask what to type.
            prompt = (
                f"The program printed the following:\n\n"
                f"{game_output}\n\n"
                f"What do you type in response? "
                f"If the program is only asking you to press ENTER to continue, "
                f"reply with just the word ENTER (nothing else). "
                f"Otherwise reply with ONLY the exact input the program is asking for. "
                f"One line only, no explanation."
            )
            try:
                llm_resp = self._llm.complete(
                    LLMRequest(
                        prompt=prompt,
                        system=system_prompt,
                        max_tokens=20,
                        temperature=0.7,
                        output_mode="raw",  # single-word player input — not RL/JSON
                    )
                )
            except Exception as exc:
                raise RuntimeError(f"LLM call failed during play: {exc}") from exc

            player_input = llm_resp.content.strip().splitlines()[0].strip()

            # If LLM responds with "ENTER", send empty line
            if player_input.upper() == "ENTER":
                actual_input = ""
                display_input = "<ENTER>"
            else:
                actual_input = player_input
                display_input = player_input

            print(f"  {_t_green('[LLM]')}  {display_input}")
            log_lines.append(f"[LLM]  {display_input}")
            transcript.append({"game_output": game_output, "llm_choice": player_input})

            try:
                proc.stdin.write(actual_input + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
            except BrokenPipeError:
                break

            turns += 1

        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()

        t.join(timeout=2.0)
        return proc.returncode if proc.returncode is not None else 0


