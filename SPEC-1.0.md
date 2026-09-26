# RAPP Ecosystem Access Specification (rapp-mcp)

> **Spec version:** `rapp-mcp-spec/1.0`
> **Status:** stable · additive-only
> **Scope:** how any AI / MCP host joins the RAPP ecosystem through the Model Context Protocol.

---

## 1. Manifesto — AIs joining the RAPP ecosystem

RAPP (Rapid Agent Prototype Platform) is an **engine, not an experience**. It runs as three
self-contained tiers — Brainstem (local), Spinal Cord (Azure), Nervous System (Microsoft 365) —
and the user advances from one to the next *only when they choose to*.

`rapp-mcp` is the layer that lets **any AI** join that engine.

Today an AI is trapped inside whatever host it ships in. Claude Desktop, GitHub Copilot CLI,
Cursor, and every other Model Context Protocol (MCP) host each speak the same wire protocol, yet
none of them can natively *use the user's own agents* or *the user's own on-device brain*. RAPP
already has both: drop-in `*_agent.py` agents and a local Brainstem that orchestrates them through
a single endpoint. The only thing missing was a doorway.

`rapp-mcp` is that doorway. It is two tiny, dependency-free MCP servers that present RAPP to any
MCP host:

- one serves a **folder of agents** as individual MCP tools;
- one serves a **whole running Brainstem** as a tool (its full LLM + memory + agent loop).

The design goals are deliberately narrow:

- **Universal** — pure Python standard library, no SDK, works in any host that speaks MCP.
- **Deterministic** — *the bytes are the contract*. The same `*_agent.py` behaves identically on
  every machine; nothing is hidden in a registry or a build step.
- **Portable** — an agent that runs here runs unchanged on Tier 2 (Azure) and Tier 3 (M365). The
  on-ramp does not fork the contract.
- **Local-first** — everything executes on the user's device, under the user's own credentials,
  until the user explicitly promotes a capability to the cloud.

When an AI joins through `rapp-mcp` it gets the full on-device experience — agents plus the
Brainstem brain — and, when the user is ready, it can ask the Brainstem to **automate promotion**
to Azure (Tier 2) and Microsoft 365 (Tier 3). One protocol in; the entire RAPP ladder out.

---

## 2. Architecture overview

```
                       ┌──────────────────────────────────────────────┐
                       │            Any MCP host / any AI              │
                       │  Claude Desktop · Copilot CLI · Cursor · …    │
                       └───────────────┬──────────────────────────────┘
                                       │  MCP (JSON-RPC over stdio)
                 ┌─────────────────────┴─────────────────────┐
                 ▼                                            ▼
   ┌──────────────────────────┐               ┌──────────────────────────────┐
   │        rapp-mcp           │               │      rapp-brainstem-mcp       │
   │  (rapp_mcp.py)            │               │  (rapp_brainstem_mcp.py)      │
   │                           │               │                              │
   │  serves a FOLDER of       │               │  bridges a RUNNING Brainstem  │
   │  *_agent.py as tools      │               │  tools: brainstem,            │
   │  (hotload, stateless)     │               │  brainstem_status,            │
   │                           │               │  brainstem_bootstrap          │
   └───────────┬──────────────┘               └───────────────┬──────────────┘
               │ direct .perform()                            │ HTTP POST /chat
               ▼                                              ▼
   ┌──────────────────────────┐               ┌──────────────────────────────┐
   │  agents/*_agent.py        │               │   RAPP Brainstem (Tier 1)     │
   │  each = one MCP tool      │◀──────────────│   Flask :7071 · POST /chat    │
   └──────────────────────────┘   same agent  │   LLM + memory + agent loop   │
                                   contract    └───────────────┬──────────────┘
                                                               │ promotion agents
                                                               │ (invoked when ready)
                          ┌────────────────────────────────────┼───────────────────────────┐
                          ▼                                                                  ▼
        ┌──────────────────────────────────┐                       ┌──────────────────────────────────┐
        │  Tier 2 — Spinal Cord (Azure)     │                       │ Tier 3 — Nervous System (M365)    │
        │  Azure Functions + Azure OpenAI   │                       │ Power Platform / Copilot Studio   │
        │  azuredeploy.json · ./deploy.sh   │                       │ managed solution → Teams          │
        └──────────────────────────────────┘                       └──────────────────────────────────┘
```

