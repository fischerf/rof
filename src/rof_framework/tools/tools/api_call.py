"""
tools/tools/api_call.py
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
from rof_framework.core.parser.rl_parser import RLParser

logger = logging.getLogger("rof.tools")


__all__ = ["APICallTool"]

# rof_tools/tools/api_call.py
class APICallTool(ToolProvider):
    """
    Generic HTTP REST caller.

    Input (ToolRequest.input):
        url (str)          – required
        method (str)       – GET | POST | PUT | PATCH | DELETE  (default GET)
        headers (dict)     – extra HTTP headers
        params (dict)      – query-string parameters
        body (dict|str)    – request body (serialised as JSON for dicts)
        auth_bearer (str)  – Authorization: Bearer <token>
        timeout (float)    – per-request timeout (default from constructor)

    Output (ToolResponse.output):
        dict with status_code, headers, body (parsed JSON or raw text), elapsed_ms

    Usage:
        api = APICallTool(default_timeout=10.0)
        resp = api.execute(ToolRequest(
            name="APICallTool",
            input={
                "url": "https://api.github.com/repos/python/cpython",
                "method": "GET",
                "headers": {"Accept": "application/vnd.github+json"},
            },
        ))
        print(resp.output["body"]["full_name"])  # python/cpython
    """

    def __init__(
        self,
        default_timeout: float = 15.0,
        default_headers: Optional[dict] = None,
        base_url: str = "",
        auth_bearer: Optional[str] = None,
    ):
        self._default_timeout = default_timeout
        self._default_headers = default_headers or {}
        self._base_url = base_url.rstrip("/")
        self._auth_bearer = auth_bearer

    @property
    def name(self) -> str:
        return "APICallTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "call api",
            "http request",
            "rest call",
            "api request",
            "fetch url",
            "http get",
            "http post",
            "web request",
            "call endpoint",
            "invoke api",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        try:
            import httpx  # type: ignore
        except ImportError:
            return ToolResponse(
                success=False,
                error="httpx not installed. Run: pip install httpx",
            )

        url = self._base_url + request.input.get("url", "")
        method = request.input.get("method", "GET").upper()
        headers = {**self._default_headers, **request.input.get("headers", {})}
        params = request.input.get("params")
        body = request.input.get("body")
        timeout = request.input.get("timeout", self._default_timeout)
        bearer = request.input.get("auth_bearer") or self._auth_bearer

        # ── Snapshot-entity fallback (orchestrator call) ──────────────────
        # Scan ALL entities: pick up url from whichever entity holds it,
        # and merge method / timeout / headers / auth from any entity.
        if not url:
            for _ename, edata in request.input.items():
                if not isinstance(edata, dict):
                    continue
                # URL + body/params come from the entity that owns the url
                if "url" in edata and not url:
                    url = self._base_url + edata.get("url", "")
                    params = edata.get("params", params)
                    body = edata.get("body", body)
                # method / timeout / auth can live in any entity (e.g. Request)
                if "method" in edata:
                    method = edata["method"].upper()
                if "timeout" in edata:
                    timeout = edata["timeout"]
                if isinstance(edata.get("headers"), dict):
                    headers = {**headers, **edata["headers"]}
                # header_<name> → HTTP header  (e.g. header_accept → Accept)
                for k, v in edata.items():
                    if k.startswith("header_") and isinstance(v, str):
                        hname = k[len("header_") :].replace("_", "-").title()
                        headers.setdefault(hname, v)
                if edata.get("auth_bearer"):
                    bearer = bearer or edata["auth_bearer"]

        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        if not url:
            return ToolResponse(success=False, error="No URL provided in input.")

        try:
            kwargs: dict[str, Any] = {
                "method": method,
                "url": url,
                "headers": headers,
                "timeout": timeout,
            }
            if params:
                kwargs["params"] = params
            if body is not None:
                if isinstance(body, dict):
                    kwargs["json"] = body
                    headers.setdefault("Content-Type", "application/json")
                else:
                    kwargs["content"] = str(body)

            start = time.perf_counter()
            resp = httpx.request(**kwargs)
            elapsed_ms = int((time.perf_counter() - start) * 1000)

            try:
                resp_body = resp.json()
            except Exception:
                resp_body = resp.text

            success = 200 <= resp.status_code < 300
            # Wrap in an entity dict so _execute_tool_step stores it in the graph.
            # body may be a dict (JSON) or str (plain text); normalise to str so
            # set_attribute() always receives a scalar and downstream tools can
            # read it as an attribute without further unwrapping.
            body_str = resp_body if isinstance(resp_body, str) else json.dumps(resp_body)
            output = {
                "APICallResult": {
                    "status_code": resp.status_code,
                    "body": body_str[:4000],  # guard against huge payloads
                    "elapsed_ms": elapsed_ms,
                    "success": success,
                },
            }
            return ToolResponse(
                success=success,
                output=output,
                error="" if success else f"HTTP {resp.status_code}",
            )

        except Exception as e:
            logger.error("APICallTool failed: %s", e)
            return ToolResponse(success=False, error=str(e))


