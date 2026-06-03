#!/usr/bin/env python3
"""Functional tests for the STATIC MCP (rapp-static-mcp/1.0). Zero external deps:

    python3 tests/test_static_mcp.py

Builds a static catalog from examples/manifest.json into a temp dir, serves it via
rapp_static_mcp.py over stdio, and checks:
  1. build emits a tools.json with the expected MCP tool + a content-addressed _frame
  2. tools/list + tools/call round-trip ("hello", name=world -> "Hello, world!")
  3. determinism — two builds of the same bytes produce the same sha8
  4. integrity gate — a tampered frame is REFUSED, never executed
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def mcp(cmd, requests, env=None, timeout=30):
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1,
                            env={**os.environ, **(env or {})})
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


def build(out_dir, self_base):
    rc = subprocess.run(
        [PY, os.path.join(REPO, "build_static_mcp.py"),
         os.path.join(REPO, "examples", "manifest.json"), "--out", out_dir, "--self", self_base],
        capture_output=True, text=True)
    return rc


def run():
    fails = []
    work = tempfile.mkdtemp(prefix="static_mcp_test_")
    try:
        out = os.path.join(work, "catalog")
        cache = os.path.join(work, "cache")

        # 1. build
        rc = build(out, out)  # self == out so _frame.url is a local path the client can fetch
        if rc.returncode != 0:
            fails.append(f"build failed: {rc.stderr.strip()[-300:]}")
        tools_path = os.path.join(out, "api", "v1", "tools.json")
        if not os.path.exists(tools_path):
            fails.append("build did not produce api/v1/tools.json")
            return fails
        cat = json.load(open(tools_path))
        tool = next((t for t in cat.get("tools", []) if t["name"] == "hello"), None)
        if not tool:
            fails.append(f"tools.json missing 'hello' tool: {[t['name'] for t in cat.get('tools', [])]}")
        else:
            fr = tool.get("_frame", {})
            if not (fr.get("sha256") and fr.get("sha8") and fr.get("class") == "HelloAgent"):
                fails.append(f"hello _frame malformed: {fr}")
            if not os.path.exists(os.path.join(out, "agents", "hello", f"{fr.get('sha8')}.py")):
                fails.append("content-addressed frame file missing")

        # 2. tools/list + tools/call round-trip
        r = mcp([PY, os.path.join(REPO, "rapp_static_mcp.py"), tools_path],
                HANDSHAKE + [
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "hello", "arguments": {"name": "world"}}}],
                env={"RAPP_STATIC_MCP_CACHE": cache})
        names = {t.get("name") for t in (r.get(2) or {}).get("tools", [])}
        if "hello" not in names:
            fails.append(f"tools/list missing 'hello': {sorted(names)}")
        # _frame must be stripped from the served tool list
        if any("_frame" in t for t in (r.get(2) or {}).get("tools", [])):
            fails.append("tools/list leaked the _frame x-extension to the host")
        text = ((r.get(3) or {}).get("content") or [{}])[0].get("text", "")
        if "Hello, world!" not in text:
            fails.append(f"tools/call returned {text!r}")

        # 3. determinism — rebuild into a second dir, sha8 must match
        out2 = os.path.join(work, "catalog2")
        build(out2, out2)
        cat2 = json.load(open(os.path.join(out2, "api", "v1", "tools.json")))
        s1 = tool["_frame"]["sha8"] if tool else None
        s2 = next((t["_frame"]["sha8"] for t in cat2["tools"] if t["name"] == "hello"), None)
        if s1 != s2:
            fails.append(f"non-deterministic sha8: {s1} != {s2}")

        # 4. integrity gate — tamper the frame, fresh cache, expect REFUSAL (no exec)
        if tool:
            frame_file = os.path.join(out, "agents", "hello", f"{tool['_frame']['sha8']}.py")
            with open(frame_file, "a", encoding="utf-8") as f:
                f.write("\n# tampered: inject behavior change\n")
            cache2 = os.path.join(work, "cache_tamper")
            r = mcp([PY, os.path.join(REPO, "rapp_static_mcp.py"), tools_path],
                    HANDSHAKE + [{"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "hello", "arguments": {"name": "evil"}}}],
                    env={"RAPP_STATIC_MCP_CACHE": cache2})
            res = r.get(4) or {}
            txt = (res.get("content") or [{}])[0].get("text", "")
            if not res.get("isError") or "integrity" not in txt.lower():
                fails.append(f"tampered frame was NOT refused: isError={res.get('isError')} text={txt!r}")
            if "Hello, evil" in txt:
                fails.append("tampered frame EXECUTED — integrity gate failed")
            if "Traceback" in txt or ".py" in txt:
                fails.append(f"integrity-refusal leaked a traceback/path: {txt!r}")

        # 5. unknown tool — clean one-line message, byte-parity with rapp_mcp.py, no path leak
        r = mcp([PY, os.path.join(REPO, "rapp_static_mcp.py"), tools_path],
                HANDSHAKE + [{"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "ghost", "arguments": {}}}],
                env={"RAPP_STATIC_MCP_CACHE": cache})
        res = r.get(5) or {}
        utxt = (res.get("content") or [{}])[0].get("text", "")
        if not (res.get("isError") and utxt == "Unknown agent tool 'ghost'."):
            fails.append(f"unknown-tool response not clean/parity: {utxt!r}")
        if "Traceback" in utxt or ".py" in utxt or "/" in utxt:
            fails.append(f"unknown-tool leaked a traceback/abs path: {utxt!r}")

        # 6. fail-closed integrity — forged tools.json with EMPTY sha256 must be refused, not run
        forged = os.path.join(work, "forged")
        os.makedirs(os.path.join(forged, "agents", "hello"), exist_ok=True)
        evil = os.path.join(forged, "agents", "hello", "deadbeef0000.py")
        with open(evil, "w") as f:
            f.write("from basic_agent import BasicAgent\n"
                    "class HelloAgent(BasicAgent):\n"
                    "    def __init__(self):\n"
                    "        self.name='hello'; self.metadata={'name':'hello','description':'x','parameters':{'type':'object','properties':{}}}\n"
                    "    def perform(self, **k): return 'PWNED'\n")
        forged_tools = os.path.join(forged, "tools.json")
        json.dump({"schema": "rapp-static-mcp-tools/1.0", "self": forged,
                   "server": {"name": "rapp-static-mcp", "version": "1.0.0", "protocol": "2024-11-05"},
                   "tools": [{"name": "hello", "description": "x",
                              "inputSchema": {"type": "object", "properties": {}},
                              "_frame": {"url": evil, "sha256": "", "sha8": "deadbeef0000",
                                         "class": "HelloAgent", "bytes": 1, "pinned": False}}]},
                  open(forged_tools, "w"))
        r = mcp([PY, os.path.join(REPO, "rapp_static_mcp.py"), forged_tools],
                HANDSHAKE + [{"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "hello", "arguments": {}}}],
                env={"RAPP_STATIC_MCP_CACHE": os.path.join(work, "cache_forged")})
        ftxt = ((r.get(6) or {}).get("content") or [{}])[0].get("text", "")
        if "PWNED" in ftxt:
            fails.append("FAIL-OPEN: forged catalog with empty sha256 EXECUTED arbitrary code")
        if not (r.get(6) or {}).get("isError"):
            fails.append(f"empty-sha256 frame was not refused: {ftxt!r}")
        if "Traceback" in ftxt or ".py" in ftxt:
            fails.append(f"empty-sha256 refusal leaked a traceback/path: {ftxt!r}")

        # 7. multi-class frame — build emits one tool per class (parity with rapp_mcp.py)
        multi = os.path.join(work, "multi")
        os.makedirs(multi, exist_ok=True)
        with open(os.path.join(multi, "multi_agent.py"), "w") as f:
            f.write("from basic_agent import BasicAgent\n"
                    "class AlphaAgent(BasicAgent):\n"
                    "    def __init__(self):\n"
                    "        self.name='alpha'; self.metadata={'name':'alpha','description':'a','parameters':{'type':'object','properties':{}}}\n"
                    "    def perform(self, **k): return 'A'\n"
                    "class BetaAgent(BasicAgent):\n"
                    "    def __init__(self):\n"
                    "        self.name='beta'; self.metadata={'name':'beta','description':'b','parameters':{'type':'object','properties':{}}}\n"
                    "    def perform(self, **k): return 'B'\n")
        mman = os.path.join(multi, "manifest.json")
        mout = os.path.join(multi, "out")
        json.dump({"schema": "rapp-static-mcp-manifest/1.0", "name": "multi",
                   "server": {"name": "rapp-static-mcp", "version": "1.0.0", "protocol": "2024-11-05"},
                   "self": mout, "agents": [{"grail": {"label": "x", "url": "multi_agent.py"}}]},
                  open(mman, "w"))
        rc = subprocess.run([PY, os.path.join(REPO, "build_static_mcp.py"), mman, "--out", mout, "--self", mout],
                            capture_output=True, text=True)
        if rc.returncode != 0:
            fails.append(f"multi-class build failed: {rc.stderr.strip()[-200:]}")
        else:
            mnames = {t["name"] for t in json.load(open(os.path.join(mout, "api", "v1", "tools.json")))["tools"]}
            if mnames != {"alpha", "beta"}:
                fails.append(f"multi-class build did not emit both tools: {sorted(mnames)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return fails


def _report(fails):
    if fails:
        print("FAILED:")
        for f in fails:
            print("  -", f)
    else:
        print("OK — build emits a content-addressed catalog; static client serves tools/list + "
              "tools/call; builds are deterministic; tampered frames are refused.")


# pytest entry point
def test_static_mcp():
    fails = run()
    _report(fails)
    assert not fails, "static MCP failures:\n" + "\n".join(fails)


if __name__ == "__main__":
    fails = run()
    _report(fails)
    sys.exit(1 if fails else 0)
