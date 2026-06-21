#!/usr/bin/env python3
"""setup_vscode_mcp_agent — a DROP-IN brainstem agent that self-bootstraps the rapp-mcp bridge so your
WHOLE brainstem shows up in VS Code agent mode (and Claude Desktop / Copilot CLI / Cursor — any MCP host).

Drop this into your brainstem's agents/ folder and just ask the brainstem, e.g. "set up MCP for VS Code".
It will:
  • locate (or `git clone`) the rapp-mcp accessory repo — NEVER into ~/.brainstem/src (brainstem.py stays
    sacred and untouched);
  • write/merge .vscode/mcp.json in your project, registering the bridge;
  • by DEFAULT expose the whole brainstem /chat loop as ONE "ask the brainstem" tool, so its soul.md +
    memory + multi-round agent orchestration stay intact (the twin, not a bag of tools);
  • OPT-IN (mode="both") also expose each agents/*_agent.py as its own MCP tool, for when you want the host
    (Copilot) to orchestrate a single deterministic agent directly.

Everything flows through /chat. No new REST routes, no changes to brainstem.py or the agents. Zero deps.
"""
import json
import os
import subprocess
import urllib.request

try:
    from basic_agent import BasicAgent  # brainstem base class (shimmed at import time)
except Exception:
    class BasicAgent:  # standalone fallback so this file is importable anywhere
        def __init__(self, name, metadata):
            self.name = name
            self.metadata = metadata

MCP_REPO = "kody-w/rapp-mcp"
ACCESSORY = os.path.expanduser("~/.brainstem/neighborhoods/rapp-mcp")   # accessory home, NOT ~/.brainstem/src


class SetupVscodeMcpAgent(BasicAgent):
    def __init__(self):
        self.name = "SetupVSCodeMCP"
        self.metadata = {
            "name": self.name,
            "description": (
                "Bootstrap the rapp-mcp bridge so THIS whole brainstem appears in VS Code agent mode (and any "
                "MCP host) as a tool — without touching brainstem.py. By default it exposes the brainstem's /chat "
                "loop as ONE 'ask the brainstem' tool (preserving its soul, memory and orchestration); set "
                "mode='both' to ALSO expose every agents/*_agent.py as its own MCP tool. Writes .vscode/mcp.json "
                "in your project. Use action='status' to see what is wired up and whether the brainstem is reachable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["install", "status", "uninstall"],
                               "description": "install = wire the bridge + write .vscode/mcp.json; status = report setup + reachability; uninstall = remove the mcp.json entries this added."},
                    "workspace_dir": {"type": "string", "description": "absolute path of the project whose .vscode/mcp.json to write. Defaults to the current working directory."},
                    "mode": {"type": "string", "enum": ["brainstem", "both"],
                             "description": "brainstem (recommended, default) = the whole brainstem /chat loop as ONE tool (keeps the twin's soul + orchestration). both = ALSO expose each agent as its own tool (the host orchestrates)."},
                    "brainstem_url": {"type": "string", "description": "base URL of the running brainstem. Default http://localhost:7071."}
                },
                "required": ["action"]
            }
        }
        super().__init__(self.name, self.metadata)

    # ---- helpers ----
    def _health(self, url):
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=3) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None

    def _ensure_repo(self):
        if os.path.isdir(os.path.join(ACCESSORY, ".git")):
            return True, "rapp-mcp already present at " + ACCESSORY
        os.makedirs(os.path.dirname(ACCESSORY), exist_ok=True)
        for cmd in (["gh", "repo", "clone", MCP_REPO, "--", ACCESSORY],
                    ["git", "clone", "https://github.com/" + MCP_REPO + ".git", ACCESSORY]):
            try:
                if subprocess.run(cmd, capture_output=True).returncode == 0:
                    return True, "cloned rapp-mcp -> " + ACCESSORY
            except Exception:
                pass
        return False, "could not clone " + MCP_REPO + " (need gh or git + network)"

    def _vscode_path(self, ws):
        vs = os.path.join(ws, ".vscode")
        os.makedirs(vs, exist_ok=True)
        return os.path.join(vs, "mcp.json")

    def perform(self, **kwargs):
        action = (kwargs.get("action") or "install").lower()
        ws = os.path.abspath(os.path.expanduser(kwargs.get("workspace_dir") or os.getcwd()))
        mode = kwargs.get("mode") or "brainstem"
        url = (kwargs.get("brainstem_url") or os.environ.get("RAPP_BRAINSTEM_URL") or "http://localhost:7071").rstrip("/")
        agents_dir = os.environ.get("AGENTS_PATH") or os.path.dirname(os.path.abspath(__file__))
        cfg_path = self._vscode_path(ws)
        health = self._health(url)

        if action == "status":
            present = os.path.isdir(os.path.join(ACCESSORY, ".git"))
            cfg = {}
            try:
                cfg = json.load(open(cfg_path))
            except Exception:
                pass
            servers = list((cfg.get("servers") or {}).keys())
            return ("MCP bridge status:\n"
                    f"  • rapp-mcp accessory: {'present' if present else 'NOT installed'} ({ACCESSORY})\n"
                    f"  • brainstem at {url}: {'reachable ✓' if health else 'NOT reachable (start it with ./start.sh)'}\n"
                    f"  • {cfg_path}: {servers or 'no rapp servers registered'}\n"
                    "  Run action='install' to wire it up.")

        # locate / clone the bridge
        ok, msg = self._ensure_repo()
        if not ok:
            return "Setup failed: " + msg
        brainstem_bridge = os.path.join(ACCESSORY, "rapp_brainstem_mcp.py")
        agents_bridge = os.path.join(ACCESSORY, "rapp_mcp.py")

        # load + merge the VS Code mcp.json (VS Code uses the top-level "servers" key)
        cfg = {}
        try:
            cfg = json.load(open(cfg_path))
        except Exception:
            cfg = {}
        cfg.setdefault("servers", {})

        if action == "uninstall":
            for k in ("rapp-brainstem", "rapp-agents"):
                cfg["servers"].pop(k, None)
            json.dump(cfg, open(cfg_path, "w"), indent=2)
            return "Removed rapp MCP servers from " + cfg_path

        # install
        cfg["servers"]["rapp-brainstem"] = {
            "type": "stdio", "command": "python3", "args": [brainstem_bridge],
            "env": {"RAPP_BRAINSTEM_URL": url}
        }
        added = ["rapp-brainstem (the whole brainstem /chat loop as one 'ask the brainstem' tool)"]
        if mode == "both":
            cfg["servers"]["rapp-agents"] = {
                "type": "stdio", "command": "python3", "args": [agents_bridge],
                "env": {"RAPP_AGENTS_DIR": agents_dir}
            }
            added.append("rapp-agents (each agents/*_agent.py as its own tool)")
        json.dump(cfg, open(cfg_path, "w"), indent=2)

        return (
            "✅ MCP bridge wired up — the brainstem stays untouched.\n"
            f"  {msg}\n"
            f"  wrote {cfg_path} with: " + "; ".join(added) + "\n"
            f"  brainstem at {url}: {'reachable ✓' if health else 'NOT running — start it (./start.sh) before using the tool'}\n\n"
            "Next: open this folder in VS Code, switch Copilot Chat to Agent mode, and the brainstem appears in the "
            "tool picker. Ask it anything — it runs its full multi-agent /chat loop and returns the answer. "
            "(Same config works in Claude Desktop / Copilot CLI / Cursor — copy the 'servers' block.)"
        )
