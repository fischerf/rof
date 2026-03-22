# MCP Integration — ROF as an MCP Client

> **Requires:** `pip install mcp>=1.0`  
> **Optional dep group:** `pip install "rof[mcp]"`

---

## What this enables

The [Model Context Protocol](https://modelcontextprotocol.io) (MCP) is an open
standard for connecting AI applications to external tools and data sources.
ROF's MCP integration lets any MCP server — local subprocess or remote HTTP —
appear as a first-class `ToolProvider` inside a ROF `ToolRegistry`.

Once registered, an MCP server is indistinguishable from a built-in tool like
`WebSearchTool` or `DatabaseTool`:

- The **ToolRouter** routes `.rl` goals to it by keyword or embedding.
- The **Orchestrator** calls it during workflow execution and writes results
  into the `WorkflowGraph`.
- The **Pipeline** can include it in any stage, including fan-out groups.

```
.rl file  →  Orchestrator  →  ToolRouter  →  MCPClientTool
                                                   │
                                          MCP stdio / HTTP
                                                   │
                                       External MCP Server
                                   (filesystem, Sentry, GitHub …)
```

---

## Quick start

### 1. Install the dependency

```bash
pip install mcp>=1.0
# or, together with all other ROF optional dependencies:
pip install "rof[all]"
```

### 2. Configure a server

```python
from rof_framework.tools.tools.mcp import MCPServerConfig

# Local subprocess (stdio transport)
fs_cfg = MCPServerConfig.stdio(
    name="filesystem",
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/my/project"],
    trigger_keywords=["read file", "list directory", "write file"],
)

# Remote HTTP server (Streamable HTTP transport)
sentry_cfg = MCPServerConfig.http(
    name="sentry",
    url="https://mcp.sentry.io/mcp",
    auth_bearer=os.environ["SENTRY_MCP_TOKEN"],
    trigger_keywords=["sentry error", "exception tracking"],
)
```

### 3. Register and run

```python
from rof_framework.tools.tools.mcp import MCPClientTool
from rof_framework.tools.registry.tool_registry import ToolRegistry
from rof_framework.core.orchestrator.orchestrator import Orchestrator
from rof_framework.core.parser.rl_parser import RLParser

registry = ToolRegistry()
registry.register(MCPClientTool(fs_cfg), tags=["mcp", "filesystem"])
registry.register(MCPClientTool(sentry_cfg), tags=["mcp", "external"])

parser = RLParser()
ast    = parser.parse(Path("my_workflow.rl").read_text())

orch = Orchestrator(
    llm_provider=my_llm,
    tools=list(registry.all_tools().values()),
)
result = orch.run(ast)
```

### 4. Use `create_default_registry` (recommended)

The simplest way to add MCP servers alongside all built-in tools:

```python
from rof_framework.tools import create_default_registry
from rof_framework.tools.tools.mcp import MCPServerConfig

registry = create_default_registry(
    web_search_backend="duckduckgo",
    mcp_servers=[
        MCPServerConfig.stdio(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "."],
            trigger_keywords=["read file", "list directory"],
        ),
        MCPServerConfig.http(
            name="sentry",
            url="https://mcp.sentry.io/mcp",
            auth_bearer=os.environ["SENTRY_MCP_TOKEN"],
        ),
    ],
    mcp_eager_connect=False,   # lazy connect on first execute() (default)
)
# registry now contains WebSearchTool, RAGTool, DatabaseTool …
# … AND MCPClientTool[filesystem], MCPClientTool[sentry]
```

---

## Core concepts

### MCPServerConfig

Describes how to reach one MCP server. Two factory classmethods cover both
transport types supported by the MCP specification:

| Factory | Transport | Requires |
|---|---|---|
| `MCPServerConfig.stdio(name, command, args)` | STDIO — spawns a local subprocess | Node.js / Python / any binary |
| `MCPServerConfig.http(name, url, auth_bearer)` | Streamable HTTP — connects to a remote URL | A running HTTP MCP server |

**Key parameters:**

```python
MCPServerConfig(
    name             = "filesystem",   # unique identifier; used as namespace prefix
    transport        = MCPTransport.STDIO,
    command          = "npx",          # stdio only
    args             = [...],          # stdio only
    env              = {},             # extra env vars for the subprocess
    url              = "",             # http only
    auth_bearer      = "",             # http only — Authorization: Bearer <token>
    auth_headers     = {},             # http only — arbitrary extra headers
    trigger_keywords = [...],          # routing hints (merged with auto-discovery)
    connect_timeout  = 30.0,           # seconds for the MCP handshake
    call_timeout     = 60.0,           # seconds per tools/call
    auto_discover    = True,           # run tools/list on connect
    namespace_tools  = True,           # prefix tool names with "<name>/"
)
```

### MCPClientTool

A `ToolProvider` that wraps one `MCPServerConfig`. Its key behaviours:

**Lazy connection.** The MCP subprocess or HTTP session is not opened until
the first `execute()` call. This keeps startup fast and matches the behaviour
of all other built-in ROF tools.

**Auto-discovery.** On connect, `tools/list` is called. Tool names and
descriptions are parsed into `trigger_keywords`, which the `ToolRouter` uses
immediately for subsequent goal routing within the same run.

**Tool name resolution.** When the Orchestrator routes a goal to
`MCPClientTool`, it resolves which specific MCP tool to call in this order:

1. Exact match on `<server>/<mcp_tool_name>` (namespaced form).
2. Exact match on `<mcp_tool_name>` (unqualified).
3. Substring of the goal expression against known tool names.
4. Keyword overlap between goal words and tool name + description.
5. First discovered tool (last resort).

**Output format.** Results are returned as an entity-keyed dict so the
Orchestrator writes them into the `WorkflowGraph` as attributes, just like
any other tool:

```python
{
    "MCPResult": {
        "server": "filesystem",
        "tool":   "read_file",
        "result": "<file contents>",
        "success": True,
    }
}
```

Downstream `.rl` goals and LLM context injection can then reference
`MCPResult.result` as a normal entity attribute.

**Dedicated event loop.** Each `MCPClientTool` manages its own background
`asyncio` event loop thread. This bridges the MCP SDK's async API into ROF's
synchronous `Orchestrator` without interfering with any outer event loop in
the host application (FastAPI, uvicorn, etc.).

### MCPToolFactory

Builds and registers multiple `MCPClientTool` instances in one call. The
canonical choice when you have several MCP servers to wire up:

```python
from rof_framework.tools.tools.mcp import MCPToolFactory, MCPServerConfig

configs = [
    MCPServerConfig.stdio("filesystem", "npx",
                          ["-y", "@modelcontextprotocol/server-filesystem", "."]),
    MCPServerConfig.http("github", "https://api.githubcopilot.com/mcp",
                         auth_bearer=os.environ["GITHUB_TOKEN"]),
    MCPServerConfig.http("sentry", "https://mcp.sentry.io/mcp",
                         auth_bearer=os.environ["SENTRY_MCP_TOKEN"]),
]

factory = MCPToolFactory(configs, eager_connect=False, tags=["mcp"])
tools   = factory.build_and_register(registry)

# … run workflows …

factory.close_all()   # cleanly terminate all subprocess / HTTP connections
```

`MCPToolFactory` logs and skips individual servers that fail to build (e.g. a
missing command), so a single broken config does not prevent the remaining
servers from registering.

---

## Writing `.rl` goals for MCP tools

Goals are matched to tools by the `ToolRouter` using keyword similarity.
Write goals that include words from the MCP tool's name or description.
With `namespace_tools=True` (default), you can also use the namespaced form.

```
# filesystem server exposes: read_file, write_file, list_directory

ensure read file /docs/README.md.
ensure list directory /src.
ensure write file /output/report.md.

# namespaced form — unambiguous when multiple servers are registered
ensure filesystem/read_file /config/settings.json.
```

If a goal is ambiguous (two servers have similar keywords), add explicit
`trigger_keywords` to the `MCPServerConfig` of the server that should win,
or use the namespaced form in the `.rl` goal.

---

## Passing arguments to MCP tools

By default, `MCPClientTool` forwards `ToolRequest.input` as the MCP tool
arguments, after flattening any entity-snapshot nesting the Orchestrator adds.

For direct calls or custom pipeline stages, the cleanest approach is to pass
arguments under the `__mcp_args__` key — this bypasses the flattening logic:

```python
from rof_framework.core.interfaces.tool_provider import ToolRequest

resp = tool.execute(ToolRequest(
    name="filesystem/read_file",
    input={"__mcp_args__": {"path": "/docs/README.md"}},
    goal="read file /docs/README.md",
))
```

When calling through the Orchestrator (the normal path), set entity attributes
in the `.rl` file. The Orchestrator passes all entity attributes as the tool
input, and `MCPClientTool` merges them into a flat arguments dict:

```
FileRequest has path of "/docs/README.md".

ensure read file /docs/README.md.
```

The entity `FileRequest` is injected into the `ToolRequest.input`, and the
`path` attribute becomes the `path` argument of the MCP `read_file` call.

---

## Authentication

### Bearer token (HTTP servers)

```python
MCPServerConfig.http(
    name="my-server",
    url="https://mcp.example.com/mcp",
    auth_bearer=os.environ["MY_MCP_TOKEN"],   # → Authorization: Bearer <token>
)
```

### Arbitrary HTTP headers

```python
MCPServerConfig.http(
    name="my-server",
    url="https://mcp.example.com/mcp",
    auth_headers={
        "X-Api-Key":    os.environ["MY_API_KEY"],
        "X-Workspace":  "my-org",
    },
)
```

### Environment variables for stdio servers

```python
MCPServerConfig.stdio(
    name="my-local-server",
    command="python",
    args=["-m", "my_mcp_server"],
    env={
        "MY_SERVER_API_KEY": os.environ["MY_SERVER_API_KEY"],
        "MY_SERVER_ENV":     "production",
    },
)
```

---

## Lifecycle and shutdown

`MCPClientTool` holds a live subprocess or HTTP connection. Call `close()`
when the tool is no longer needed, or use `MCPToolFactory.close_all()` for
bulk teardown:

```python
# Single tool
with MCPClientTool(cfg) as tool:
    registry.register(tool)
    result = orch.run(ast)
# tool.close() called automatically on __exit__

# Factory (recommended for multiple servers)
factory = MCPToolFactory(configs)
tools   = factory.build_and_register(registry)
try:
    run_application()
finally:
    factory.close_all()
```

In FastAPI / `rof_bot`, add `factory.close_all()` to the lifespan shutdown
handler:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    factory = MCPToolFactory(mcp_configs)
    factory.build_and_register(registry)
    yield
    factory.close_all()
```

Eager connection (opens sessions at startup rather than on first call):

```python
factory = MCPToolFactory(configs, eager_connect=True)
# or via create_default_registry:
registry = create_default_registry(mcp_servers=configs, mcp_eager_connect=True)
```

This surfaces misconfiguration errors (wrong command, unreachable URL, bad
token) at startup rather than silently during the first workflow execution.

---

## Multiple MCP servers

When two or more servers expose tools with the same name (e.g. both a
`filesystem` server and a `github` server expose a `read_file` tool),
`namespace_tools=True` (the default) prefixes each tool name with the server's
`name`:

```
filesystem/read_file   ← from the filesystem server
github/read_file       ← from the github server
```

Use the namespaced form in `.rl` goals when you need to be explicit:

```
ensure filesystem/read_file /local/data.csv.
ensure github/read_file owner=myorg repo=myrepo path=README.md.
```

Set `namespace_tools=False` only when a single MCP server is registered and
you want unqualified tool names.

---

## Example: multi-server workflow

```
# multi_source_report.rl

define Report      as "The final compiled report".
define LocalData   as "Data read from the local filesystem".
define ErrorLog    as "Recent errors from Sentry".

LocalData  has path of "/data/metrics.json".
Report     has title of "Weekly Status Report".

ensure read file /data/metrics.json.
ensure retrieve sentry errors for project "my-project".
ensure write file /output/report.md.
```

```python
registry = create_default_registry(
    mcp_servers=[
        MCPServerConfig.stdio(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/"],
            trigger_keywords=["read file", "write file", "list directory"],
        ),
        MCPServerConfig.http(
            name="sentry",
            url="https://mcp.sentry.io/mcp",
            auth_bearer=os.environ["SENTRY_MCP_TOKEN"],
            trigger_keywords=["sentry errors", "retrieve sentry", "error log"],
        ),
    ],
)

orch   = Orchestrator(llm_provider=llm, tools=list(registry.all_tools().values()))
result = orch.run(RLParser().parse(Path("multi_source_report.rl").read_text()))
```

---

## Running the demo

A runnable demo covering all integration points ships with the repository:

```bash
# Offline — no external dependencies, uses a mock filesystem tool
python demos/mcp_client_demo.py --mode mock

# Real filesystem server (requires Node.js + npx)
python demos/mcp_client_demo.py --mode filesystem --dir /tmp

# Remote HTTP server
python demos/mcp_client_demo.py --mode http --url https://mcp.example.com/mcp

# Verbose MCP protocol logging
python demos/mcp_client_demo.py --mode mock --verbose
```

---

## Reference

| Symbol | Location | Description |
|---|---|---|
| `MCPServerConfig` | `rof_framework.tools.tools.mcp.config` | Transport config for one MCP server |
| `MCPTransport` | `rof_framework.tools.tools.mcp.config` | `STDIO` \| `HTTP` |
| `MCPClientTool` | `rof_framework.tools.tools.mcp.client_tool` | `ToolProvider` wrapping one MCP server |
| `MCPToolFactory` | `rof_framework.tools.tools.mcp.factory` | Builds and registers multiple tools |
| All four | `rof_framework.tools` | Re-exported from the top-level tools package |
| `create_default_registry` | `rof_framework.tools.registry.factory` | Accepts `mcp_servers=` and `mcp_eager_connect=` |

All four symbols are also importable from the top-level package:

```python
from rof_framework.tools import MCPServerConfig, MCPTransport, MCPClientTool, MCPToolFactory
```
