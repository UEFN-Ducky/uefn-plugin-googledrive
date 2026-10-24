"""Google Drive for the googledrive plugin: ids, safe downloads, zip extraction, listing.

Three access modes, auto-picked per request (most capable first):
  1. OAuth — private files + full-Drive search (sign-in lives in ``auth``).
  2. API key — browse/download files and folders shared "anyone with link".
  3. No credentials — public share links still download.

Safety model (deliberate — keep it this way):
  - User input is reduced to a validated Drive file id — raw URLs are never
    fetched; every request goes through ``auth._open`` (Google hosts only).
  - Downloads: extension allowlist (models/textures/audio/zip), size caps,
    sanitized filenames, streamed to a temp file then atomically moved into a
    dedicated staging folder under AppData. Nothing is ever executed.
  - Zips: safe extraction only — zip-slip guard, entry/total-size caps, the
    same extension allowlist, encrypted entries skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from . import auth
from .auth import KEY_API, PLUGIN_ID, log

API_BASE = "https://www.googleapis.com/drive/v3"
PUBLIC_DOWNLOAD_URL = "https://drive.usercontent.google.com/download"

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps"

DEFAULT_MAX_DOWNLOAD_MB = 512
ZIP_MAX_ENTRIES = 400
ZIP_MAX_DEPTH = 6
IMPORT_MAX_FILES = 40
CHUNK = 1024 * 1024

MODEL_EXTS = {".fbx", ".glb", ".gltf", ".obj"}
MODEL_SUPPORT_EXTS = {".bin", ".mtl"}  # ride along with gltf/obj; never imported alone
TEXTURE_EXTS = {".png", ".jpg", ".jpeg", ".tga", ".psd", ".tif", ".tiff", ".exr", ".hdr", ".bmp"}
AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".flac"}
ARCHIVE_EXTS = {".zip"}
ALLOWED_EXTS = MODEL_EXTS | MODEL_SUPPORT_EXTS | TEXTURE_EXTS | AUDIO_EXTS | ARCHIVE_EXTS
# What we hand to UEFN's import pipeline (wav is the only audio UEFN takes).
IMPORTABLE_EXTS = MODEL_EXTS | TEXTURE_EXTS | {".wav"}


# --------------------------------------------------------------------------- #
# Pure helpers (no app imports, no network) — covered by scripts/selfcheck.py.
# --------------------------------------------------------------------------- #

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_URL_ID_PATTERNS = (
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,128})"),
    re.compile(r"/folders/([A-Za-z0-9_-]{10,128})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,128})"),
)


def extract_drive_id(text: str) -> str:
    """Reduce a share link or bare id to a validated Drive id. Raises ValueError."""
    t = (text or "").strip().strip("<>\"'")
    if not t:
        raise ValueError("Empty file reference — pass a Drive share link or file id")
    if _ID_RE.match(t):
        return t
    parsed = urllib.parse.urlparse(t)
    host = (parsed.netloc or "").lower()
    if host and host not in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"):
        raise ValueError(
            f"Not a Google Drive link (host {host!r}) — only drive.google.com links are accepted"
        )
    if host == "docs.google.com" and any(
        seg in parsed.path for seg in ("/document/", "/spreadsheets/", "/presentation/", "/forms/")
    ):
        raise ValueError(
            "That is a Google Docs/Sheets/Slides link, not an asset file — export it "
            "from Google first if you need its contents"
        )
    for pat in _URL_ID_PATTERNS:
        m = pat.search(t)
        if m:
            return m.group(1)
    raise ValueError("Could not find a Drive file id in that link")


_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)
}


def _sanitize_component(name: str, *, max_len: int = 80) -> str:
    """One path segment: strip separators/control chars, dodge reserved names."""
    seg = _BAD_CHARS_RE.sub("_", (name or "").strip()).strip(" .")
    if not seg or seg in (".", ".."):
        return "_"
    if seg.split(".")[0].lower() in _WINDOWS_RESERVED:
        seg = "_" + seg
    return seg[:max_len]


def sanitize_filename(name: str, *, require_allowed_ext: bool = True) -> str:
    """Safe basename for the staging dir. Raises ValueError on empty/blocked type."""
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    base = _BAD_CHARS_RE.sub("_", base).strip(" .")
    if not base:
        raise ValueError("Empty filename")
    ext = Path(base).suffix.lower()
    if require_allowed_ext and ext not in ALLOWED_EXTS:
        allowed = ", ".join(sorted(ALLOWED_EXTS))
        raise ValueError(
            f"File type {ext or '(no extension)'} is not allowed — this plugin only "
            f"handles asset files: {allowed}"
        )
    stem = base[: -len(ext)] if ext else base
    if stem.lower() in _WINDOWS_RESERVED:
        stem = "_" + stem
    stem = stem[:120] or "file"
    return stem + ext


def classify_name(name: str, mime: str = "") -> str:
    if mime == FOLDER_MIME:
        return "folder"
    ext = Path(str(name or "")).suffix.lower()
    if ext in MODEL_EXTS:
        return "model"
    if ext in TEXTURE_EXTS:
        return "texture"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in MODEL_SUPPORT_EXTS:
        return "support"
    return "other"


def _human_size(n: int | None) -> str:
    if not n or n < 0:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{int(n)} B"


def _unique_path(path: Path) -> Path:
    """path, or path-2 / path-3 … if it already exists."""
    if not path.exists():
        return path
    for i in range(2, 100):
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise ValueError(f"Too many files named like {path.name} in the staging folder")


_FORM_ACTION_RE = re.compile(r'<form[^>]+action="([^"]+)"', re.I)
_FORM_INPUT_RE = re.compile(r'<input[^>]+name="([^"]+)"[^>]*value="([^"]*)"', re.I)


def parse_public_confirm_form(page: str) -> dict[str, str] | None:
    """Params from Google's 'can't scan for viruses' interstitial, else None."""
    text = page or ""
    if "download-form" not in text and "uc-download-link" not in text:
        return None
    import html as html_mod

    action = ""
    m = _FORM_ACTION_RE.search(text)
    if m:
        action = html_mod.unescape(m.group(1))
        host = urllib.parse.urlparse(action).netloc.lower()
        if host and host != "drive.usercontent.google.com":
            return None
    params = {html_mod.unescape(k): html_mod.unescape(v) for k, v in _FORM_INPUT_RE.findall(text)}
    if not params.get("id"):
        return None
    params.setdefault("export", "download")
    params.setdefault("confirm", "t")
    return params


_CD_UTF8_RE = re.compile(r"filename\*\s*=\s*UTF-8''([^;]+)", re.I)
_CD_PLAIN_RE = re.compile(r'filename\s*=\s*"([^"]+)"', re.I)


def parse_content_disposition(value: str) -> str:
    v = value or ""
    m = _CD_UTF8_RE.search(v)
    if m:
        try:
            return urllib.parse.unquote(m.group(1).strip())
        except (ValueError, TypeError):
            pass
    m = _CD_PLAIN_RE.search(v)
    return m.group(1).strip() if m else ""


_CR_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)", re.I)


def parse_content_range_total(value: str) -> int | None:
    m = _CR_RE.search(value or "")
    return int(m.group(1)) if m else None


def safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    max_total_bytes: int,
    max_entries: int = ZIP_MAX_ENTRIES,
) -> dict[str, Any]:
    """Extract only allowlisted files, guarding zip-slip / zip bombs.

    Returns {files: [Path], skipped: [(name, reason)], total_bytes}. Raises
    ValueError on structural problems (too many entries, over the size cap).
    """
    extracted: list[Path] = []
    skipped: list[tuple[str, str]] = []
    total = 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise ValueError(f"Zip has {len(infos)} entries (max {max_entries}) — refusing to extract")
        for info in infos:
            if info.is_dir():
                continue
            raw = info.filename.replace("\\", "/")
            if info.flag_bits & 0x1:
                skipped.append((raw, "encrypted entry"))
                continue
            parts = [p for p in raw.split("/") if p not in ("", ".", "..")]
            if not parts:
                skipped.append((raw, "empty path"))
                continue
            if len(parts) > ZIP_MAX_DEPTH:
                parts = parts[-ZIP_MAX_DEPTH:]
            try:
                fname = sanitize_filename(parts[-1])
            except ValueError:
                skipped.append((raw, "file type not allowed"))
                continue
            if fname.lower().endswith(".zip"):
                skipped.append((raw, "nested zip"))
                continue
            safe_parents = [_sanitize_component(p) for p in parts[:-1]]
            out = dest_dir.joinpath(*safe_parents, fname)
            try:
                out_resolved = out.resolve()
            except OSError:
                skipped.append((raw, "unresolvable path"))
                continue
            if not (out_resolved == dest_resolved or out_resolved.is_relative_to(dest_resolved)):
                skipped.append((raw, "path escapes extraction dir"))
                continue
            if total + max(int(info.file_size or 0), 0) > max_total_bytes:
                raise ValueError(
                    f"Zip contents exceed the size cap ({_human_size(max_total_bytes)}) — "
                    "raise 'Max download size' in Settings → Google Drive if intended"
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            copied = 0
            with zf.open(info) as src, open(out, "wb") as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    copied += len(chunk)
                    # Trust the declared size only so far — a lying header is a bomb.
                    if copied > int(info.file_size or 0) + 4096 or total + copied > max_total_bytes:
                        dst.close()
                        out.unlink(missing_ok=True)
                        raise ValueError("Zip entry larger than declared — aborting (zip bomb?)")
                    dst.write(chunk)
            total += copied
            extracted.append(out)
    return {"files": extracted, "skipped": skipped, "total_bytes": total}


# --------------------------------------------------------------------------- #
# Settings + staging folder (lazy app imports so this file stays runnable standalone).
# --------------------------------------------------------------------------- #


def _prefs() -> dict[str, Any]:
    try:
        from frontend.ui_web.plugin_host_api import prefs_plugin_get

        prefs = prefs_plugin_get(PLUGIN_ID)
        return prefs if isinstance(prefs, dict) else {}
    except Exception:  # noqa: BLE001 — prefs are best-effort
        return {}


def _max_download_bytes() -> int:
    try:
        mb = int(str(_prefs().get("max_download_mb") or "").strip() or DEFAULT_MAX_DOWNLOAD_MB)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_DOWNLOAD_MB
    return max(1, min(mb, 4096)) * 1024 * 1024


def _target_folder_ref() -> str:
    """Raw 'your Drive folder' setting (a Drive folder link or id), or empty."""
    return str(_prefs().get("target_folder") or "").strip()


def _target_folder_id() -> str:
    """Validated id of the configured target folder, or empty if unset/invalid."""
    ref = _target_folder_ref()
    if not ref:
        return ""
    try:
        return extract_drive_id(ref)
    except ValueError:
        return ""


def _staging_dir() -> Path:
    from backend.skills.store import appdata_dir

    path = appdata_dir() / "downloads" / "googledrive"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Drive API — metadata, listing, download.
# --------------------------------------------------------------------------- #


def _auth_modes() -> list[str]:
    modes: list[str] = []
    if auth._load_token():
        modes.append("oauth")
    if auth._get_secret(KEY_API):
        modes.append("api_key")
    modes.append("public")
    return modes


def _api_get_json(path_and_query: str, *, mode: str, timeout: float = 30.0) -> dict[str, Any]:
    url = f"{API_BASE}/{path_and_query.lstrip('/')}"
    headers: dict[str, str] = {}
    if mode == "oauth":
        token = auth._access_token()
        if not token:
            raise ValueError("Not connected to Google Drive — run gdrive_connect")
        headers["Authorization"] = f"Bearer {token}"
    elif mode == "api_key":
        key = auth._get_secret(KEY_API)
        if not key:
            raise ValueError("No Google API key set (Settings → Google Drive)")
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={urllib.parse.quote(key)}"
    else:
        raise ValueError(f"Metadata requires OAuth or an API key (mode {mode!r})")
    with auth._open(url, headers=headers, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Unexpected response from Google Drive")
    return data


_META_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails"


def _fetch_metadata(file_id: str) -> tuple[dict[str, Any] | None, str, dict[str, str]]:
    """(metadata, mode_used, errors_by_mode). Resolves Drive shortcuts one hop."""
    errors: dict[str, str] = {}
    for mode in _auth_modes():
        if mode == "public":
            continue
        try:
            meta = _api_get_json(
                f"files/{urllib.parse.quote(file_id)}?fields={urllib.parse.quote(_META_FIELDS)}"
                "&supportsAllDrives=true",
                mode=mode,
            )
            if meta.get("mimeType") == SHORTCUT_MIME:
                target = str(((meta.get("shortcutDetails") or {}).get("targetId")) or "")
                if target and _ID_RE.match(target):
                    meta = _api_get_json(
                        f"files/{urllib.parse.quote(target)}?fields={urllib.parse.quote(_META_FIELDS)}"
                        "&supportsAllDrives=true",
                        mode=mode,
                    )
            return meta, mode, errors
        except urllib.error.HTTPError as exc:
            errors[mode] = f"HTTP {exc.code}: {auth._http_error_detail(exc)}"
        except (urllib.error.URLError, ValueError) as exc:
            errors[mode] = str(exc)
    return None, "", errors


def _public_probe(file_id: str) -> dict[str, Any] | None:
    """Name/size/mime of a public file via a 1-byte ranged download. None if blocked."""
    params = {"id": file_id, "export": "download"}
    for _attempt in range(2):
        url = PUBLIC_DOWNLOAD_URL + "?" + urllib.parse.urlencode(params)
        try:
            with auth._open(url, headers={"Range": "bytes=0-0"}, timeout=30) as resp:
                ctype = str(resp.headers.get("Content-Type") or "")
                if "text/html" in ctype.lower():
                    page = resp.read(512 * 1024).decode("utf-8", "replace")
                    form = parse_public_confirm_form(page)
                    if not form:
                        return None
                    params = form
                    continue
                name = parse_content_disposition(str(resp.headers.get("Content-Disposition") or ""))
                size = parse_content_range_total(str(resp.headers.get("Content-Range") or ""))
                if size is None:
                    try:
                        size = int(resp.headers.get("Content-Length") or 0) or None
                    except (TypeError, ValueError):
                        size = None
                return {"name": name, "size": size, "mimeType": ctype.split(";")[0].strip()}
        except (urllib.error.URLError, ValueError):
            return None
    return None


def _stream_to_file(resp: Any, tmp_path: Path, cap_bytes: int, md5_expect: str = "") -> dict[str, Any]:
    length = resp.headers.get("Content-Length")
    if length:
        try:
            if int(length) > cap_bytes:
                raise ValueError(
                    f"File is {_human_size(int(length))} — over the "
                    f"{_human_size(cap_bytes)} cap (Settings → Google Drive → Max download size)"
                )
        except (TypeError, ValueError):
            pass
    sha = hashlib.sha256()
    md5 = hashlib.md5() if md5_expect else None
    total = 0
    with open(tmp_path, "wb") as out:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > cap_bytes:
                raise ValueError(
                    f"Download passed the {_human_size(cap_bytes)} cap — aborted "
                    "(raise 'Max download size' in Settings → Google Drive if intended)"
                )
            out.write(chunk)
            sha.update(chunk)
            if md5 is not None:
                md5.update(chunk)
    if md5 is not None and md5.hexdigest() != md5_expect.lower():
        raise ValueError("Checksum mismatch after download — the file is corrupted, try again")
    return {"bytes": total, "sha256": sha.hexdigest()}


def _download_stream(file_id: str, mode: str, tmp_path: Path, cap: int, md5_expect: str) -> dict[str, Any]:
    if mode == "oauth":
        token = auth._access_token()
        if not token:
            raise ValueError("Not connected — run gdrive_connect")
        url = f"{API_BASE}/files/{urllib.parse.quote(file_id)}?alt=media&supportsAllDrives=true"
        with auth._open(url, headers={"Authorization": f"Bearer {token}"}, timeout=600) as resp:
            return _stream_to_file(resp, tmp_path, cap, md5_expect)
    if mode == "api_key":
        key = auth._get_secret(KEY_API)
        if not key:
            raise ValueError("No API key set")
        url = (
            f"{API_BASE}/files/{urllib.parse.quote(file_id)}?alt=media"
            f"&key={urllib.parse.quote(key)}"
        )
        with auth._open(url, timeout=600) as resp:
            return _stream_to_file(resp, tmp_path, cap, md5_expect)
    # public
    params: dict[str, str] = {"id": file_id, "export": "download"}
    for _attempt in range(2):
        url = PUBLIC_DOWNLOAD_URL + "?" + urllib.parse.urlencode(params)
        with auth._open(url, timeout=600) as resp:
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                page = resp.read(512 * 1024).decode("utf-8", "replace")
                form = parse_public_confirm_form(page)
                if not form:
                    raise ValueError(
                        "Google would not serve the file anonymously — it is probably not "
                        "shared 'anyone with link'. Connect with gdrive_connect or share the file."
                    )
                params = form
                continue
            return _stream_to_file(resp, tmp_path, cap, md5_expect)
    raise ValueError("Google kept returning an interstitial page — could not download")


def _reject_non_binary_meta(meta: dict[str, Any]) -> None:
    mime = str(meta.get("mimeType") or "")
    if mime == FOLDER_MIME:
        raise ValueError("That link is a folder — use gdrive_list to see its files, then pick one")
    if mime.startswith(GOOGLE_APPS_PREFIX):
        raise ValueError(
            f"That is a Google-native document ({mime}) — not a downloadable asset file"
        )


def download_to_staging(
    file_ref: str,
    *,
    filename: str = "",
    subfolder: str = "",
    overwrite: bool = False,
    extract_archives: bool = True,
) -> dict[str, Any]:
    """Download one Drive file into the staging dir (extracting zips safely)."""
    file_id = extract_drive_id(file_ref)
    cap = _max_download_bytes()
    meta, meta_mode, meta_errors = _fetch_metadata(file_id)
    if meta:
        _reject_non_binary_meta(meta)
        file_id = str(meta.get("id") or file_id)
    else:
        probe = _public_probe(file_id)
        if probe:
            meta = probe
    known_size = None
    try:
        known_size = int(meta.get("size")) if meta and meta.get("size") is not None else None
    except (TypeError, ValueError):
        known_size = None
    if known_size is not None and known_size > cap:
        raise ValueError(
            f"File is {_human_size(known_size)} — over the {_human_size(cap)} cap "
            "(Settings → Google Drive → Max download size)"
        )
    raw_name = (filename or "").strip() or str((meta or {}).get("name") or "")
    if not raw_name:
        raise ValueError(
            "Could not determine the filename (file is private or metadata unavailable). "
            "Pass filename=\"model.fbx\" explicitly, or connect with gdrive_connect."
        )
    safe_name = sanitize_filename(raw_name)
    dest_dir = _staging_dir()
    for part in (subfolder or "").replace("\\", "/").split("/"):
        if part.strip():
            dest_dir = dest_dir / _sanitize_component(part)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    if dest.exists() and not overwrite:
        dest = _unique_path(dest)

    md5_expect = str((meta or {}).get("md5Checksum") or "")
    tmp = dest.with_suffix(dest.suffix + ".part")
    errors: dict[str, str] = dict(meta_errors)
    got: dict[str, Any] | None = None
    used_mode = ""
    try:
        for mode in _auth_modes():
            try:
                got = _download_stream(file_id, mode, tmp, cap, md5_expect)
                used_mode = mode
                break
            except urllib.error.HTTPError as exc:
                errors[mode] = f"HTTP {exc.code}: {auth._http_error_detail(exc)}"
            except urllib.error.URLError as exc:
                errors[mode] = f"network: {exc.reason}"
            except ValueError as exc:
                errors[mode] = str(exc)
        if got is None:
            detail = "; ".join(f"{m}: {e}" for m, e in errors.items()) or "no access mode available"
            raise ValueError(f"Could not download {file_id}: {detail}")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)

    log.info("gdrive: downloaded %s -> %s (%s, %s)", file_id, dest.name, _human_size(got["bytes"]), used_mode)
    result: dict[str, Any] = {
        "file_id": file_id,
        "path": str(dest),
        "bytes": got["bytes"],
        "size": _human_size(got["bytes"]),
        "sha256": got["sha256"],
        "mode": used_mode,
        "kind": classify_name(dest.name),
        "staging_dir": str(_staging_dir()),
    }
    if dest.suffix.lower() == ".zip" and extract_archives:
        extract_dir = _unique_path(dest_dir / dest.stem)
        try:
            info = safe_extract_zip(dest, extract_dir, max_total_bytes=2 * cap)
        except (ValueError, zipfile.BadZipFile) as exc:
            shutil.rmtree(extract_dir, ignore_errors=True)
            result["extract_error"] = str(exc)
            return result
        result["extracted_dir"] = str(extract_dir)
        result["extracted_files"] = [
            {"path": str(p), "kind": classify_name(p.name), "importable": p.suffix.lower() in IMPORTABLE_EXTS}
            for p in info["files"]
        ]
        if info["skipped"]:
            result["extract_skipped"] = [f"{n} ({why})" for n, why in info["skipped"][:20]]
    return result


def _q_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _rows_from_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "")
        mime = str(f.get("mimeType") or "")
        try:
            size = int(f.get("size")) if f.get("size") is not None else None
        except (TypeError, ValueError):
            size = None
        kind = classify_name(name, mime)
        rows.append(
            {
                "id": str(f.get("id") or ""),
                "name": name,
                "kind": kind,
                "mime": mime,
                "size": _human_size(size),
                "size_bytes": size,
                "modified": str(f.get("modifiedTime") or ""),
                "importable": kind in ("model", "texture") or Path(name).suffix.lower() in IMPORTABLE_EXTS,
                "downloadable": kind == "folder" or Path(name).suffix.lower() in ALLOWED_EXTS,
            }
        )
    return rows


def list_drive(
    folder: str = "",
    *,
    query: str = "",
    page_size: int = 50,
    page_token: str = "",
) -> dict[str, Any]:
    modes = [m for m in _auth_modes() if m != "public"]
    if not modes:
        raise ValueError(
            "Browsing needs an API key (public folders) or gdrive_connect (your Drive). "
            "Without either you can still download a direct share link via gdrive_download."
        )
    # No explicit folder → fall back to the user's configured "your Drive folder".
    folder_id = extract_drive_id(folder) if folder.strip() else _target_folder_id()
    q_parts = ["trashed = false"]
    if folder_id:
        q_parts.append(f"'{_q_escape(folder_id)}' in parents")
    elif modes[0] == "api_key" and not query.strip():
        raise ValueError(
            "API-key mode can only list inside a specific public folder — pass "
            "folder=<link or id>, or set 'Your Drive folder' in Settings → Google Drive"
        )
    if query.strip():
        q_parts.append(f"name contains '{_q_escape(query.strip())}'")
    if not folder_id and modes[0] == "oauth" and not query.strip():
        q_parts.append("'root' in parents")
    params = {
        "q": " and ".join(q_parts),
        "pageSize": str(max(1, min(int(page_size or 50), 100))),
        "fields": f"nextPageToken,files({_META_FIELDS})",
        "orderBy": "folder,name",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if page_token.strip():
        params["pageToken"] = page_token.strip()
    errors: dict[str, str] = {}
    for mode in modes:
        try:
            data = _api_get_json("files?" + urllib.parse.urlencode(params), mode=mode)
            return {
                "files": _rows_from_files(data.get("files") or []),
                "next_page_token": str(data.get("nextPageToken") or ""),
                "mode": mode,
                "folder_id": folder_id or ("root" if mode == "oauth" else ""),
            }
        except urllib.error.HTTPError as exc:
            errors[mode] = f"HTTP {exc.code}: {auth._http_error_detail(exc)}"
        except (urllib.error.URLError, ValueError) as exc:
            errors[mode] = str(exc)
    detail = "; ".join(f"{m}: {e}" for m, e in errors.items())
    raise ValueError(f"Drive listing failed: {detail}")
