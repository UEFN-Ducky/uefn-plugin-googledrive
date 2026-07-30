"""Google Drive — UEFN Ducky desktop plugin.

Safely pull 3D models, textures, and audio from Google Drive and import them
into UEFN as real assets via the app's import pipeline (``import_asset``).

Three access modes, auto-picked per request (most capable first):
  1. OAuth — the user's own Google Cloud "Desktop app" client with the
     read-only Drive scope. Private files + full-Drive search. Connected via
     the ``gdrive_connect`` tool (sign-in happens in the user's browser).
  2. API key — browse/download files and folders shared "anyone with link".
  3. No credentials — public share links still download.

Safety model (deliberate — keep it this way):
  - Requests go ONLY to Google hosts (accounts.google.com, oauth2/www
    .googleapis.com, drive[.usercontent].google.com, *.googleusercontent.com
    redirects). User input is reduced to a validated Drive file id — raw URLs
    are never fetched.
  - OAuth uses the read-only scope + PKCE. Tokens live in the app's encrypted
    credential store (never in this package, never logged, never returned).
  - Downloads: extension allowlist (models/textures/audio/zip), size caps,
    sanitized filenames, streamed to a temp file then atomically moved into a
    dedicated staging folder under AppData. Nothing is ever executed.
  - Zips: safe extraction only — zip-slip guard, entry/total-size caps, the
    same extension allowlist, encrypted entries skipped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets as pysecrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger("uefn.plugin.googledrive")

PLUGIN_ID = "googledrive"
KEY_API = "gdrive_api_key"
KEY_CLIENT_ID = "gdrive_oauth_client_id"
KEY_CLIENT_SECRET = "gdrive_oauth_client_secret"
KEY_TOKEN = "gdrive_oauth_token"

OAUTH_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
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

_ALLOWED_HOSTS = {
    "www.googleapis.com",
    "oauth2.googleapis.com",
    "accounts.google.com",
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
}

_CONNECT_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Pure helpers (no app imports, no network) — covered by _self_check().
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


def token_expired(tok: dict[str, Any], now: float | None = None) -> bool:
    """True when the access token is missing or expires within 60 seconds."""
    if not isinstance(tok, dict) or not str(tok.get("access_token") or ""):
        return True
    try:
        expires_at = float(tok.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True
    return (now if now is not None else time.time()) >= expires_at - 60.0


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
# App plumbing (lazy imports so this file stays runnable standalone).
# --------------------------------------------------------------------------- #


def _get_secret(name: str) -> str:
    from backend.agent.secrets import get_key

    return (get_key(name) or "").strip()


def _set_secret(name: str, value: str) -> None:
    from backend.agent.secrets import set_key

    set_key(name, value)


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
    from backend.skill import appdata_dir

    path = appdata_dir() / "downloads" / "googledrive"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tool_json(payload: dict[str, Any], pretty: bool = False) -> str:
    try:
        from backend.json_util import tool_json

        return tool_json(payload, pretty=pretty)
    except Exception:  # noqa: BLE001 — standalone / early-boot fallback
        return json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)


# --------------------------------------------------------------------------- #
# HTTP core — every request is host-checked, including redirects.
# --------------------------------------------------------------------------- #


def _host_ok(host: str) -> bool:
    h = (host or "").lower().split(":")[0]
    return h in _ALLOWED_HOSTS or h.endswith(".googleusercontent.com")


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not _host_ok(parsed.netloc):
        raise ValueError(f"Blocked non-Google URL: {parsed.scheme}://{parsed.netloc}")


class _GoogleRedirectGuard(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GoogleRedirectGuard())


def _open(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
    timeout: float = 60.0,
):
    _check_url(url)
    all_headers = {"User-Agent": "UEFN-Ducky-GoogleDrive/1"}
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    return _OPENER.open(req, timeout=timeout)


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        body = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or body)[:300]
            if isinstance(err, str):
                desc = str(parsed.get("error_description") or "")
                return f"{err}: {desc}"[:300] if desc else err[:300]
    except (ValueError, TypeError):
        pass
    return body[:300]


# --------------------------------------------------------------------------- #
# OAuth (user's own Desktop-app client, read-only scope, PKCE).
# --------------------------------------------------------------------------- #


def _oauth_client() -> tuple[str, str]:
    """The user's own OAuth client (client_id, client_secret) from Settings."""
    return _get_secret(KEY_CLIENT_ID).strip(), _get_secret(KEY_CLIENT_SECRET).strip()


def _load_token() -> dict[str, Any] | None:
    raw = _get_secret(KEY_TOKEN)
    if not raw:
        return None
    try:
        tok = json.loads(raw)
        return tok if isinstance(tok, dict) else None
    except (ValueError, TypeError):
        return None


def _save_token(tok: dict[str, Any]) -> None:
    _set_secret(KEY_TOKEN, json.dumps(tok))


def _clear_token() -> None:
    _set_secret(KEY_TOKEN, "")


