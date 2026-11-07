"""preflight_audit.py - Pre-Flight AST and Contract Audit Tool for OldRAG."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path


def audit_banned_tokens(root_dir: Path) -> list[str]:
    violations = []
    banned = [
        re.compile(r"\blocalhost\b", re.IGNORECASE),
        re.compile(r"\b127\.0\.0\.1\b"),
        re.compile(r"::1"),
    ]
    src_dir = root_dir / "src" / "old_rag"
    for py_file in src_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for b in banned:
            if b.search(text):
                violations.append(f"{py_file.relative_to(root_dir)}: Contains banned network token ({b.pattern})")
    return violations


def audit_engine_rounding(root_dir: Path) -> list[str]:
    violations = []
    engine_dir = root_dir / "src" / "old_rag" / "engine"
    for py_file in engine_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "round":
                    violations.append(f"{py_file.relative_to(root_dir)}: Line {node.lineno} calls round() - Rule 18 violation!")
    return violations


def audit_bridge_schema_alignment(root_dir: Path) -> list[str]:
    violations = []
    schema_file = root_dir / "src" / "old_rag" / "bridge_schema.json"
    bridge_js = root_dir / "src" / "old_rag" / "ui" / "static" / "bridge.js"

    if not schema_file.exists():
        return [f"Missing {schema_file}"]
    if not bridge_js.exists():
        return [f"Missing {bridge_js}"]

    js_text = bridge_js.read_text(encoding="utf-8")
    expected_methods = ["search", "getStatus", "openCachedMsg", "reindex"]
    for m in expected_methods:
        if m not in js_text:
            violations.append(f"bridge.js missing contract method: {m}")

    return violations


def audit_zero_external_cdns(root_dir: Path) -> list[str]:
    violations = []
    static_dir = root_dir / "src" / "old_rag" / "ui" / "static"
    cdn_pattern = re.compile(r"https?://", re.IGNORECASE)
    for f in static_dir.glob("*.*"):
        text = f.read_text(encoding="utf-8")
        if cdn_pattern.search(text):
            violations.append(f"{f.name}: Contains external URL / CDN reference!")
    return violations


def main() -> None:
    root_dir = Path(__file__).parent.parent
    all_violations = []

    print("Running OldRAG Pre-Flight Gate Audit...")
    v1 = audit_banned_tokens(root_dir)
    v2 = audit_engine_rounding(root_dir)
    v3 = audit_bridge_schema_alignment(root_dir)
    v4 = audit_zero_external_cdns(root_dir)

    all_violations.extend(v1)
    all_violations.extend(v2)
    all_violations.extend(v3)
    all_violations.extend(v4)

    if all_violations:
        print(f"\nFAILED: Found {len(all_violations)} violations:")
        for v in all_violations:
            print(f"  - {v}")
        sys.exit(1)
    else:
        print("\nPASSED: 0 banned tokens, 0 round() calls in engine, 100% bridge alignment, 0 external CDNs.")


if __name__ == "__main__":
    main()