Two front doors (the MCP servers), one shared agent contract, one sacred endpoint (`/chat`), and an
automated path up the three tiers — invoked only when the user chooses.

---

## 3. The MCP servers

Every server speaks MCP over **stdio** using JSON-RPC, protocol version `2024-11-05`. All are pure
Python standard library — no install, no dependencies. Logs go to **stderr**; the **stdout** stream
is reserved for the MCP protocol. Two serve agents/brainstem from the local machine (§3.1, §3.2); a
third **static profile** (§3.3) serves a catalog hosted entirely on `raw.githubusercontent.com`.

Public source: **github.com/kody-w/rapp-mcp**.

### 3.1 `rapp_mcp.py` — agents as tools

**Purpose.** Serve a folder of drop-in `*_agent.py` files as individual MCP tools. Lightweight,
stateless, no server to run. Each agent file becomes one tool whose name, description, and input
schema come straight from the agent's `metadata`.

**Invocation.**

```bash
python3 rapp_mcp.py /path/to/agents
```

If no path is given, the current working directory is used. The agents folder is scanned
**recursively**; `basic_agent.py` and any path segment named `experimental` or `disabled` is
skipped.

**Hotload.** Agents are re-scanned on **every** `tools/list` and `tools/call`. Drop a new
`*_agent.py` into the folder and it appears as a tool with no restart. *The bytes are the contract.*

**Tools exposed.** One MCP tool **per agent**. For an agent with `self.name = "hello"`:

| Field | Source |
|---|---|
| tool `name` | `agent.name` |
| tool `description` | `agent.metadata["description"]` |
| tool `inputSchema` | `agent.metadata["parameters"]` (a JSON-Schema object) |

A `tools/call` invokes the matching agent's `perform(**arguments)` and returns its result as MCP
text content:

```jsonc
// tools/call result (success)
{ "content": [ { "type": "text", "text": "<perform() return, JSON-encoded if not a string>" } ] }

// tools/call result (unknown tool or perform() raised)
{ "content": [ { "type": "text", "text": "<error message>" } ], "isError": true }
```

**Storage shim.** Agents written for the cloud often import
`from utils.azure_file_storage import AzureFileStorageManager`. `rapp_mcp.py` injects a local
stand-in via `sys.modules`, persisting JSON under `RAPP_MCP_DATA` (default `~/.rapp_mcp_data`). The
same agent file therefore runs unchanged locally and in Azure.

**Client config — `mcpServers` form** (Claude Desktop, Cursor, and most hosts):

```json
{
  "mcpServers": {
    "rapp-mcp": {
      "command": "python3",
      "args": ["/abs/path/rapp_mcp.py", "/abs/path/to/agents"]
    }
  }
}
```

**Client config — `servers` + `type: local` form** (hosts that use this variant):

```json
{
  "servers": {
    "rapp-mcp": {
      "type": "local",
      "command": "python3",
      "args": ["/abs/path/rapp_mcp.py", "/abs/path/to/agents"]
    }
  }
}
```

> Use **absolute paths** for both the script and the agents folder — MCP hosts launch the command
> from an unspecified working directory.

### 3.2 `rapp_brainstem_mcp.py` — the full brainstem, as a tool

**Purpose.** Expose a *whole running RAPP Brainstem* as MCP tools. Where `rapp_mcp.py` serves
individual agents, this bridges the Brainstem's LLM-orchestrated `/chat` endpoint — so any MCP host
can use the user's full on-device brain (its system prompt/soul, memory, and every agent dropped
in) without running the Brainstem itself. And if the Brainstem isn't installed or running, one tool
call sets it up.

**Invocation.**

```bash
python3 rapp_brainstem_mcp.py        # bridges http://localhost:7071 by default
```

**Configuration (env).**

| Variable | Default | Meaning |
|---|---|---|
| `RAPP_BRAINSTEM_URL` | `http://localhost:7071` | Base URL of the running Brainstem. |
| `BRAINSTEM_HOME` | `~/.brainstem` | Install location used by `brainstem_bootstrap`. |

**Tools exposed.**

#### `brainstem`
Routes a request through the full `/chat` loop (LLM + memory + agents).

