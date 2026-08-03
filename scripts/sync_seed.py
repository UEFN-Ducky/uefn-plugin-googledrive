#!/usr/bin/env python3
"""Copy this package into ducky_app/frontend/uefn_plugins/<id> for EXE seeding."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]  # UEFN-Ducky/
SKIP = {".git", "scripts", "deploy", ".gitignore", "README.md", "__pycache__"}


def main() -> None:
    if not (ROOT / "plugin.json").is_file():
        raise SystemExit(f"missing plugin.json in {ROOT}")
    pid = str(json.loads((ROOT / "plugin.json").read_text(encoding="utf-8")).get("id") or "").strip()
    if not pid:
        raise SystemExit("plugin.json missing id")
    seed = REPO / "ducky_app" / "frontend" / "uefn_plugins" / pid
    if seed.exists():
        shutil.rmtree(seed)
    seed.mkdir(parents=True)
    for path in ROOT.iterdir():
        if path.name in SKIP or path.name.startswith("."):
            continue
        dest = seed / path.name
        if path.is_dir():
            shutil.copytree(path, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(path, dest)
    manifest = json.loads((seed / "plugin.json").read_text(encoding="utf-8"))
    manifest["source"] = "bundled"
    (seed / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"synced {ROOT.name} -> {seed}")


if __name__ == "__main__":
    main()
