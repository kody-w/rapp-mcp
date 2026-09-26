#!/usr/bin/env python3
"""Functional tests for the rapp-mcp servers. Zero external deps — run directly:

    python3 tests/test_servers.py

Spawns each MCP server over stdio, does the protocol handshake, and checks that the
expected tools are served and a tool call works.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = sys.executable

PROBE_AGENT = '''try:
    from agents.basic_agent import BasicAgent
except ImportError:
    from basic_agent import BasicAgent


class ProbeAgent(BasicAgent):
    def __init__(self):
        self.name = {name!r}
        self.metadata = {{"name": self.name, "description": "Discovery probe.",
                         "parameters": {{"type": "object", "properties": {{}}}}}}
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        return self.name
'''

HANDSHAKE = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "tests", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]


def mcp(cmd, requests, timeout=30):
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    want = {r["id"] for r in requests if "id" in r}
    out = {}
    try:
        for r in requests:
            proc.stdin.write(json.dumps(r) + "\n")
        proc.stdin.flush(); proc.stdin.close()
        t0 = time.time()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("id") in want:
                out[m["id"]] = m.get("result", m.get("error"))
                if want.issubset(out):
                    break
            if time.time() - t0 > timeout:
                break
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    return out


def check_top_level_only(fails):
    """rapp_mcp.py serves only top-level *_agent.py files; every subfolder is parked.

    RAPP proposal 0001 (kody-w/RAPP#124): a nested experimental_agents/x_agent.py is NOT
    served, and a top-level experimental_thing_agent.py IS served.
    """
    layout = {
        "top_hello_agent.py": "top_hello",
        "experimental_thing_agent.py": "experimental_thing",
        "disabled_thing_agent.py": "disabled_thing",
        os.path.join("experimental_agents", "x_agent.py"): "nested_experimental_x",
        os.path.join("disabled_agents", "y_agent.py"): "nested_disabled_y",
        os.path.join("parked", "z_agent.py"): "nested_parked_z",
    }
    with tempfile.TemporaryDirectory() as root:
        agents_dir = os.path.join(root, "agents")
        for rel, name in layout.items():
            path = os.path.join(agents_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(PROBE_AGENT.format(name=name))
        saved = os.environ.get("RAPP_MCP_DATA")
        os.environ["RAPP_MCP_DATA"] = os.path.join(root, "data")
        try:
            r = mcp([PY, os.path.join(REPO, "rapp_mcp.py"), agents_dir],
                    HANDSHAKE + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
        finally:
            if saved is None:
                os.environ.pop("RAPP_MCP_DATA", None)
            else:
                os.environ["RAPP_MCP_DATA"] = saved
    tools = {t.get("name") for t in (r.get(2) or {}).get("tools", [])}
    expected = {"top_hello", "experimental_thing", "disabled_thing"}
    if tools != expected:
        fails.append(f"rapp_mcp: top-level-only discovery expected {sorted(expected)}, got {sorted(tools)}")
    version = ((r.get(1) or {}).get("serverInfo") or {}).get("version")
    if version != "2.0.0":
        fails.append(f"rapp_mcp: expected serverInfo.version 2.0.0 (rapp-mcp-spec/2.0), got {version!r}")

def main():
    fails = []

    # rapp_mcp.py serves the example agent and calls it
    r = mcp([PY, os.path.join(REPO, "rapp_mcp.py"), os.path.join(REPO, "examples")],
            HANDSHAKE + [
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "hello", "arguments": {"name": "world"}}}])
    tools = {t.get("name") for t in (r.get(2) or {}).get("tools", [])}
    if "hello" not in tools:
        fails.append(f"rapp_mcp: expected 'hello' tool, got {sorted(tools)}")
    text = ((r.get(3) or {}).get("content") or [{}])[0].get("text", "")
    if "Hello, world" not in text:
        fails.append(f"rapp_mcp: tools/call returned {text!r}")

    # rapp_mcp.py serves only top-level agents (a nested folder is parked)
    check_top_level_only(fails)

    # rapp_brainstem_mcp.py serves its three tools
    r = mcp([PY, os.path.join(REPO, "rapp_brainstem_mcp.py")],
            HANDSHAKE + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
    bt = {t.get("name") for t in (r.get(2) or {}).get("tools", [])}
    expected = {"brainstem", "brainstem_status", "brainstem_bootstrap"}
    if not expected.issubset(bt):
        fails.append(f"rapp_brainstem: expected {sorted(expected)}, got {sorted(bt)}")

    if fails:
        print("FAILED:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("OK — rapp_mcp serves + calls the example agent and serves only top-level agents; "
          "rapp_brainstem serves its 3 tools.")


if __name__ == "__main__":
    main()
