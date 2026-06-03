#!/usr/bin/env python3
"""build_static_mcp.py — turn a hand-edited manifest into a STATIC MCP catalog.

The static profile of rapp-mcp (`rapp-static-mcp/1.0`), built on the
`rapp-static-api/1.0` pattern: a `manifest.json` you hand-edit -> one idempotent
build step -> a generated, CDN-servable catalog under `raw.githubusercontent.com`:

    api/v1/tools.json   the pre-baked MCP tools/list payload (zero compute to serve)
    agents/<tool>/<sha8>.py   append-only, content-addressed, immutable agent frames
    registry.json       the full index — every version of every tool, pinnable forever
    api/v1/status.json  drift verdict, shaped so rapp-god can OBSERVE this repo as a part
    api/v1/badge.json   shields.io endpoint badge

MCP's tools/call needs compute, but rapp-mcp's thesis is "bytes are the contract":
so the CATALOG and the agent BYTES are 100% static here; the tiny universal
`rapp_static_mcp.py` shim is the only thing that runs (fetch a pinned frame, verify
its sha256, exec locally). This build step is the direct generalization of
rapp_mcp.py's local agents dir -> a list of grail URLs.

USAGE
    python3 build_static_mcp.py manifest.json                 # build into repo root
    python3 build_static_mcp.py manifest.json --out DIR       # build into DIR
    python3 build_static_mcp.py manifest.json --self URL_OR_DIR  # override frame base
    python3 build_static_mcp.py manifest.json --check         # fail if output would change

Pure stdlib. No network needed for local (file path) grails; http(s) grails use urllib.
"""
import argparse
import hashlib
import json
import os
import sys
import types
import urllib.error
import urllib.request

SCHEMA_TOOLS = "rapp-static-mcp-tools/1.0"
SCHEMA_REGISTRY = "rapp-static-mcp-registry/1.0"
SCHEMA_STATUS = "rapp-static-mcp-status/1.0"
GENERATED = "1970-01-01T00:00:00Z"  # stable placeholder; real builds may stamp post-hoc
DATA_DIR = os.path.join(os.path.expanduser("~"), ".rapp_static_mcp_build_data")


def log(msg):
    sys.stderr.write(f"[build-static-mcp] {msg}\n")
    sys.stderr.flush()


# ── Shims so brainstem agents introspect/exec standalone (copied from rapp_mcp.py) ──
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


def load_agents(src_text, modname):
    """Exec agent bytes in-memory and return ALL (instance, class_name) pairs. Mirrors
    rapp_mcp.py, which registers EVERY class with perform()+name+metadata (one file may
    define several agents); excludes BasicAgent/object and underscore-prefixed names."""
    _register_shims()
    mod = types.ModuleType(modname)
    mod.__dict__["__name__"] = modname
    exec(compile(src_text, modname, "exec"), mod.__dict__)
    found = []
    for attr in dir(mod):
        cls = getattr(mod, attr)
        if (isinstance(cls, type) and hasattr(cls, "perform")
                and attr not in ("BasicAgent", "object") and not attr.startswith("_")):
            try:
                inst = cls()
                if getattr(inst, "name", None) and getattr(inst, "metadata", None):
                    found.append((inst, attr))
            except Exception:
                continue
    if not found:
        raise ValueError(f"no BasicAgent-style class with name+metadata found in {modname}")
    return found


# ── fetch: http(s) URL OR local path (relative resolved against base_dir) ───────────
def fetch_bytes(src, base_dir):
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "rapp-static-mcp-build/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    path = src[len("file://"):] if src.startswith("file://") else src
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    with open(path, "rb") as f:
        return f.read()


def sha_full(b):
    return hashlib.sha256(b).hexdigest()


def sha8(b):
    # Short content address: first 12 hex chars (48 bits) of the sha256. The "sha8" name
    # is the rapp-static-api short-id convention; it is 12 hex chars, not 8.
    return hashlib.sha256(b).hexdigest()[:12]


# ── stable JSON write (idempotent: identical input -> byte-identical file) ───────────
def write_json_stable(path, obj, prev_generated_key="generated"):
    """Write pretty JSON. If an existing file differs ONLY by the `generated`
    timestamp, preserve the old timestamp so scheduled rebuilds commit no noise."""
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
            new_cmp = dict(obj); old_cmp = dict(old)
            new_cmp.pop(prev_generated_key, None); old_cmp.pop(prev_generated_key, None)
            if new_cmp == old_cmp and prev_generated_key in old:
                obj = dict(obj); obj[prev_generated_key] = old[prev_generated_key]
        except Exception:
            pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    if os.path.exists(path) and open(path, encoding="utf-8").read() == text:
        return False
    open(path, "w", encoding="utf-8").write(text)
    return True


