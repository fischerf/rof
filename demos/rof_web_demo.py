"""
rof_web_demo.py – WebSearchTool smoke-test
==========================================
Tests httpx and ddgs both directly and through the
rof-tools WebSearchTool / ToolRouter pipeline.

Install dependencies first:
    pip install httpx ddgs

Run:
    python rof_web_demo.py

Optional – use rof-tools alongside this file:
    Place rof_tools.py (or rof-tools.py renamed) next to this script.
    If rof_tools is not importable the script falls back to stand-alone mode.
"""

from __future__ import annotations

import sys
import time

# ---------------------------------------------------------------------------
# 0.  Windows-safe output (same guard as rof-tools)
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SEP = "-" * 60
SEPP = "=" * 60


def header(title: str) -> None:
    print(f"\n{SEPP}\n  {title}\n{SEPP}")


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def err(msg: str) -> None:
    print(f"  [ERR]  {msg}")


def info(msg: str) -> None:
    print(f"         {msg}")


# ---------------------------------------------------------------------------
# 1.  Dependency check
# ---------------------------------------------------------------------------
header("Dependency check")


def _check(pkg: str, import_name: str | None = None) -> bool:
    name = import_name or pkg
    try:
        __import__(name)
        ok(f"{pkg} is installed")
        return True
    except ImportError:
        err(f"{pkg} NOT found  ->  pip install {pkg}")
        return False


has_httpx = _check("httpx")
has_ddg = _check("ddgs", "ddgs")

if not has_httpx and not has_ddg:
    print("\n  Nothing to test. Install the packages and re-run.\n")
    sys.exit(1)


# ===========================================================================
# PART A – Direct httpx tests (no rof-tools dependency)
# ===========================================================================
if has_httpx:
    import httpx

    # -----------------------------------------------------------------------
    # A-1: Synchronous GET – public JSON API (no auth required)
    # -----------------------------------------------------------------------
    header("A-1  httpx  –  synchronous GET  (httpbin.org/get)")

    URL = "https://httpbin.org/get"
    try:
        t0 = time.perf_counter()
        resp = httpx.get(URL, params={"rof_test": "hello"}, timeout=10.0)
        ms = int((time.perf_counter() - t0) * 1000)
        resp.raise_for_status()
        data = resp.json()

        ok(f"Status {resp.status_code}  ({ms} ms)")
        info(f"URL returned: {data['url']}")
        info(f"Args echoed:  {data.get('args')}")
        info(f"HTTP version: {resp.http_version}")
    except Exception as e:
        err(f"GET failed: {e}")

    # -----------------------------------------------------------------------
    # A-2: Synchronous POST with JSON body
    # -----------------------------------------------------------------------
    header("A-2  httpx  –  synchronous POST  (httpbin.org/post)")

    try:
        payload = {
            "entity": "Customer",
            "total_purchases": 15000,
            "segment": "HighValue",
        }
        resp = httpx.post("https://httpbin.org/post", json=payload, timeout=10.0)
        resp.raise_for_status()
        echoed = resp.json().get("json", {})
        ok(f"Status {resp.status_code}")
        info(f"Body echoed back: {echoed}")
    except Exception as e:
        err(f"POST failed: {e}")

    # -----------------------------------------------------------------------
    # A-3: Async GET – shows httpx async client
    # -----------------------------------------------------------------------
    header("A-3  httpx  –  async GET  (httpbin.org/uuid)")

    import asyncio

    async def async_get_uuid() -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://httpbin.org/uuid")
            resp.raise_for_status()
            uuid = resp.json().get("uuid", "?")
            ok(f"Status {resp.status_code}")
            info(f"Random UUID from server: {uuid}")

    try:
        asyncio.run(async_get_uuid())
    except Exception as e:
        err(f"Async GET failed: {e}")

    # -----------------------------------------------------------------------
    # A-4: Error handling – 404 and timeout
    # -----------------------------------------------------------------------
    header("A-4  httpx  –  error handling (404 + timeout)")

    # 404
    try:
        resp = httpx.get("https://httpbin.org/status/404", timeout=5.0)
        if resp.status_code == 404:
            ok(f"Got expected 404  (raise_for_status would raise HTTPStatusError)")
        else:
            info(f"Unexpected status: {resp.status_code}")
    except Exception as e:
        err(f"Unexpected exception: {e}")

    # Deliberate timeout (connect to a non-routable IP)
    try:
        httpx.get("http://10.255.255.1/", timeout=2.0)
        err("Expected timeout but request succeeded – unexpected!")
    except httpx.TimeoutException:
        ok("Timeout raised as httpx.TimeoutException  (correct behaviour)")
    except Exception as e:
        # Could also be a connect error on some networks
        ok(f"Connection refused / timed out as expected: {type(e).__name__}")

    # -----------------------------------------------------------------------
    # A-5: Custom headers + response inspection
    # -----------------------------------------------------------------------
    header("A-5  httpx  –  custom headers & response inspection")

    try:
        headers = {
            "X-ROF-Version": "3.0",
            "Accept": "application/json",
            "User-Agent": "rof-tools/3.0 (httpx)",
        }
        resp = httpx.get("https://httpbin.org/headers", headers=headers, timeout=10.0)
        resp.raise_for_status()
        echoed_headers = resp.json().get("headers", {})
        ok(f"Status {resp.status_code}")
        info(f"X-ROF-Version echoed: {echoed_headers.get('X-Rof-Version', '?')}")
        info(f"User-Agent echoed:    {echoed_headers.get('User-Agent', '?')}")
    except Exception as e:
        err(f"Headers test failed: {e}")


