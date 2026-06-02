#!/usr/bin/env python3
"""rapp-brainstem-mcp — talk to a locally running RAPP brainstem from any MCP host.

Where `rapp_mcp.py` serves individual `*_agent.py` files as tools, this exposes the
WHOLE on-device brainstem as a single MCP tool: its LLM-orchestrated `/chat` endpoint,
which routes your request through every local agent (memory, digital twin, and whatever
else you've dropped in) with full on-device context.

So any MCP client (Claude Desktop, GitHub Copilot CLI, Cursor, …) can use your full
on-device brainstem without running it itself — and if the brainstem isn't installed or
running, the `brainstem_bootstrap` tool installs + starts it. Setup becomes one tool call.

USAGE
    python3 rapp_brainstem_mcp.py

MCP client config, e.g.:
    {
      "mcpServers": {
        "rapp-brainstem": {
          "command": "python3",
          "args": ["/abs/path/rapp_brainstem_mcp.py"]
        }
      }
    }

Env: RAPP_BRAINSTEM_URL (default http://localhost:7071).
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

SERVER_NAME = "rapp-brainstem"
SERVER_VERSION = "0.1.0"
PROTOCOL = "2024-11-05"

BRAINSTEM_URL = os.environ.get("RAPP_BRAINSTEM_URL", "http://localhost:7071").rstrip("/")
BRAINSTEM_HOME = os.path.expanduser(os.environ.get("BRAINSTEM_HOME", "~/.brainstem"))
INSTALL_ONELINER = "curl -sSfL https://raw.githubusercontent.com/kody-w/rapp-installer/main/install.sh | bash"

_session = {"id": None}


def log(m):
    sys.stderr.write(f"[rapp-brainstem] {m}\n"); sys.stderr.flush()


def _health(timeout=3):
    try:
        with urllib.request.urlopen(BRAINSTEM_URL + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _chat(message, new_session=False):
    if not message:
        return "Provide a message to send to the brainstem."
    if not _health():
        return ("The RAPP brainstem isn't reachable at " + BRAINSTEM_URL +
                ". Call the 'brainstem_bootstrap' tool to install + start it, then retry.")
    body = {"user_input": message}
    if not new_session and _session["id"]:
        body["session_id"] = _session["id"]
    data = json.dumps(body).encode()
    req = urllib.request.Request(BRAINSTEM_URL + "/chat", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        if e.code == 500 and "auth" in detail.lower():
            return "Brainstem needs to authenticate. Open its /login (GitHub device code) and retry."
        return f"Brainstem error HTTP {e.code}: {detail}"
    except Exception as e:
        return f"Could not reach the brainstem: {e}"
    _session["id"] = d.get("session_id") or _session["id"]
    return d.get("response") or d.get("error") or "(no response)"


def _bootstrap():
    if _health():
        h = _health()
        return f"Brainstem already running at {BRAINSTEM_URL} (status: {h.get('status')})."
    start_sh = os.path.join(BRAINSTEM_HOME, "src", "rapp_brainstem", "start.sh")
    if os.path.exists(start_sh):
        log("starting installed brainstem via start.sh")
        try:
            subprocess.Popen(["bash", start_sh], cwd=os.path.dirname(start_sh),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            return f"Failed to start brainstem: {e}"
        action = "started"
    else:
        log("brainstem not installed — running installer one-liner")
        try:
            subprocess.Popen(["bash", "-lc", INSTALL_ONELINER],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            return f"Failed to launch installer: {e}\nRun it yourself:\n  {INSTALL_ONELINER}"
        action = "installing + starting"
    for _ in range(60):                      # wait up to ~2 min for health
        time.sleep(2)
        h = _health(timeout=2)
        if h:
            return (f"Brainstem {action} — now up at {BRAINSTEM_URL} (status: {h.get('status')}). "
                    + ("Sign in at /login if it reports unauthenticated, then use the 'brainstem' tool."
                       if h.get("status") != "ok" else "Ready — use the 'brainstem' tool."))
    return (f"Brainstem {action} but didn't report healthy within 2 min. "
            "Check its logs, or run the installer manually:\n  " + INSTALL_ONELINER)


TOOLS = [
    {"name": "brainstem",
     "description": ("Send a request to your locally running RAPP brainstem — your on-device AI that "
                     "routes through your personal agents (memory, digital twin, and anything you've added) "
                     "with full local, private context. Use it for anything that benefits from on-device "
                     "agents and memory. If it isn't running, call 'brainstem_bootstrap' first."),
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string", "description": "What to ask or tell your brainstem (plain English)."},
         "new_session": {"type": "boolean", "description": "Start a fresh conversation (default false — continues the session for memory continuity)."}},
         "required": ["message"]}},
    {"name": "brainstem_status",
     "description": "Check whether the local RAPP brainstem is running and authenticated.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "brainstem_bootstrap",
     "description": ("Install (if needed) and start the local RAPP brainstem, then wait until it's healthy. "
                     "Run this once when brainstem_status shows it isn't running — it makes on-device setup a "
                     "single tool call."),
     "inputSchema": {"type": "object", "properties": {}}},
]


def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()


def handle(req):
    mid, method = req.get("id"), req.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = req.get("params", {}) or {}
        name, args = p.get("name"), (p.get("arguments") or {})
        if name == "brainstem":
            text = _chat(args.get("message", ""), bool(args.get("new_session")))
        elif name == "brainstem_status":
            h = _health()
            text = (f"Brainstem UP at {BRAINSTEM_URL} — status {h.get('status')}, "
                    f"{len(h.get('agents', []))} agents, model {h.get('model')}.") if h else \
                   f"Brainstem NOT running at {BRAINSTEM_URL}. Call 'brainstem_bootstrap'."
        elif name == "brainstem_bootstrap":
            text = _bootstrap()
        else:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"Unknown tool '{name}'."}], "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
    if method and method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    log(f"bridging {BRAINSTEM_URL}")
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