def ensure_frame(out_dir, frame_rel, raw, full, check, would_change):
    """Write the content-addressed frame if absent; if present, RE-VERIFY its on-disk bytes
    hash to `full` (append-only frames are immutable — a mismatch at a fixed sha8 path is
    corruption: self-healed in build mode, flagged in --check). Returns True iff it wrote."""
    frame_abs = os.path.join(out_dir, frame_rel)
    if os.path.exists(frame_abs):
        if sha_full(open(frame_abs, "rb").read()) == full:
            return False
        if check:
            would_change.append(frame_rel + " (corrupt)")
            return False
        with open(frame_abs, "wb") as f:
            f.write(raw)
        log(f"healed corrupted frame {frame_rel}")
        return True
    if check:
        would_change.append(frame_rel + " (missing)")
        return False
    os.makedirs(os.path.dirname(frame_abs), exist_ok=True)
    with open(frame_abs, "wb") as f:
        f.write(raw)
    return True


def build(manifest_path, out_dir, self_override, check):
    manifest_path = os.path.abspath(manifest_path)
    base_dir = os.path.dirname(manifest_path)
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    out_dir = os.path.abspath(out_dir)
    self_base = (self_override or manifest.get("self") or out_dir).rstrip("/")
    server = manifest.get("server", {}) or {}
    server.setdefault("name", manifest.get("name", "rapp-static-mcp"))
    server.setdefault("version", "1.0.0")
    server.setdefault("protocol", "2024-11-05")

    # prior registry (for append-only history + first_captured provenance)
    reg_path = os.path.join(out_dir, "registry.json")
    prev_reg = {}
    if os.path.exists(reg_path):
        try:
            prev_reg = {a["tool"]: a for a in json.load(open(reg_path, encoding="utf-8")).get("agents", [])}
        except Exception:
            prev_reg = {}

    tools, reg_agents, status_agents = [], [], []
    seen_tools = set()
    changed_any = False
    would_change = []

    for entry in manifest.get("agents", []):
        grail = entry.get("grail", {}) or {}
        url = grail.get("url")
        if not url:
            raise ValueError(f"manifest agent missing grail.url: {entry}")
        raw = fetch_bytes(url, base_dir)
        text = raw.decode("utf-8")
        full, short = sha_full(raw), sha8(raw)
        # one frame may define several agents — emit a tool per class, like rapp_mcp.py
        agents_in_frame = load_agents(text, f"buildagent_{short}")
        tool_override = entry.get("tool")
        if tool_override and len(agents_in_frame) > 1:
            raise ValueError(f"manifest 'tool' override is invalid for a multi-class frame "
                             f"({url} defines {len(agents_in_frame)} agents) — split the file or drop the override")
        pin = entry.get("pin")
        pinned = bool(pin)

        for inst, class_name in agents_in_frame:
            md = inst.metadata or {}
            tool = tool_override or inst.name
            if tool in seen_tools:
                raise ValueError(f"duplicate tool name '{tool}' — set a unique manifest 'tool' override")
            seen_tools.add(tool)

            # append-only content-addressed frame (one copy per tool dir; identical bytes share a sha)
            frame_rel = f"agents/{tool}/{short}.py"
            if ensure_frame(out_dir, frame_rel, raw, full, check, would_change):
                changed_any = True

            # pin resolution
            if pin and pin != short:
                pin_path = os.path.join(out_dir, f"agents/{tool}/{pin}.py")
                if not os.path.exists(pin_path):
                    raise ValueError(f"pin '{pin}' for tool '{tool}' has no existing frame")
                pin_bytes = open(pin_path, "rb").read()
                frame_url = f"{self_base}/agents/{tool}/{pin}.py"
                frame_sha, frame_sha8, frame_bytes_len = sha_full(pin_bytes), pin, len(pin_bytes)
            else:
                frame_url = f"{self_base}/{frame_rel}"
                frame_sha, frame_sha8, frame_bytes_len = full, short, len(raw)

            # tools.json entry — clean MCP tool + a leading-underscore _frame x-extension
            tools.append({
                "name": tool,
                "description": md.get("description", tool),
                "inputSchema": md.get("parameters", {"type": "object", "properties": {}}),
                "_frame": {
                    "url": frame_url,
                    "sha256": frame_sha,
                    "sha8": frame_sha8,
                    "class": class_name,
                    "bytes": frame_bytes_len,
                    "pinned": pinned,
                },
            })

            # registry: append-only version history (preserve first_captured)
            prev = prev_reg.get(tool, {})
            versions = {v["sha8"]: v for v in prev.get("versions", [])}
            if short not in versions:
                versions[short] = {
                    "sha256": full, "sha8": short, "bytes": len(raw),
                    "path": frame_rel, "url": f"{self_base}/{frame_rel}",
                    "first_captured": GENERATED, "class": class_name,
                }
            reg_agents.append({
                "tool": tool,
                "group": entry.get("group", ""),
                "kind": entry.get("kind", "observe"),
                "note": entry.get("note", ""),
                "grail_label": grail.get("label", ""),
                "grail_url": url,
                "grail_sha8": short,
                "current_sha8": frame_sha8,
                "current_frame_url": frame_url,
                "pinned": pinned,
                "version_count": len(versions),
                "versions": sorted(versions.values(), key=lambda v: v["sha8"]),
            })
            status_agents.append({
                "tool": tool, "group": entry.get("group", ""), "kind": entry.get("kind", "observe"),
                "grail_sha8": short, "current_sha8": frame_sha8,
                "drift": False, "update_available": pinned and frame_sha8 != short,
                "version_count": len(versions),
            })

    tools_obj = {
        "schema": SCHEMA_TOOLS, "generated": GENERATED, "self": self_base,
        "server": server, "tools": tools,
    }
    registry_obj = {
        "schema": SCHEMA_REGISTRY, "name": manifest.get("name", "rapp-static-mcp"),
        "generated": GENERATED, "self": self_base, "policy": manifest.get("policy", {}),
        "release": manifest.get("release", {}),
        "summary": {"tools": len(tools), "versions": sum(a["version_count"] for a in reg_agents)},
        "agents": reg_agents,
    }
    n_upd = sum(1 for a in status_agents if a["update_available"])
    status_obj = {
        "schema": SCHEMA_STATUS, "generated": GENERATED, "self": self_base,
        "summary": {"tools": len(tools), "in_sync": len(tools) - n_upd, "update_available": n_upd},
        "agents": status_agents,
    }
    badge_obj = {
        "schemaVersion": 1, "label": "rapp-static-mcp",
        "message": f"{len(tools)} tools" + (f" · {n_upd} update" if n_upd else " · in sync"),
        "color": "blue" if not n_upd else "orange",
    }

    targets = {
        os.path.join(out_dir, "api/v1/tools.json"): tools_obj,
        os.path.join(out_dir, "registry.json"): registry_obj,
        os.path.join(out_dir, "api/v1/status.json"): status_obj,
        os.path.join(out_dir, "api/v1/badge.json"): badge_obj,
    }
    for path, obj in targets.items():
        if check:
            existing = open(path, encoding="utf-8").read() if os.path.exists(path) else None
            cand = dict(obj)
            if existing:
                try:
                    old = json.loads(existing)
                    a, b = dict(cand), dict(old)
                    a.pop("generated", None); b.pop("generated", None)
                    if a != b:
                        would_change.append(os.path.relpath(path, out_dir))
                except Exception:
                    would_change.append(os.path.relpath(path, out_dir))
            else:
                would_change.append(os.path.relpath(path, out_dir))
        else:
            if write_json_stable(path, obj):
                changed_any = True

    nojekyll = os.path.join(out_dir, ".nojekyll")
    if not check and not os.path.exists(nojekyll):
        open(nojekyll, "w").close()

    if check:
        if would_change:
            log(f"OUT OF DATE: would change {would_change}")
            return 1
        log("up to date")
        return 0
    log(f"built {len(tools)} tools -> {out_dir} (self={self_base})")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Build a static MCP catalog from a manifest.")
    ap.add_argument("manifest")
    ap.add_argument("--out", default=".", help="output dir (default: cwd)")
    ap.add_argument("--self", dest="self_base", default=None,
                    help="override the frame base URL/dir (default: manifest 'self' or --out)")
    ap.add_argument("--check", action="store_true", help="exit non-zero if output would change")
    a = ap.parse_args()
    sys.exit(build(a.manifest, a.out, a.self_base, a.check))


if __name__ == "__main__":
    main()