# ===========================================================================
# PART B – Direct ddgs tests
# ===========================================================================
if has_ddg:
    from ddgs import DDGS

    # -----------------------------------------------------------------------
    # B-1: Text search
    # -----------------------------------------------------------------------
    header("B-1  ddgs  –  text search")

    QUERY = "RelateLang LLM prompt engineering declarative"
    try:
        with DDGS(timeout=20) as ddgs:
            results = list(ddgs.text(QUERY, max_results=3))

        ok(f"Query: {QUERY!r}")
        info(f"Results returned: {len(results)}")
        print()
        for i, r in enumerate(results, 1):
            print(f"  [{i}] {r.get('title', 'no title')}")
            print(f"       {r.get('href', 'no url')}")
            snippet = r.get("body", "")[:120].replace("\n", " ")
            print(f"       {snippet}...")
            print()
    except Exception as e:
        err(f"Text search failed: {e}")

    # -----------------------------------------------------------------------
    # B-2: News search
    # -----------------------------------------------------------------------
    header("B-2  ddgs  –  news search")

    try:
        with DDGS(timeout=20) as ddgs:
            news = list(ddgs.news("Python programming language 2025", max_results=3))

        ok(f"News articles returned: {len(news)}")
        print()
        for i, n in enumerate(news, 1):
            print(f"  [{i}] {n.get('title', 'no title')}")
            print(
                f"       Source : {n.get('source', '?')}  |  Date: {n.get('date', '?')}"
            )
            print(f"       URL    : {n.get('url', '?')}")
            print()
    except Exception as e:
        err(f"News search failed: {e}")

    # -----------------------------------------------------------------------
    # B-3: Structured result fields
    # -----------------------------------------------------------------------
    header("B-3  ddgs  –  inspect result structure")

    try:
        with DDGS(timeout=20) as ddgs:
            sample = list(ddgs.text("httpx Python async HTTP client", max_results=1))

        if sample:
            r = sample[0]
            ok("Raw result dict keys and values:")
            for k, v in r.items():
                preview = str(v)[:80].replace("\n", " ")
                info(f"  {k:12s} = {preview}")
        else:
            info("No results returned.")
    except Exception as e:
        err(f"Structure inspection failed: {e}")


