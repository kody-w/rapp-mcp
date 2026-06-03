#!/usr/bin/env python3
"""rapp-static-mcp — serve a STATIC, CDN-hosted MCP catalog in any MCP host.

The execution half of the static profile (`rapp-static-mcp/1.0`). Point it at a
`tools.json` that `build_static_mcp.py` published to `raw.githubusercontent.com`
(or any base URL / local path). It serves the pre-baked catalog with zero compute,
and on tools/call it fetches the agent's pinned, content-addressed frame, VERIFIES
its sha256 before running it, and execs locally.

This is rapp_mcp.py with "fetch a pinned URL" swapped in for "read a local file":
the catalog and the agent bytes live on a global CDN, immutable and pinnable; the
only thing that runs is this tiny shim. The wire shape is byte-identical to
rapp_mcp.py, so any host already wired for rapp-mcp works unchanged.

USAGE
    python3 rapp_static_mcp.py <TOOLS_URL | BASE_URL | manifest.json | local path>

MCP client config, e.g.:
    {
      "mcpServers": {
        "rapp-static-mcp": {
          "command": "python3",
          "args": ["/abs/path/rapp_static_mcp.py",
                   "https://raw.githubusercontent.com/<owner>/<repo>/main/api/v1/tools.json"]
        }
      }
    }

ENV
    RAPP_STATIC_MCP_URL    overrides argv[1]
    RAPP_STATIC_MCP_CACHE  content-addressed cache dir (default ~/.rapp_static_mcp)
    RAPP_STATIC_MCP_DATA   agent local-storage dir   (default ~/.rapp_static_mcp_data)

Pure stdlib. Integrity gate: a frame whose bytes don't match the baked sha256 is
NEVER executed. A pinned-and-cached frame keeps working fully offline.
"""
import hashlib
import json
import os
import sys
import traceback
import types
import urllib.error
import urllib.request

SERVER_NAME = "rapp-static-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL = "2024-11-05"

SOURCE = os.environ.get("RAPP_STATIC_MCP_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")
CACHE_DIR = os.environ.get("RAPP_STATIC_MCP_CACHE", os.path.join(os.path.expanduser("~"), ".rapp_static_mcp"))
DATA_DIR = os.environ.get("RAPP_STATIC_MCP_DATA", os.path.join(os.path.expanduser("~"), ".rapp_static_mcp_data"))


def log(msg):
    sys.stderr.write(f"[rapp-static-mcp] {msg}\n")
    sys.stderr.flush()


# ── Shims so brainstem agents exec standalone (verbatim from rapp_mcp.py) ───────────
class BasicAgent:
    def __init__(self, name=None, metadata=None):
        if name and not getattr(self, "name", None):
            self.name = name
        if metadata and not getattr(self, "metadata", None):
            self.metadata = metadata

    def perform(self, **kwargs):
        raise NotImplementedError


class _LocalStorage:
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

    def __getattr__(self, _):
        return lambda *a, **k: None


def _register_shims():
    if "agents" not in sys.modules:
        m = types.ModuleType("agents"); m.__path__ = []; sys.modules["agents"] = m
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


# ── fetch: http(s) URL or local path; returns bytes (raises on failure) ─────────────
def fetch_bytes(src):
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "rapp-static-mcp/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    path = src[len("file://"):] if src.startswith("file://") else src
    with open(path, "rb") as f:
        return f.read()


def _tools_url(source):
    """Accept a direct tools.json, a manifest.json (-> sibling api/v1/tools.json),
    or a base URL/dir (-> base/api/v1/tools.json)."""
    s = source.rstrip("/")
    if s == "tools.json" or s.endswith("/tools.json"):
        return s
    if s == "manifest.json" or s.endswith("/manifest.json"):
        return s[: -len("manifest.json")].rstrip("/") + "/api/v1/tools.json"
    return s + "/api/v1/tools.json"


def _is_sha256(s):
    return isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower())


# ── catalog: fetched once at initialize, cached for offline resilience ──────────────
_CATALOG = {"server": None, "tools": [], "frames": {}}


