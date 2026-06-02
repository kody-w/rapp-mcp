# rapp-mcp

A tiny, dependency-free **MCP (Model Context Protocol)** server that serves a folder of
drop-in `*_agent.py` files as MCP tools — usable from any MCP client (Claude Desktop,
GitHub Copilot CLI, Cursor, …).

- **Drop-in + hotload** — add a `*_agent.py` to the folder and it appears as a tool. No restart.
- **Deterministic + portable** — the agent's bytes are the contract; identical on every machine.
- **Zero dependencies** — pure Python standard library.

## Run
```bash
python3 rapp_mcp.py /path/to/agents
```

## Register in an MCP client
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
Some hosts use the key `"servers"` instead of `"mcpServers"`, with `"type": "local"`.

## Agent format
Each tool is a single-file `*_agent.py` defining a class that extends `BasicAgent`:

```python
from basic_agent import BasicAgent  # shimmed by rapp-mcp at load time

class HelloAgent(BasicAgent):
    def __init__(self):
        self.name = "hello"
        self.metadata = {
            "name": "hello",
            "description": "Say hello to someone.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
                "required": ["name"],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        return f"Hello, {kwargs.get('name', 'world')}!"
```

`rapp-mcp` ships a `BasicAgent` shim so agents load standalone — no framework required.
See [`examples/`](examples/).

## License
MIT
