# rapp-mcp

**[📖 Docs & live site →](https://kody-w.github.io/rapp-mcp)**  ·  **[Spec → `SPEC.md`](SPEC.md)**

Bring **RAPP** onto any MCP host (Claude Desktop, GitHub Copilot CLI, Cursor, …).
Three small, dependency-free **MCP (Model Context Protocol)** servers — two serve the local
machine, the third serves a static catalog straight off `raw.githubusercontent.com`:

| Server | What it gives you |
|---|---|
| **`rapp_mcp.py`** | Serves a folder of drop-in `*_agent.py` files as individual MCP tools. Lightweight, stateless, no server to run. |
| **`rapp_brainstem_mcp.py`** | Exposes a *whole running RAPP brainstem* as one tool — its LLM-orchestrated `/chat` (memory, agents, on-device context). Can install + start the brainstem for you. |
| **`rapp_static_mcp.py`** | Serves a static, CDN-hosted catalog (`rapp-static-mcp/1.0`) — `rapp_mcp.py` generalized from a local folder to a manifest of URLs, verifying each frame's `sha256` before exec (§3). |

All are pure Python standard library. Pick one or run any combination.

---

## 1. `rapp_mcp.py` — agents as tools

```bash
python3 rapp_mcp.py /path/to/agents
```
Each `*_agent.py` in the folder becomes an MCP tool. Drop a new one in and it **hotloads**
— re-scanned on every call, nothing to restart. The bytes are the contract: identical on
every machine.

```json
{ "mcpServers": { "rapp-mcp": {
  "command": "python3", "args": ["/abs/path/rapp_mcp.py", "/abs/path/to/agents"] } } }
```

### Agent format
A single-file `*_agent.py` defining a class that extends `BasicAgent`:
```python
from basic_agent import BasicAgent  # shimmed by rapp-mcp at load time

class HelloAgent(BasicAgent):
    def __init__(self):
        self.name = "hello"
        self.metadata = {
            "name": "hello", "description": "Say hello.",
            "parameters": {"type": "object",
                "properties": {"name": {"type": "string"}}, "required": ["name"]},
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        return f"Hello, {kwargs.get('name', 'world')}!"
```
See [`examples/`](examples/).

---

## 2. `rapp_brainstem_mcp.py` — the full brainstem, as a tool

```bash
python3 rapp_brainstem_mcp.py        # bridges http://localhost:7071 by default
```
```json
{ "mcpServers": { "rapp-brainstem": {
  "command": "python3", "args": ["/abs/path/rapp_brainstem_mcp.py"] } } }
```

Tools:
- **`brainstem`** — send a request to your locally running brainstem; it routes through
  every local agent with full on-device context and returns the answer.
- **`brainstem_status`** — is it up and authenticated?
- **`brainstem_bootstrap`** — install (if needed) and start the brainstem, then wait
  until healthy. On-device setup in one tool call.

Env: `RAPP_BRAINSTEM_URL` (default `http://localhost:7071`).

## 3. The static profile — a catalog on `raw.githubusercontent.com`

MCP's `tools/call` needs compute, but rapp-mcp's thesis is **"bytes are the contract"** — so the
**catalog and the agent bytes can be 100 % static**, served from GitHub raw with no server, and the
only thing that runs is a tiny universal client. This is `rapp_mcp.py` generalized from a local
folder to a manifest of URLs (`rapp-static-mcp/1.0`, built on
[`rapp-static-api/1.0`](https://github.com/kody-w/rapp-static-apis)).

```bash
# Build a static catalog from a hand-edited manifest (run once, in CI or by hand)
python3 build_static_mcp.py examples/manifest.json --out examples/static \
  --self https://raw.githubusercontent.com/kody-w/rapp-mcp/main/examples/static
#  -> api/v1/tools.json  (the pre-baked tools/list)
#  -> agents/<tool>/<sha8>.py  (append-only, content-addressed, pinnable frames)
#  -> registry.json + api/v1/status.json + api/v1/badge.json

# Serve it in any MCP host — fetches tools.json + verifies each frame's sha256 before exec
python3 rapp_static_mcp.py https://raw.githubusercontent.com/kody-w/rapp-mcp/main/examples/static/api/v1/tools.json
```

```json
{ "mcpServers": { "rapp-static-mcp": {
  "command": "python3",
  "args": ["/abs/path/rapp_static_mcp.py",
           "https://raw.githubusercontent.com/kody-w/rapp-mcp/main/examples/static/api/v1/tools.json"] } } }
```

The catalog is free, CDN-cached, CORS-open, forkable, and durable: **pin a `sha8` and that exact
agent runs forever**, even if `main` breaks or the source vanishes — and the client **refuses to run
any frame whose hash doesn't match the pin**. A worked example ships under `examples/static/`.
See [`SPEC.md` §3.3](SPEC.md).

## License
MIT