def load_catalog():
    url = _tools_url(SOURCE)
    cache_path = os.path.join(CACHE_DIR, "tools.json")
    raw = None
    try:
        raw = fetch_bytes(url)
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(raw)
    except Exception as e:
        log(f"fetch tools.json failed ({e}); trying cache {cache_path}")
        if os.path.exists(cache_path):
            raw = open(cache_path, "rb").read()
        else:
            log("no cached catalog available")
            return
    try:
        cat = json.loads(raw.decode("utf-8"))
    except Exception as e:
        log(f"bad tools.json: {e}")
        return
    _CATALOG["server"] = cat.get("server") or {}
    clean, frames = [], {}
    for t in cat.get("tools", []):
        frames[t["name"]] = t.get("_frame", {})
        clean.append({k: v for k, v in t.items() if k != "_frame"})
    _CATALOG["tools"] = clean
    _CATALOG["frames"] = frames
    log(f"catalog loaded: {len(clean)} tools from {url}")


# ── frame fetch + integrity gate + local exec (the only compute) ────────────────────
def _frame_bytes(tool, frame):
    """Return verified frame bytes, using a content-addressed local cache. The integrity
    gate FAILS CLOSED: a frame with no valid 64-hex sha256 is refused outright, and fetched
    bytes are compared unconditionally — untrusted/forked catalogs cannot run unverified code."""
    want = (frame.get("sha256") or "").lower()
    if not _is_sha256(want):
        raise ValueError(f"frame for '{tool}' has no valid sha256 — refusing to fetch or exec")
    sha8 = frame.get("sha8") or want[:12]
    cache_path = os.path.join(CACHE_DIR, "agents", tool, f"{sha8}.py")
    if os.path.exists(cache_path):
        b = open(cache_path, "rb").read()
        if hashlib.sha256(b).hexdigest() == want:
            return b  # cached + verified -> fully offline
    b = fetch_bytes(frame["url"])  # may raise if offline + uncached
    got = hashlib.sha256(b).hexdigest()
    if got != want:
        raise ValueError(f"integrity check FAILED for '{tool}': expected {want[:12]}, got {got[:12]} — refusing to exec")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(b)
    return b


def call_tool(tool, args):
    frame = _CATALOG["frames"].get(tool)
    if not frame:
        raise KeyError(f"Unknown tool '{tool}'.")
    raw = _frame_bytes(tool, frame)
    _register_shims()
    sha8 = frame.get("sha8") or frame.get("sha256", "")[:12]
    modname = f"staticagent_{tool}_{sha8}"
    mod = types.ModuleType(modname)
    mod.__dict__["__name__"] = modname
    exec(compile(raw.decode("utf-8"), modname, "exec"), mod.__dict__)
    # prefer the build-resolved class; fall back to rapp_mcp.py's scan
    cls = None
    want_cls = frame.get("class")
    if want_cls and isinstance(getattr(mod, want_cls, None), type):
        cls = getattr(mod, want_cls)
    if cls is None:
        for attr in dir(mod):
            c = getattr(mod, attr)
            if (isinstance(c, type) and hasattr(c, "perform")
                    and attr not in ("BasicAgent", "object") and not attr.startswith("_")):
                cls = c
                break
    if cls is None:
        raise ValueError(f"no BasicAgent-style class found in frame for '{tool}'")
    inst = cls()
    result = inst.perform(**args)
    return result if isinstance(result, str) else json.dumps(result)


# ── MCP stdio JSON-RPC loop (shapes identical to rapp_mcp.py) ────────────────────────
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        load_catalog()
        srv = _CATALOG.get("server") or {}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": srv.get("protocol", PROTOCOL),
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": srv.get("name", SERVER_NAME), "version": srv.get("version", SERVER_VERSION)},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        if not _CATALOG["tools"]:
            load_catalog()
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _CATALOG["tools"]}}
    if method == "tools/call":
        params = req.get("params", {}) or {}
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if not _CATALOG["frames"]:
            load_catalog()
        if name not in _CATALOG["frames"]:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"Unknown agent tool '{name}'."}], "isError": True}}
        try:
            text = call_tool(name, args)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
        except ValueError as e:
            # Known refusals (integrity failure, no/invalid sha256, no class in frame) — return a
            # clean one-line message. NEVER a traceback: that would leak this server's absolute path.
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        except Exception as e:
            tb = traceback.format_exc().splitlines()[-3:]
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"{name} error: {e}\n" + "\n".join(tb)}], "isError": True}}
    if method and method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    if not SOURCE:
        log("usage: rapp_static_mcp.py <tools.json URL | base URL | manifest.json | local path>")
        sys.exit(2)
    log(f"serving static catalog from {SOURCE}")
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
