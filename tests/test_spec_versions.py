#!/usr/bin/env python3
"""Checks that rapp-mcp-spec/1.0 ships alongside 2.0, byte-identical. Zero external deps:

    python3 tests/test_spec_versions.py

SPEC.md is rapp-mcp-spec/2.0. Its section 8 promises that a breaking change ships as a new
major alongside the old one, so SPEC-1.0.md keeps the 1.0 text exactly as it was at 651ce82.
SPEC-1.0.md is never edited, not even to fix drift: it is the superseded version, kept whole.
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

SPEC_1_0_SHA256 = "fabb9051b15458c821bca3f9ef7def1856e0a89210f1094186843bcb8b5d192e"
SPEC_1_0_URL = "https://github.com/kody-w/rapp-mcp/blob/main/SPEC-1.0.md"


def main():
    fails = []

    with open(os.path.join(REPO, "SPEC-1.0.md"), "rb") as f:
        old = f.read()
    digest = hashlib.sha256(old).hexdigest()
    if digest != SPEC_1_0_SHA256:
        fails.append(f"SPEC-1.0.md must stay byte-identical to rapp-mcp-spec/1.0: sha256 {digest}")
    if b"> **Spec version:** `rapp-mcp-spec/1.0`" not in old:
        fails.append("SPEC-1.0.md does not declare rapp-mcp-spec/1.0")

    with open(os.path.join(REPO, "SPEC.md"), encoding="utf-8") as f:
        new = f.read()
    if "> **Spec version:** `rapp-mcp-spec/2.0`" not in new:
        fails.append("SPEC.md does not declare rapp-mcp-spec/2.0")
    if new.count(SPEC_1_0_URL) < 2:
        fails.append("SPEC.md must link SPEC-1.0.md from its Supersedes header and from section 8.1")
    if SPEC_1_0_SHA256 not in new:
        fails.append("SPEC.md section 8.1 must record the sha256 of SPEC-1.0.md")

    if fails:
        print("FAILED:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("OK — SPEC.md is rapp-mcp-spec/2.0, and rapp-mcp-spec/1.0 ships alongside it, byte-identical.")


if __name__ == "__main__":
    main()