def _token_post(form: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    try:
        with _open(
            TOKEN_URL,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if "invalid_grant" in detail:
            raise ValueError(
                "Google rejected the saved sign-in (invalid_grant) — it was revoked or "
                "expired. Run gdrive_connect again."
            ) from exc
        raise ValueError(f"Google token endpoint error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Network error reaching Google: {exc.reason}") from exc


def _access_token() -> str:
    """Current access token, refreshing if needed. Empty string when not connected."""
    tok = _load_token()
    if not tok:
        return ""
    if not token_expired(tok):
        return str(tok.get("access_token") or "")
    refresh = str(tok.get("refresh_token") or "")
    if not refresh:
        _clear_token()
        return ""
    client_id, client_secret = _oauth_client()
    if not client_id or not client_secret:
        return ""
    try:
        fresh = _token_post(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            }
        )
    except ValueError as exc:
        if "invalid_grant" in str(exc):
            _clear_token()
        raise
    tok["access_token"] = str(fresh.get("access_token") or "")
    tok["expires_at"] = time.time() + float(fresh.get("expires_in") or 3600) - 30.0
    if fresh.get("refresh_token"):
        tok["refresh_token"] = str(fresh["refresh_token"])
    _save_token(tok)
    return str(tok.get("access_token") or "")


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        self.server.oauth_result = params  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in params
        msg = "You're connected — close this tab and return to UEFN Ducky." if ok else (
            "Sign-in was cancelled or failed. Close this tab and try again from UEFN Ducky."
        )
        self.wfile.write(
            f"<html><body style='font-family:sans-serif;padding:2rem'><h2>UEFN Ducky × Google "
            f"Drive</h2><p>{msg}</p></body></html>".encode("utf-8")
        )

    def log_message(self, *args: Any) -> None:  # silence per-request stderr noise
        return


def _fetch_account_email(access_token: str) -> str:
    try:
        with _open(
            f"{API_BASE}/about?fields=user(emailAddress,displayName)",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        user = data.get("user") if isinstance(data, dict) else {}
        return str((user or {}).get("emailAddress") or "")
    except Exception:  # noqa: BLE001 — cosmetic only
        return ""


def _run_oauth_flow(timeout_s: float) -> dict[str, Any]:
    """Loopback OAuth: open the user's browser, wait for the redirect, swap the code."""
    client_id, client_secret = _oauth_client()
    if not client_id or not client_secret:
        raise ValueError(
            "Google sign-in isn't set up in this build (no bundled client). You can "
            "still use public share links (gdrive_download) with no login. To enable "
            "one-click sign-in for private Drive, a Google Cloud 'Desktop app' OAuth "
            "client must be configured — either bundled into the plugin, or pasted "
            "under Settings → Google Drive → Advanced."
        )
    if not _CONNECT_LOCK.acquire(blocking=False):
        raise ValueError("A Google Drive sign-in is already in progress — finish it in the browser")
    try:
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        server.oauth_result = None  # type: ignore[attr-defined]
        server.timeout = 1.0
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}"
        state = pysecrets.token_urlsafe(24)
        verifier = pysecrets.token_urlsafe(64)[:100]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": OAUTH_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        import webbrowser

        opened = webbrowser.open(auth_url)
        log.info("gdrive oauth: browser opened=%s port=%s", opened, port)
        deadline = time.monotonic() + max(30.0, min(timeout_s, 300.0))
        try:
            while time.monotonic() < deadline and server.oauth_result is None:  # type: ignore[attr-defined]
                server.handle_request()
            result = server.oauth_result  # type: ignore[attr-defined]
        finally:
            server.server_close()
        if result is None:
            raise ValueError(
                "Timed out waiting for the Google sign-in. If the browser did not open, "
                f"visit this URL yourself, then retry: {auth_url}"
            )
        if result.get("error"):
            raise ValueError(f"Google sign-in failed: {result['error']}")
        if result.get("state") != state:
            raise ValueError("OAuth state mismatch — sign-in rejected (possible tampering), try again")
        code = str(result.get("code") or "")
        if not code:
            raise ValueError("Google returned no auth code — try again")
        token = _token_post(
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            }
        )
        access = str(token.get("access_token") or "")
        if not access:
            raise ValueError("Google returned no access token — check the OAuth client and retry")
        email = _fetch_account_email(access)
        _save_token(
            {
                "access_token": access,
                "refresh_token": str(token.get("refresh_token") or ""),
                "expires_at": time.time() + float(token.get("expires_in") or 3600) - 30.0,
                "scope": str(token.get("scope") or OAUTH_SCOPE),
                "email": email,
                "obtained_at": time.time(),
            }
        )
        log.info("gdrive oauth: connected as %s", email or "(email unknown)")
        return {"account": email}
    finally:
        _CONNECT_LOCK.release()


# --------------------------------------------------------------------------- #
# Drive API — metadata, listing, download.
# --------------------------------------------------------------------------- #


