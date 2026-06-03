#!/usr/bin/env python3
"""rapp-mcp — serve drop-in `*_agent.py` files as MCP tools, in any MCP host.

A single, dependency-free MCP (Model Context Protocol) server that exposes a folder of
`agent.py` files to ANY MCP client (Claude Desktop, GitHub Copilot CLI, Cursor, and
anything else that speaks MCP).

Point it at a folder of `*_agent.py` files. Each agent becomes an MCP tool. Drop a new
`*_agent.py` into the folder and it hotloads — agents are re-scanned on every tools/list
and tools/call, so there's nothing to restart. The bytes are the contract: the same file
behaves identically on every machine.

USAGE
    python3 rapp_mcp.py /path/to/agents

MCP client config, e.g.:
    {
      "mcpServers": {
        "rapp-mcp": {
          "command": "python3",
          "args": ["/abs/path/rapp_mcp.py", "/abs/path/to/agents"]
        }
      }
    }

Agents are single-file `*_agent.py` defining a class that extends BasicAgent with
`self.name`, `self.metadata` (a JSON-Schema function definition), and `perform(**kwargs)`.
"""
import glob
import importlib.util
import json
import os
import sys
import time
import traceback
import types

SERVER_NAME = "rapp-mcp"
SERVER_VERSION = "1.0.0"  # tracks the stable rapp-mcp-spec/1.0
PROTOCOL = "2024-11-05"

AGENTS_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()
DATA_DIR = os.environ.get("RAPP_MCP_DATA", os.path.join(os.path.expanduser("~"), ".rapp_mcp_data"))


def log(msg):
    # MCP uses stdout for protocol; logs go to stderr.
    sys.stderr.write(f"[rapp-mcp] {msg}\n")
    sys.stderr.flush()


# ── Shims so brainstem agents load standalone (basic_agent + local storage) ───────
class BasicAgent:
    def __init__(self, name=None, metadata=None):
        if name and not getattr(self, "name", None):
            self.name = name
        if metadata and not getattr(self, "metadata", None):
            self.metadata = metadata

    def perform(self, **kwargs):
        raise NotImplementedError


class _LocalStorage:
    """Minimal stand-in for utils.azure_file_storage.AzureFileStorageManager."""
    def __init__(self, *a, **k):
        os.makedirs(DATA_DIR, exist_ok=True)
        self._f = os.path.join(DATA_DIR, "store.json")

    def _read(self):
        try:
            return json.load(open(self._f))
        except Exception:
            return {}

    def read_json(self, *a, **k):
        return self._read()

    def write_json(self, data, *a, **k):
        json.dump(data, open(self._f, "w"))

    def __getattr__(self, _):  # tolerate other calls
        return lambda *a, **k: None


def _register_shims():
    if "agents" not in sys.modules:
        m = types.ModuleType("agents"); m.__path__ = [AGENTS_DIR]; sys.modules["agents"] = m
    ba = types.ModuleType("agents.basic_agent"); ba.BasicAgent = BasicAgent
    sys.modules["agents.basic_agent"] = ba
    sys.modules["agents"].basic_agent = ba
    baf = types.ModuleType("basic_agent"); baf.BasicAgent = BasicAgent
    sys.modules["basic_agent"] = baf
    if "utils" not in sys.modules:
        u = types.ModuleType("utils"); u.__path__ = []; sys.modules["utils"] = u
    afs = types.ModuleType("utils.azure_file_storage"); afs.AzureFileStorageManager = _LocalStorage
    sys.modules["utils.azure_file_storage"] = afs
    sys.modules["utils"].azure_file_storage = afs


# ── Loader (hotload: re-scanned each request) ─────────────────────────────────────
def load_agents():
    _register_shims()
    if AGENTS_DIR not in sys.path:
        sys.path.insert(0, AGENTS_DIR)
    agents = {}
    for fp in sorted(glob.glob(os.path.join(AGENTS_DIR, "**", "*_agent.py"), recursive=True)):
        base = os.path.basename(fp)
        if base in ("basic_agent.py",) or "/experimental" in fp or "/disabled" in fp:
            continue
        try:
            modname = "agentpy_" + base[:-3] + "_" + str(abs(hash(fp)))
            spec = importlib.util.spec_from_file_location(modname, fp)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for attr in dir(mod):
                cls = getattr(mod, attr)
                if (isinstance(cls, type) and hasattr(cls, "perform")
                        and attr not in ("BasicAgent", "object") and not attr.startswith("_")):
                    try:
                        inst = cls()
                        if getattr(inst, "name", None) and getattr(inst, "metadata", None):
                            agents[inst.name] = inst
                    except Exception:
                        pass
        except Exception as e:
            log(f"failed to load {base}: {e}")
    return agents


def tool_defs(agents):
    out = []
    for name, inst in agents.items():
        md = inst.metadata or {}
        out.append({
            "name": name,
            "description": md.get("description", name),
            "inputSchema": md.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


# ── MCP stdio JSON-RPC loop ───────────────────────────────────────────────────────
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        agents = load_agents()
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_defs(agents)}}
    if method == "tools/call":
        params = req.get("params", {}) or {}
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        agents = load_agents()
        inst = agents.get(name)
        if not inst:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"Unknown agent tool '{name}'."}], "isError": True}}
        try:
            result = inst.perform(**args)
            text = result if isinstance(result, str) else json.dumps(result)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:
            tb = traceback.format_exc().splitlines()[-3:]
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"{name} error: {e}\n" + "\n".join(tb)}], "isError": True}}
    if method and method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    log(f"serving *_agent.py from {AGENTS_DIR}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = handle(req)
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()
