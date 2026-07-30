#!/usr/bin/env python3
"""Zip plugin.json + backend for Store upload. No secrets."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "deploy"
SKIP_NAMES = {".git", "scripts", "deploy", ".gitignore", "README.md", "__pycache__"}
SKIP_SUFFIX = {".pyc", ".pyo", ".zip", ".ducky-plugin"}


def build_zip(*, out: Path | None = None) -> Path:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    pid = str(manifest.get("id") or "").strip()
    version = manifest.get("version") or 1
    if not pid:
        raise SystemExit("plugin.json missing id")
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".dat", ".env", ".pem", ".key"}:
            raise SystemExit(f"refusing to pack secret-looking file: {path}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = out or (OUT_DIR / f"{pid}-{version}.ducky-plugin.zip")
    # Build in memory, CRC-test, then atomic write — never ship a half-written zip.
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(ROOT).parts
            if not rel_parts or rel_parts[0] in SKIP_NAMES:
                continue
            if path.suffix.lower() in SKIP_SUFFIX or path.name.startswith("."):
                continue
            # Force forward-slash arcnames + explicit bytes (stable CRC across OS).
            arc = "/".join(rel_parts)
            zf.writestr(arc, path.read_bytes())
    raw = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"built zip failed CRC for {bad} — refusing to write")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(dest)
    print(f"wrote {dest} ({dest.stat().st_size} bytes, CRC ok)")
    return dest


if __name__ == "__main__":
    build_zip()
