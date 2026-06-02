# rapp-mcp

**[📖 Docs & live site →](https://kody-w.github.io/rapp-mcp)**  ·  **[Spec → `SPEC.md`](SPEC.md)**

Bring **RAPP** onto any MCP host (Claude Desktop, GitHub Copilot CLI, Cursor, …).
Two small, dependency-free **MCP (Model Context Protocol)** servers:

| Server | What it gives you |
|---|---|
| **`rapp_mcp.py`** | Serves a folder of drop-in `*_agent.py` files as individual MCP tools. Lightweight, stateless, no server to run. |
| **`rapp_brainstem_mcp.py`** | Exposes a *whole running RAPP brainstem* as one tool — its LLM-orchestrated `/chat` (memory, agents, on-device context). Can install + start the brainstem for you. |

Both are pure Python standard library. Pick one or run both.

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

## License
MIT
