"""Google Drive — UEFN Ducky desktop plugin.

Safely pull 3D models, textures, and audio from Google Drive and import them
into UEFN as real assets via the app's import pipeline (``import_asset``).

Modules: ``auth`` (secrets, the Google-only HTTP core, OAuth sign-in) and
``drive`` (ids, safe downloads, zip extraction, listing). This file keeps
``register(api)``: the MCP tools and workflow nodes on top of them.
"""

from __future__ import annotations

import json
import re
import time  # noqa: F401 — re-exported for scripts/selfcheck.py
import zipfile  # noqa: F401 — re-exported for scripts/selfcheck.py
from pathlib import Path
from typing import Any

from . import auth, display
from .auth import (  # noqa: F401 — re-exported for scripts/selfcheck.py
    KEY_API,
    PLUGIN_ID,
    _check_url,
    _host_ok,
    _load_token,
    _oauth_client,
    _run_oauth_flow,
    _using_bundled_client,
    log,
    token_expired,
)
from .drive import (  # noqa: F401 — re-exported for scripts/selfcheck.py
    ALLOWED_EXTS,
    FOLDER_MIME,
    IMPORT_MAX_FILES,
    IMPORTABLE_EXTS,
    _auth_modes,
    _fetch_metadata,
    _human_size,
    _max_download_bytes,
    _public_probe,
    _q_escape,
    _staging_dir,
    _target_folder_id,
    _target_folder_ref,
    _unique_path,
    classify_name,
    download_to_staging,
    extract_drive_id,
    list_drive,
    parse_content_disposition,
    parse_content_range_total,
    parse_public_confirm_form,
    safe_extract_zip,
    sanitize_filename,
)


def _tool_json(payload: dict[str, Any], pretty: bool = False) -> str:
    try:
        from backend.util.json_util import tool_json

        return tool_json(payload, pretty=pretty)
    except Exception:  # noqa: BLE001 — standalone / early-boot fallback
        return json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)


# --------------------------------------------------------------------------- #
# register(api) — MCP tools.
# --------------------------------------------------------------------------- #


