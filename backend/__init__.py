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

from . import auth, display, mail_store, nodes, poller, tools_account, tools_drive, tools_gmail
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
    preview_image,
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


_POLLER: Any = None


def _drive_status() -> dict[str, Any]:
    """Drive details merged into google_status / gdrive_status (tools_account)."""
    target_ref = _target_folder_ref()
    target_id = _target_folder_id()
    api_key_set = bool(auth._get_secret(KEY_API))
    out: dict[str, Any] = {
        "sign_in_kind": (
            "bundled" if _using_bundled_client() else "custom" if auth._uses_own_client() else "none"
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
    if target_ref and not target_id:
        out["drive_hint"] = f"'Your Drive folder' setting isn't a valid Drive folder link/id: {target_ref!r}"
    elif not target_ref:
        out["drive_hint"] = (
            "Tip: set 'Your Drive folder' in Settings so gdrive_list / gdrive_search default "
            "to one folder you drop assets into."
        )
    return out


def allowed_models(api: Any) -> dict[str, Any]:
    """The models Settings allows for this plugin (Draft with AI / Ask Ducky use only these).

    Read from the host api when it offers ``allowed_models``, else from the app's model
    gate; a host without either answers ``{models: [], reason}``.
    """
    fn = getattr(api, "allowed_models", None)
    try:
        if callable(fn):
            return {"ok": True, "models": [str(m) for m in fn(PLUGIN_ID) or []]}
        from backend.uefn_plugins.model_gate import allowed_models as gate_models
    except ImportError:
        return {"ok": True, "models": [], "reason": "host has no model gate"}
    return {"ok": True, "models": [str(m) for m in gate_models(PLUGIN_ID) or []]}


def _poll_seconds() -> Any:
    from .drive import _prefs

    return _prefs().get("gmail_poll_seconds") or poller.DEFAULT_INTERVAL_S


def shutdown() -> None:
    """Stop the new-mail poller (plugin unload, reinstall, tests)."""
    global _POLLER
    if _POLLER is not None:
        _POLLER.stop()
        _POLLER = None


def register(api: Any) -> None:
    """Wire the Google Workspace tools, panel calls, nodes and the new-mail poller."""
    global _POLLER
    auth.bind(api)

    def _fail(exc: Exception, pretty: bool) -> str:
        return _tool_json({"ok": False, "error": str(exc) or exc.__class__.__name__}, pretty=pretty)

    @api.tool(listener=False)
    def gdrive_list(
        folder: str = "",
        page_size: int = 50,
        page_token: str = "",
        account: str = "",
        pretty: bool = False,
    ) -> str:
        """List a Google Drive folder (models/textures/audio flagged as importable).

        folder: a Drive folder link/id or "root". Empty → the user's configured "Your
        Drive folder" (Settings → Google Drive) if set, else My Drive root (signed-in
        only). API-key mode can only list public folders. account: which connected
        Google account (email); empty = the first one with Drive. Returns
        id/name/kind/size rows — pass a row's id to gdrive_download or gdrive_import_to_uefn.
        """
        try:
            data = list_drive(str(folder or ""), page_size=page_size, page_token=page_token, account=str(account or ""))
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
        account: str = "",
        pretty: bool = False,
    ) -> str:
        """Search Google Drive by filename (kind: any|model|texture|audio|archive|folder).

        OAuth mode searches the whole Drive (optionally scoped to folder); API-key
        mode requires folder=<public folder link>. account: which connected Google
        account (email). Kind filtering happens on the returned page, so a page can
        come back empty while next_page_token remains.
        """
        try:
            if not str(query or "").strip():
                raise ValueError("query is required — e.g. gdrive_search(query=\"barrel\")")
            data = list_drive(
                str(folder or ""), query=str(query), page_size=page_size, page_token=page_token,
                account=str(account or ""),
            )
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
    def gdrive_file_info(file: str, account: str = "", pretty: bool = False) -> str:
        """Metadata for one Drive file (name, size, type, importability) before downloading.

        file: share link or id. account: which connected Google account (email). Uses
        OAuth/API key when available, else a public probe.
        """
        try:
            file_id = extract_drive_id(str(file or ""))
            meta, mode, errors = _fetch_metadata(file_id, str(account or ""))
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
        account: str = "",
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
                account=str(account or ""),
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
        account: str = "",
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
                download_info = download_to_staging(str(file), extract_archives=True, account=str(account or ""))
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

    @api.tool(listener=False)
    def gdrive_preview(file: str, account: str = "", pretty: bool = False) -> str:
        """Preview a Drive picture in the Google tab: returns a local media url for it.

        file: share link or id of a png/jpg/gif/webp/bmp (20 MB max). account: which
        connected Google account (email). Returns {ok, url, name, mime}.
        """
        try:
            got = preview_image(
                str(file or ""), Path(api.local_dir("drive-previews")), account=str(account or "")
            )
            url = api.media_url(got["path"])
            if not url:
                raise ValueError("This picture type can't be shown in the tab")
            return _tool_json({"ok": True, "url": url, "name": got["name"], "mime": got["mime"]}, pretty=pretty)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    # The Drive tab calls the same functions as panel RPCs.
    for _rpc in (gdrive_list, gdrive_search, gdrive_file_info, gdrive_import_to_uefn, gdrive_preview):
        api.register_panel_rpc(_rpc.__name__, _rpc)

    @api.tool(listener=False)
    def google_allowed_models(pretty: bool = False) -> str:
        """Models allowed to read this plugin's mail and files (Settings → Allowed models).

        Returns {ok, models}; an empty list means no model may draft with Gmail data.
        """
        try:
            return _tool_json(allowed_models(api), pretty=pretty)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc, pretty)

    api.register_panel_rpc("google_allowed_models", google_allowed_models)

    display.register(api)

    @api.tool(listener=False)
    def google_open_pending(pretty: bool = False) -> str:
        """Take the last google_open request (view, item, search) once, as the Google tab does on open."""
        return _tool_json({"ok": True, "request": display.take_pending_open()}, pretty=pretty)

    # Accounts (google_* + the gdrive_status/connect/disconnect aliases) over auth.py.
    account_tools = tools_account.register(api, auth=auth, extra_status=_drive_status)

    tools_drive.register(api, accounts=account_tools.rows, token_for=lambda email: auth.access_token(email))

    # Gmail: one client and one mail cache per account, shared by the tools and the poller.
    stores: dict[str, Any] = {}

    def client_for(account: str) -> Any:
        token = auth.access_token_getter(account, scope=auth.SCOPE_GMAIL)
        return tools_gmail.gmail.GmailClient(api.http_json, token)

    def store_for(account: str) -> Any:
        key = account.strip().lower()
        if key not in stores:
            stores[key] = mail_store.MailStore(api, account, client_for(account))
        return stores[key]

    tools_gmail.register(
        api,
        accounts=auth.gmail_accounts,
        client_for=client_for,
        store_for=store_for,
        importer=gdrive_import_to_uefn,
    )
    nodes.register(api)

    shutdown()  # a reinstall registers again: never two pollers
    _POLLER = poller.Poller(api, accounts=auth.gmail_accounts, store_for=store_for, interval=_poll_seconds)
    _POLLER.start()

    api.log("Google Workspace tools registered")