def _auth_modes() -> list[str]:
    modes: list[str] = []
    if _load_token():
        modes.append("oauth")
    if _get_secret(KEY_API):
        modes.append("api_key")
    modes.append("public")
    return modes


def _api_get_json(path_and_query: str, *, mode: str, timeout: float = 30.0) -> dict[str, Any]:
    url = f"{API_BASE}/{path_and_query.lstrip('/')}"
    headers: dict[str, str] = {}
    if mode == "oauth":
        token = _access_token()
        if not token:
            raise ValueError("Not connected to Google Drive — run gdrive_connect")
        headers["Authorization"] = f"Bearer {token}"
    elif mode == "api_key":
        key = _get_secret(KEY_API)
        if not key:
            raise ValueError("No Google API key set (Settings → Google Drive)")
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={urllib.parse.quote(key)}"
    else:
        raise ValueError(f"Metadata requires OAuth or an API key (mode {mode!r})")
    with _open(url, headers=headers, timeout=timeout) as resp:
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
            errors[mode] = f"HTTP {exc.code}: {_http_error_detail(exc)}"
        except (urllib.error.URLError, ValueError) as exc:
            errors[mode] = str(exc)
    return None, "", errors


def _public_probe(file_id: str) -> dict[str, Any] | None:
    """Name/size/mime of a public file via a 1-byte ranged download. None if blocked."""
    params = {"id": file_id, "export": "download"}
    for _attempt in range(2):
        url = PUBLIC_DOWNLOAD_URL + "?" + urllib.parse.urlencode(params)
        try:
            with _open(url, headers={"Range": "bytes=0-0"}, timeout=30) as resp:
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
        token = _access_token()
        if not token:
            raise ValueError("Not connected — run gdrive_connect")
        url = f"{API_BASE}/files/{urllib.parse.quote(file_id)}?alt=media&supportsAllDrives=true"
        with _open(url, headers={"Authorization": f"Bearer {token}"}, timeout=600) as resp:
            return _stream_to_file(resp, tmp_path, cap, md5_expect)
    if mode == "api_key":
        key = _get_secret(KEY_API)
        if not key:
            raise ValueError("No API key set")
        url = (
            f"{API_BASE}/files/{urllib.parse.quote(file_id)}?alt=media"
            f"&key={urllib.parse.quote(key)}"
        )
        with _open(url, timeout=600) as resp:
            return _stream_to_file(resp, tmp_path, cap, md5_expect)
    # public
    params: dict[str, str] = {"id": file_id, "export": "download"}
    for _attempt in range(2):
        url = PUBLIC_DOWNLOAD_URL + "?" + urllib.parse.urlencode(params)
        with _open(url, timeout=600) as resp:
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
                errors[mode] = f"HTTP {exc.code}: {_http_error_detail(exc)}"
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
            errors[mode] = f"HTTP {exc.code}: {_http_error_detail(exc)}"
        except (urllib.error.URLError, ValueError) as exc:
            errors[mode] = str(exc)
    detail = "; ".join(f"{m}: {e}" for m, e in errors.items())
    raise ValueError(f"Drive listing failed: {detail}")


# --------------------------------------------------------------------------- #
# register(api) — MCP tools.
# --------------------------------------------------------------------------- #


def register(api: Any) -> None:
    """Wire the Google Drive tools into the app's shared MCP server."""

    def _fail(exc: Exception, pretty: bool) -> str:
        return _tool_json({"ok": False, "error": str(exc) or exc.__class__.__name__}, pretty=pretty)

    @api.tool(intent=r"\b(google\s*drive|gdrive|drive\.google|drive\s+(link|folder|file))\b")
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
            api_key_set = bool(_get_secret(KEY_API))
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

    @api.tool()
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

    @api.tool()
    def gdrive_disconnect(revoke: bool = True, pretty: bool = False) -> str:
        """Disconnect Google Drive: revoke the token at Google (best effort) and erase it."""
        try:
            tok = _load_token()
            revoked = False
            if revoke and tok:
                for field in ("refresh_token", "access_token"):
                    value = str(tok.get(field) or "")
                    if not value:
                        continue
                    try:
                        with _open(
                            REVOKE_URL,
                            data=urllib.parse.urlencode({"token": value}).encode("utf-8"),
                            method="POST",
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                            timeout=20,
                        ):
                            revoked = True
                        break
                    except (urllib.error.URLError, ValueError):
                        continue
            _clear_token()
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

    @api.tool()
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

    @api.tool()
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

    @api.tool()
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

    @api.tool()
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
        destination_path (project content path e.g. /VideoTest/Models, or relative
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
                    "use e.g. /VideoTest/Models/Props or Models/Props "
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

    api.log("Google Drive tools registered")


# --------------------------------------------------------------------------- #
# Standalone self-check: py plugins/uefn-plugin-googledrive/backend/__init__.py

if __name__ == "__main__":
    import runpy, pathlib
    runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "selfcheck.py"), run_name="__main__")