- **Params**
  - `message` *(string, required)* — what to ask or tell the Brainstem, in plain English.
  - `new_session` *(boolean, optional, default `false`)* — `true` starts a fresh conversation;
    `false` continues the bridge's current session for memory continuity.
- **Returns** — MCP text content containing the Brainstem's `response`. The server tracks the
  returned `session_id` internally, so consecutive `brainstem` calls share one conversation unless
  `new_session: true` is set.
- **Degraded paths** — if the Brainstem is unreachable, the tool replies with guidance to run
  `brainstem_bootstrap`. If the Brainstem reports an auth error, it instructs the user to sign in at
  its `/login` (GitHub device-code flow) and retry. No secrets ever cross the MCP boundary.

#### `brainstem_status`
Reports whether the local Brainstem is up and authenticated.

- **Params** — none.
- **Returns** — MCP text content summarizing reachability, `status`, loaded agent count, and the
  active `model` (from the Brainstem's `/health`), or a not-running message pointing at
  `brainstem_bootstrap`.

#### `brainstem_bootstrap`
Installs (if needed) and starts the local Brainstem, then waits until it reports healthy.

- **Params** — none.
- **Returns** — MCP text content reporting the outcome (already running / started / installing) and
  next step. If the Brainstem is already installed it runs `start.sh`; otherwise it runs the public
  install one-liner. It polls `/health` for up to ~2 minutes and tells the user to sign in at
  `/login` if the Brainstem comes up unauthenticated.
- **Public install one-liner**:
  ```bash
  curl -fsSL https://microsoft.github.io/aibast-agents-library/install.sh | bash
  ```

**Recommended first-run sequence for an AI:** call `brainstem_status`; if not running, call
`brainstem_bootstrap`; then use `brainstem`.

**Client config — `mcpServers` form:**

```json
{
  "mcpServers": {
    "rapp-brainstem": {
      "command": "python3",
      "args": ["/abs/path/rapp_brainstem_mcp.py"]
    }
  }
}
```

**Client config — `servers` + `type: local` form:**

```json
{
  "servers": {
    "rapp-brainstem": {
      "type": "local",
      "command": "python3",
      "args": ["/abs/path/rapp_brainstem_mcp.py"]
    }
  }
}
```

You may register **both** servers at once — agents as fine-grained tools *and* the Brainstem as the
do-everything tool — by placing both entries under the same `mcpServers` (or `servers`) object.

---

### 3.3 The static profile — a catalog on `raw.githubusercontent.com`

`rapp-static-mcp/1.0` is the **static profile** of rapp-mcp, built on the
[`rapp-static-api/1.0`](https://github.com/kody-w/rapp-static-apis) pattern. MCP's `tools/call`
needs *compute*, but rapp-mcp's thesis is **"bytes are the contract"** — so the **catalog and the
agent bytes are 100 % static and global**, and the only thing that runs is a tiny universal client.
It is the direct generalization of `rapp_mcp.py` from a *local agents dir* to a *manifest of URLs*.

Two files:

- **`build_static_mcp.py`** — the **build step** (run once, in CI or by hand). A hand-edited
  `manifest.json` → a generated, CDN-servable tree:

  | Path | What it is |
  |------|-----------|
  | `api/v1/tools.json` | the **pre-baked MCP `tools/list` payload** — served with zero compute. Each tool carries a leading-underscore `_frame` x-extension (`url`, `sha256`, `sha8`, `class`, `bytes`, `pinned`) that MCP hosts ignore and the static client reads. |
  | `agents/<tool>/<sha8>.py` | **append-only, content-addressed agent frames.** `sha8` = first 12 hex of `sha256(raw bytes)`. A frame is **never mutated or deleted** — every version is an immutable, pinnable fallback. |
  | `registry.json` | the full index — every version of every tool, each pinnable forever. |
  | `api/v1/status.json`, `api/v1/badge.json` | a drift verdict shaped so **rapp-god** can observe this repo as just another part. |

  The build is **idempotent** (same input bytes → byte-identical output; `--check` fails CI if stale)
  and **deterministic** (the same agent bytes always hash to the same `sha8` on any host).

- **`rapp_static_mcp.py`** — the **universal client**, a pure-stdlib stdio MCP server whose wire shape
  is byte-identical to `rapp_mcp.py` (so any host already wired for rapp-mcp works unchanged):

  ```json
  { "mcpServers": { "rapp-static-mcp": {
    "command": "python3",
    "args": ["/abs/path/rapp_static_mcp.py",
             "https://raw.githubusercontent.com/<owner>/<repo>/main/api/v1/tools.json"] } } }
  ```

  It accepts a `tools.json` URL, a base URL, a `manifest.json`, or a **local path**.
  `initialize`/`tools/list` serve the pre-baked catalog (cached locally for offline use, `_frame`
  stripped from what the host sees). On **`tools/call`** it fetches the tool's pinned frame,
  **verifies `sha256` before executing it** (a frame whose bytes don't match the baked hash —
  *or that carries no valid 64-hex `sha256` at all* — is **never run**: the integrity gate fails
  closed), execs locally with the same `BasicAgent` + `utils.azure_file_storage` shims as
  `rapp_mcp.py`, and returns the identical `{ "content": [ { "type": "text", "text": … } ] }` shape.

  Environment:

  | Var | Effect |
  |-----|--------|
  | `RAPP_STATIC_MCP_URL` | overrides `argv[1]` (the catalog source) |
  | `RAPP_STATIC_MCP_CACHE` | content-addressed cache dir (default `~/.rapp_static_mcp`) — pinned, already-fetched frames serve fully offline from here |
  | `RAPP_STATIC_MCP_DATA` | agent local-storage dir for `utils.azure_file_storage` (default `~/.rapp_static_mcp_data`) |

- **`manifest.json`** (the only hand-edited file) declares the catalog:

  ```json
  {
    "schema": "rapp-static-mcp-manifest/1.0",
    "name": "rapp-static-mcp-examples",
    "server": { "name": "rapp-static-mcp", "version": "1.0.0", "protocol": "2024-11-05" },
    "self": "https://raw.githubusercontent.com/<owner>/<repo>/main/<base>",
    "agents": [
      { "group": "examples", "kind": "observe", "pin": null,
        "grail": { "label": "rapp-mcp", "url": "https://raw.githubusercontent.com/.../hello_agent.py" } }
    ]
  }
  ```

  Optional per-agent keys: `tool` (override the MCP tool name), `note`, `pin` (a `sha8` to **freeze**
  the tool to forever — immune to any future change on `main`).

**Why it matters:** the catalog and code are free, CDN-cached, CORS-open, forkable, and durable.
Pinning a `sha8` is an eternal, host-independent fallback that keeps working even if `main` breaks,
the source repo vanishes, or a bad update ships — the frame is already on GitHub's CDN, and the
client refuses to run anything whose hash doesn't match the pin. A worked example ships under
`examples/static/` (built from `examples/manifest.json`); its frame `url` is the canonical raw
path, so a live `tools/call` against the committed example resolves once that path is published.

---

## 4. The agent contract (`*_agent.py`)

An agent is a **single Python file** named `*_agent.py`. It defines a class that extends
`BasicAgent` and sets three things:

- `self.name` — the tool name (stable, machine-readable).
- `self.metadata` — a JSON-Schema **function definition** describing the tool.
- `perform(**kwargs) -> str` — the implementation; returns a string (other types are JSON-encoded).

**`self.metadata` shape** (an OpenAI-style function-calling schema):

```jsonc
{
  "name": "<same as self.name>",
  "description": "<what it does + when to call it — this is the only thing the calling AI sees>",
  "parameters": {                       // a JSON-Schema object → becomes the tool's inputSchema
    "type": "object",
    "properties": {
      "<arg>": { "type": "string", "description": "<self-sufficient description>" }
    },
    "required": ["<arg>"]
  }
}
```

> **Every parameter must be fully self-describing.** The calling AI only ever sees `description`
> and the parameter schema — never the source. Make the description say *when* to call the tool, and
> give each parameter a description that stands on its own. Do not hardcode values that should be
> caller-supplied.

**Drop-in + hotload.** Place the file in the agents folder; it is picked up on the next call — no
restart, no registration step. Removing the file removes the tool.

**Bytes are the contract / deterministic across machines.** The agent's behavior is wholly
contained in its source. There is no per-machine config, no implicit registry, no build artifact.
Copy the file to another machine and the tool is byte-for-byte the same. This is what makes the
agent portable across Tier 1 (local), Tier 2 (Azure), and Tier 3 (M365) without modification.

**`BasicAgent` base.** Both MCP servers and the Brainstem provide a compatible `BasicAgent`. The
canonical base offers:

- `__init__(self, name=None, metadata=None)` — sets `self.name` / `self.metadata` if not already
  set by the subclass.
- `perform(self, **kwargs)` — override; return a string.
- `system_context(self) -> str | None` — *optional*; when non-empty, the Brainstem injects the
  returned text into the system prompt every turn (used by memory-style agents). `rapp_mcp.py` does
  not call this — it is a Brainstem-only enrichment.
- `to_tool(self)` — used by the Brainstem to build the OpenAI tool definition from `metadata`.

Import the base portably so the file runs in every context:

```python
try:
    from agents.basic_agent import BasicAgent   # inside a Brainstem package
except ImportError:
    from basic_agent import BasicAgent          # standalone / shimmed by rapp-mcp
```

**Minimal example agent:**

```python
"""hello_agent.py — drop any *_agent.py like this into your agents folder."""
try:
    from agents.basic_agent import BasicAgent
except ImportError:
    from basic_agent import BasicAgent


class HelloAgent(BasicAgent):
    def __init__(self):
        self.name = "hello"
        self.metadata = {
            "name": self.name,
            "description": "Say hello to someone. Call when the user wants a greeting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Who to greet."}
                },
                "required": ["name"],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        return f"Hello, {kwargs.get('name', 'world')}!"
```

Saved as `hello_agent.py` in the agents folder, this immediately appears as the MCP tool `hello`
with a required `name` argument — usable from any MCP host.

---

## 5. The `/chat` contract — the single sacred endpoint

Everything the Brainstem does flows through **one** endpoint. There are no per-capability REST
routes; new capabilities are added as agents, not as new endpoints. `rapp_brainstem_mcp.py`'s
`brainstem` tool is a thin bridge over this contract.

**Request — `POST /chat`** (`Content-Type: application/json`):

```jsonc
{
  "user_input": "string, required — the user's message",
  "conversation_history": [               // optional; prior turns
    { "role": "user",      "content": "…" },
    { "role": "assistant", "content": "…" },
    { "role": "tool",      "content": "…" }
  ],
  "session_id": "optional string — omit to start a new session"
}
```

**Response — `200 OK`:**

```jsonc
{
  "response": "string — the assistant's reply",
  "session_id": "string — echo this back on the next call to continue the session",
  "agent_logs": "string — newline-joined log of any agents the LLM invoked",
  "voice_mode": false
}
```

**Errors:** `400` if `user_input` is missing; `502` on upstream model errors (with `model` and
`detail`); `500` on unexpected failure — each as `{ "error": "…", … }`.

**Session continuity.** Pass the returned `session_id` back on the next request to continue the same
conversation; omit it (or send `new_session` through the MCP bridge) to start fresh. The MCP
`brainstem` tool manages this for the calling AI automatically.

**Internal flow (informative).** Each `/chat` request: loads the system prompt (`soul.md`) and any
agent `system_context()`, fresh-discovers the agents, calls the LLM with the agents as tools, runs
any tool calls and loops (up to 3 rounds), and returns the final reply plus `agent_logs`. Agents are
re-discovered every request, so the on-ramp and the Brainstem share the same hotload semantics.

**Why it is sacred.** One endpoint means one place to reason about auth, memory, and orchestration —
and it means any MCP host that can reach `/chat` has the *entire* RAPP capability surface, present
and future, through a single tool.

---

## 6. The three tiers + automated promotion

RAPP is a ladder. Each rung is self-contained and runs the **same agent contract** from §4. The
user advances only when they choose to.

| Tier | Name | Runtime | Provisioned by |
|---|---|---|---|
| **1** | **Brainstem** (local) | Flask server on `:7071`, `POST /chat`, GitHub-Copilot-backed LLM | install one-liner |
| **2** | **Spinal Cord** (Azure) | Azure Functions + Azure OpenAI, same agent contract | ARM template `azuredeploy.json` via `./deploy.sh` |
| **3** | **Nervous System** (M365) | Power Platform / Copilot Studio managed solution, surfaced in Teams | solution `.zip` imported into a Dataverse environment |

**Promotion is itself an agent.** Because every capability is an agent reachable through `/chat`,
*deployment is a capability too*. The Brainstem can ship promotion agents that drive the
higher-tier provisioning, and because `rapp_brainstem_mcp.py` exposes `/chat`, **any MCP host can
ask the Brainstem to promote — when the user is ready.**

- **Promote to Tier 2 (Azure).** A deploy agent runs the Azure ARM deployment:
  ```bash
  ./deploy.sh [resource-group] [location] [openai-location]
  ```
  using the public template
  `https://raw.githubusercontent.com/microsoft/aibast-agents-library/main/azuredeploy.json`. It
  uses the **Azure CLI** under the user's own Azure login; the agents that already run locally run
  unchanged on Azure Functions + Azure OpenAI.

- **Promote to Tier 3 (Microsoft 365).** A solution-import agent imports the Power Platform /
  Copilot Studio managed solution `.zip` into the user's chosen Dataverse environment (via the
  **Power Platform CLI**), surfacing the agents in Teams.

These promotion agents are **local Brainstem files** and may name the Azure / Power Platform CLIs
directly. They must **never** embed tenant ids, client/app ids, subscription ids, or any personal
credentials — provisioning runs under the user's own interactive CLI login, on the user's machine,
when the user initiates it.

The net effect: an AI joins at Tier 1 through MCP, gets the full on-device experience, and — on the
user's word — has the Brainstem carry that same agent set up to Azure and Microsoft 365 with no
change to the agents themselves.

---

## 7. Security & trust model

- **Local-first execution.** Both MCP servers and the Brainstem run on the user's own device. Agent
  code executes locally; `/chat` is a `localhost` service by default
  (`http://localhost:7071`). Nothing in this layer phones home.
- **The user's own credentials.** The Brainstem authenticates the LLM through the user's GitHub
  Copilot account (device-code flow at `/login`). Tier-2/Tier-3 promotion runs under the user's own
  interactive Azure CLI / Power Platform CLI logins. The MCP servers never see, store, or transmit
  those secrets — at most they tell the user to go sign in and retry.
- **No secrets in public artifacts.** This spec, the website, and the published MCP servers contain
  no tenant ids, client/app ids, subscription ids, API keys, tokens, or personal information. Local
  state (e.g. `~/.rapp_mcp_data`, `~/.brainstem`) stays on disk and out of the repo.
- **MCP host permissions.** The calling AI can only invoke the tools the host has registered and the
  user has approved. `rapp_mcp.py` exposes exactly the agents in the pointed-at folder — no more.
  `rapp_brainstem_mcp.py` exposes exactly three tools (`brainstem`, `brainstem_status`,
  `brainstem_bootstrap`); the user controls whether the bridge is registered at all.
- **Arbitrary-code awareness.** A `*_agent.py` is Python and runs with the user's privileges. Treat
  agent files like any other code you add to your machine: only load agents you trust, from folders
  you control. The hotload model means *what is in the folder is what runs* — the folder is the
  trust boundary.
- **Deterministic surface.** Because the bytes are the contract, a reviewer can read an agent file
  and know exactly what the tool does on every machine. There is no hidden server-side behavior to
  audit separately.

---

## 8. Versioning

This document is **`rapp-mcp-spec/1.0`**.

- **Additive, never breaking.** Within major version `1.x`, changes only *add* — new optional tool
  params, new optional response fields, new tiers/promotion agents, new client-config variants.
  Existing tool names, required params, and response fields do not change meaning or disappear.
- **Compatibility contract.** A client written against `1.0` keeps working against any `1.x`. Read
  permissively (ignore unknown fields); write conservatively (emit only what is specified).
- **Wire protocol.** The MCP servers implement MCP protocol `2024-11-05` over stdio. Protocol-level
  upgrades, if any, will be negotiated through MCP's own `initialize` handshake and noted here.
- **Breaking changes** — should they ever be unavoidable — would ship as a new major
  (`rapp-mcp-spec/2.0`) alongside, never silently in place.

---

*RAPP is an engine, not an experience. `rapp-mcp` is the doorway any AI walks through to use it.*