# ===========================================================================
# PART C – rof-tools WebSearchTool integration
#           (only runs if rof_tools is importable)
# ===========================================================================
header("C    rof-tools WebSearchTool integration")

try:
    import importlib
    import importlib.util
    import sys as _sys

    spec = importlib.util.find_spec("rof_framework.rof_tools")
    if spec is None:
        raise ImportError("rof_framework.rof_tools not on path")
    from rof_framework.rof_tools import (  # type: ignore
        RoutingStrategy,
        ToolRegistry,
        ToolRequest,
        ToolRouter,
        WebSearchTool,
    )

    _has_rof = True
except ImportError:
    _has_rof = False
    info("rof_framework.rof_tools not importable – skipping integration test.")

if _has_rof:
    # C-1: WebSearchTool direct call
    header("C-1  WebSearchTool  –  direct execute()")

    if has_ddg:
        tool = WebSearchTool(backend="duckduckgo", max_results=3)
        req = ToolRequest(
            name="WebSearchTool",
            goal='retrieve web_information about "large language model" prompt engineering 2025',
        )
        try:
            t0 = time.perf_counter()
            resp = tool.execute(req)
            ms = int((time.perf_counter() - t0) * 1000)

            if resp.success:
                ok(f"WebSearchTool succeeded  ({ms} ms)")
                info(f"Query auto-extracted: {resp.output['query']!r}")
                info(f"Results count:        {len(resp.output['results'])}")
                print()
                print("  --- RL context snippet (first 600 chars) ---")
                print(resp.output["rl_context"][:600])
                print("  ...")
            else:
                err(f"WebSearchTool failed: {resp.error}")
        except Exception as e:
            err(f"Exception: {e}")
    else:
        info("Skipping – ddgs not installed.")

    # C-2: WebSearchTool via ToolRouter
    header("C-2  ToolRouter  ->  WebSearchTool routing")
    from rof_framework.rof_tools import (  # type: ignore
        RoutingStrategy,
        ToolRegistry,
        ToolRouter,
        WebSearchTool,
    )

    registry = ToolRegistry()
    registry.register(
        WebSearchTool(
            backend="duckduckgo" if has_ddg else "auto",
            max_results=2,
        )
    )
    router = ToolRouter(registry, strategy=RoutingStrategy.KEYWORD)

    goals = [
        "retrieve web_information about Python asyncio",
        "search web for latest AI news",
        "look up RelateLang documentation",
        "ensure determine Customer segment",  # should NOT match -> None
    ]

    for goal in goals:
        result = router.route(goal)
        name = result.tool.name if result.tool else "no match"
        print(f"  {goal[:55]!r:58s}  ->  {name}  (conf={result.confidence:.2f})")

    # C-3: httpx-based APICallTool smoke test
    if has_httpx:
        header("C-3  APICallTool  –  httpx under the hood")

        try:
            from rof_framework import rof_tools as _rof_tools  # type: ignore

            APICallTool = _rof_tools.APICallTool

            api = APICallTool(default_timeout=10.0)
            resp = api.execute(
                ToolRequest(
                    name="APICallTool",
                    input={
                        "url": "https://httpbin.org/get",
                        "method": "GET",
                        "params": {"tool": "APICallTool", "version": "3.0"},
                    },
                )
            )
            if resp.success:
                ok(
                    f"Status {resp.output['status_code']}  ({resp.output['elapsed_ms']} ms)"
                )
                info(f"Args echoed: {resp.output['body'].get('args')}")
            else:
                err(f"APICallTool failed: {resp.error}")
        except Exception as e:
            err(f"Exception: {e}")


# ===========================================================================
# Summary
# ===========================================================================
header("Summary")

rows = [
    ("httpx installed", has_httpx),
    ("ddgs installed", has_ddg),
    ("rof_tools importable", _has_rof if "_has_rof" in dir() else False),
]
for label, status in rows:
    mark = "[OK]" if status else "[--]"
    print(f"  {mark}  {label}")

print(f"\n  All done.\n")