def register(api: Any) -> None:
    """Wire the Google Drive tools into the app's shared MCP server."""

    def _fail(exc: Exception, pretty: bool) -> str:
        return _tool_json({"ok": False, "error": str(exc) or exc.__class__.__name__}, pretty=pretty)

    @api.tool(intent=r"\b(google\s*drive|gdrive|drive\.google|drive\s+(link|folder|file))\b", listener=False)
    def gdrive_status(pretty: bool = False) -> str:
        """Google Drive plugin status: access modes, connected account, staging folder.

        Call this first. Shows whether OAuth (private Drive), an API key (public
        folders), or only public-link downloads are available, plus where files land
        on disk and the active size cap.
        """
        try:
            tok = _load_token()
            client_id, client_secret = _oauth_client()
            sign_in_ready = bool(client_id and client_secret)
            api_key_set = bool(auth._get_secret(KEY_API))
            target_ref = _target_folder_ref()
            target_id = _target_folder_id()
            payload: dict[str, Any] = {
                "ok": True,
                "connected": bool(tok),
                "account": str((tok or {}).get("email") or "") or None,
                "sign_in_ready": sign_in_ready,
                "sign_in_kind": (
                    "bundled" if _using_bundled_client()
                    else "custom" if sign_in_ready
                    else "none"
                ),
                "target_folder": target_ref or None,
                "target_folder_id": target_id or None,
                "api_key_set": api_key_set,
                "access_modes": _auth_modes(),
                "staging_dir": str(_staging_dir()),
                "max_download": _human_size(_max_download_bytes()),
                "allowed_extensions": sorted(ALLOWED_EXTS),
                "importable_extensions": sorted(IMPORTABLE_EXTS),
            }
            hints: list[str] = []
            if tok:
                hints.append(f"Signed in as {payload['account'] or 'your Google account'}.")
            elif sign_in_ready:
                hints.append(
                    "Sign in: run gdrive_connect — a Google sign-in opens in the browser "
                    "(one click, nothing to paste)."
                )
            else:
                hints.append(
                    "One-click sign-in isn't wired up in this build. Public share links "
                    "still work via gdrive_download with no login."
                )
            if target_ref and not target_id:
                hints.append(
                    f"'Your Drive folder' setting isn't a valid Drive folder link/id: {target_ref!r}"
                )
            elif not target_ref:
                hints.append(
                    "Tip: set 'Your Drive folder' in Settings → Google Drive so gdrive_list / "
                    "gdrive_search default to one folder you drop assets into."
                )
            if not api_key_set and not sign_in_ready:
                hints.append(
                    "To browse public folders without signing in, add a Google API key in "
                    "Settings → Google Drive → Advanced."
                )
            if client_id and not client_id.endswith(".apps.googleusercontent.com"):
                hints.append(
                    "Warning: the OAuth Client ID does not look like *.apps.googleusercontent.com "
                    "— double-check it."
                )
            payload["hints"] = hints
            return _tool_json(payload, pretty=pretty)
        except Exception as exc:  # noqa: BLE001 — never raise across the MCP bridge
            return _fail(exc, pretty)

    @api.tool(listener=False)
    def gdrive_connect(timeout_s: float = 180.0, pretty: bool = False) -> str:
        """Sign in with Google (read-only Drive scope) via one-click browser sign-in.

        Opens the default browser on Google's own consent screen; the user picks
        their account and approves read-only Drive — credentials never touch this
        app, only a read-only token is stored (encrypted, on-device). Uses the
        plugin's built-in Google client, so there is nothing to paste. Blocks until
        sign-in completes or timeout_s (max 300) elapses. Tell the user to finish
        the sign-in in their browser.
        """
        try:
            result = _run_oauth_flow(float(timeout_s or 180.0))
            account = result.get("account") or ""
            return _tool_json(
                {
                    "ok": True,
                    "connected": True,
                    "account": account or None,
                    "scope": "read-only (drive.readonly)",
                    "message": (
                        f"Connected to Google Drive as {account}." if account else
                        "Connected to Google Drive."
                    ),
                },
                pretty=pretty,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    @api.tool(listener=False)
    def gdrive_disconnect(revoke: bool = True, pretty: bool = False) -> str:
        """Disconnect Google Drive: revoke the token at Google (best effort) and erase it."""
        try:
            revoked = auth.revoke_and_clear(bool(revoke))
            return _tool_json(
                {
                    "ok": True,
                    "connected": False,
                    "revoked_at_google": revoked,
                    "message": "Google Drive disconnected — the stored token was erased.",
                },
                pretty=pretty,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    @api.tool(listener=False)
    def gdrive_list(
        folder: str = "",
        page_size: int = 50,
        page_token: str = "",
        pretty: bool = False,
    ) -> str:
        """List a Google Drive folder (models/textures/audio flagged as importable).

        folder: a Drive folder link/id. Empty → the user's configured "Your Drive
        folder" (Settings → Google Drive) if set, else My Drive root (signed-in
        only). API-key mode can only list public folders. Returns id/name/kind/size
        rows — pass a row's id to gdrive_download or gdrive_import_to_uefn.
        """
        try:
            data = list_drive(folder, page_size=page_size, page_token=page_token)
            data["ok"] = True
            return _tool_json(data, pretty=pretty)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    @api.tool(listener=False)
    def gdrive_search(
        query: str,
        folder: str = "",
        kind: str = "any",
        page_size: int = 25,
        page_token: str = "",
        pretty: bool = False,
    ) -> str:
        """Search Google Drive by filename (kind: any|model|texture|audio|archive|folder).

        OAuth mode searches the whole Drive (optionally scoped to folder); API-key
        mode requires folder=<public folder link>. Kind filtering happens on the
        returned page, so a page can come back empty while next_page_token remains.
        """
        try:
            if not str(query or "").strip():
                raise ValueError("query is required — e.g. gdrive_search(query=\"barrel\")")
            data = list_drive(folder, query=str(query), page_size=page_size, page_token=page_token)
            want = str(kind or "any").strip().lower()
            if want not in ("", "any"):
                before = len(data["files"])
                data["files"] = [r for r in data["files"] if r.get("kind") == want]
                data["filtered_out"] = before - len(data["files"])
            data["ok"] = True
            return _tool_json(data, pretty=pretty)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    @api.tool(listener=False)
    def gdrive_file_info(file: str, pretty: bool = False) -> str:
        """Metadata for one Drive file (name, size, type, importability) before downloading.

        file: share link or id. Uses OAuth/API key when available, else a public probe.
        """
        try:
            file_id = extract_drive_id(str(file or ""))
            meta, mode, errors = _fetch_metadata(file_id)
            if not meta:
                meta = _public_probe(file_id)
                mode = "public" if meta else ""
            if not meta:
                detail = "; ".join(f"{m}: {e}" for m, e in errors.items()) or (
                    "file is not public and no OAuth/API-key access is configured"
                )
                raise ValueError(f"No metadata available: {detail}")
            name = str(meta.get("name") or "")
            mime = str(meta.get("mimeType") or "")
            try:
                size = int(meta.get("size")) if meta.get("size") is not None else None
            except (TypeError, ValueError):
                size = None
            ext = Path(name).suffix.lower()
            payload = {
                "ok": True,
                "id": str(meta.get("id") or file_id),
                "name": name,
                "kind": classify_name(name, mime),
                "mime": mime,
                "size": _human_size(size),
                "size_bytes": size,
                "modified": str(meta.get("modifiedTime") or ""),
                "mode": mode,
                "allowed": (ext in ALLOWED_EXTS) if name else None,
                "importable_to_uefn": (ext in IMPORTABLE_EXTS) if name else None,
                "within_size_cap": (size <= _max_download_bytes()) if size is not None else None,
            }
            if mime == FOLDER_MIME:
                payload["hint"] = "This is a folder — use gdrive_list to browse it."
            return _tool_json(payload, pretty=pretty)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    @api.tool(listener=False)
    def gdrive_download(
        file: str,
        filename: str = "",
        subfolder: str = "",
        overwrite: bool = False,
        extract: bool = True,
        pretty: bool = False,
    ) -> str:
        """Download a Drive file into the local staging folder (models/textures/audio/zip only).

        file: share link or id. filename: override when Google hides the name (private
        file without OAuth). Zips are safely extracted next to the download (extract=false
        to skip). Returns local paths — feed them to import_asset, or use
        gdrive_import_to_uefn to do both steps at once. Nothing is ever executed.
        """
        try:
            result = download_to_staging(
                str(file or ""),
                filename=str(filename or ""),
                subfolder=str(subfolder or ""),
                overwrite=bool(overwrite),
                extract_archives=bool(extract),
            )
            result["ok"] = True
            importable = [
                f["path"] for f in result.get("extracted_files") or [] if f.get("importable")
            ]
            if result.get("kind") in ("model", "texture") or Path(str(result["path"])).suffix.lower() in IMPORTABLE_EXTS:
                importable.insert(0, str(result["path"]))
            result["message"] = (
                f"Saved to {result['path']}. "
                + (
                    f"{len(importable)} importable file(s) — import with import_asset or gdrive_import_to_uefn."
                    if importable
                    else "No directly importable files (supporting files only)."
                )
            )
            return _tool_json(result, pretty=pretty)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    @api.tool()
    def gdrive_import_to_uefn(
        file: str = "",
        local_path: str = "",
        destination_path: str = "",
        replace_existing: bool = True,
        pretty: bool = False,
    ) -> str:
        """Download a Drive file AND import it into UEFN as a real asset, in one call.

        Pass file=<Drive link/id> (downloads first) or local_path=<already-downloaded
        file>. Models (.fbx/.glb/.gltf/.obj), textures, and .wav are imported into
        destination_path (project content path e.g. /MyProject/Models, or relative
        e.g. Imported/Props — listener pins to content_root) via the editor's import
        pipeline; zips are extracted and every importable file inside is imported
        (textures first, so materials resolve). Requires UEFN online with the Ducky
        listener. Afterwards verify with validate_uefn_asset and see the modeling
        skill for collision/LODs/placement.
        """
        try:
            dest = str(destination_path or "").strip() or "Imported"
            if not re.match(r"^/?[A-Za-z0-9_][A-Za-z0-9_\-/ .]*$", dest):
                raise ValueError(
                    f"destination_path {dest!r} is not a valid content path — "
                    "use e.g. /MyProject/Models/Props or Models/Props "
                    "(listener pins relative paths to content_root)"
                )
            files: list[Path] = []
            download_info: dict[str, Any] | None = None
            if str(local_path or "").strip():
                p = Path(str(local_path).strip())
                if not p.is_file():
                    raise ValueError(f"local_path not found: {p}")
                if p.suffix.lower() not in ALLOWED_EXTS:
                    raise ValueError(f"{p.name}: type not allowed for import")
                if p.suffix.lower() == ".zip":
                    info = safe_extract_zip(
                        p, _unique_path(p.parent / p.stem), max_total_bytes=2 * _max_download_bytes()
                    )
                    files = list(info["files"])
                else:
                    files = [p]
            elif str(file or "").strip():
                download_info = download_to_staging(str(file), extract_archives=True)
                main = Path(str(download_info["path"]))
                if main.suffix.lower() != ".zip":
                    files = [main]
                files += [Path(str(f["path"])) for f in download_info.get("extracted_files") or []]
            else:
                raise ValueError("Pass file=<Drive link/id> or local_path=<downloaded file>")

            importable = [f for f in files if f.suffix.lower() in IMPORTABLE_EXTS]
            if not importable:
                raise ValueError(
                    "Nothing importable found (need .fbx/.glb/.gltf/.obj, a texture, or .wav). "
                    "Downloaded files are still in the staging folder."
                )
            if len(importable) > IMPORT_MAX_FILES:
                raise ValueError(
                    f"{len(importable)} importable files — over the {IMPORT_MAX_FILES}-per-call "
                    "cap. Import a subset via local_path instead."
                )
            # Textures before models so imported materials can find their maps.
            order = {"texture": 0, "audio": 1, "model": 2}
            importable.sort(key=lambda p: (order.get(classify_name(p.name), 3), p.name.lower()))

            results: list[dict[str, Any]] = []
            imported = 0
            for path in importable:
                try:
                    res = api.listener(
                        "import_asset",
                        {
                            "source_file": str(path),
                            "destination_path": dest,
                            "replace_existing": bool(replace_existing),
                        },
                        timeout=300.0,
                    )
                    ok = not (isinstance(res, dict) and res.get("error"))
                    imported += 1 if ok else 0
                    results.append({"file": path.name, "ok": ok, "result": res})
                except Exception as exc:  # noqa: BLE001 — keep going per file
                    results.append({"file": path.name, "ok": False, "error": str(exc)})
                    msg = str(exc).lower()
                    if "listener" in msg or "connect" in msg or "refused" in msg or "offline" in msg:
                        results.append(
                            {
                                "file": "(aborted)",
                                "ok": False,
                                "error": "UEFN listener appears offline — open the project in UEFN and retry",
                            }
                        )
                        break
            payload: dict[str, Any] = {
                "ok": imported > 0,
                "imported": imported,
                "attempted": len(importable),
                "destination_path": dest,
                "results": results,
                "message": (
                    f"Imported {imported}/{len(importable)} file(s) into {dest}. Verify with "
                    "validate_uefn_asset, preview with preview_asset, then save_directory. "
                    "See the modeling skill for collision/LODs/placement."
                    if imported
                    else "No files imported — see per-file errors."
                ),
            }
            if download_info:
                payload["downloaded"] = {
                    "path": download_info["path"],
                    "size": download_info["size"],
                    "mode": download_info["mode"],
                }
            return _tool_json(payload, pretty=pretty)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    @api.register_pipeline_node("googledrive.list")
    def node_list(ctx: dict) -> dict:
        cfg = {**(ctx.get("config") or {}), **(ctx.get("inputs") or {})}
        try:
            data = list_drive(str(cfg.get("folder") or ""), query=str(cfg.get("query") or ""))
            return {"ok": True, "files": data["files"], "count": len(data["files"])}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}

    @api.register_pipeline_node("googledrive.download")
    def node_download(ctx: dict) -> dict:
        cfg = {**(ctx.get("config") or {}), **(ctx.get("inputs") or {})}
        try:
            if not str(cfg.get("file") or "").strip():
                raise ValueError("Pass a Drive share link or file id in 'file'")
            result = download_to_staging(str(cfg["file"]), extract_archives=True)
            return {"ok": True, "path": result["path"], "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}

    display.register(api)

    @api.tool(listener=False)
    def google_open_pending(pretty: bool = False) -> str:
        """Take the last google_open request (view, item, search) once, as the Google tab does on open."""
        return _tool_json({"ok": True, "request": display.take_pending_open()}, pretty=pretty)

    api.log("Google Drive tools registered")
