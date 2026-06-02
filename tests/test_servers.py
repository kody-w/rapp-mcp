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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = sys.executable

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
    print("OK — rapp_mcp serves + calls the example agent; rapp_brainstem serves its 3 tools.")


if __name__ == "__main__":
    main()
