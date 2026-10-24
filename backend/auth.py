"""Google sign-in for the googledrive plugin: secrets, the Google-only HTTP core, OAuth.

Safety model (deliberate — keep it this way):
  - Requests go ONLY to Google hosts (accounts.google.com, oauth2/www
    .googleapis.com, drive[.usercontent].google.com, *.googleusercontent.com
    redirects), including every redirect.
  - OAuth uses the loopback redirect + state + PKCE (S256). Tokens live in the
    app's encrypted credential store (never in this package, never logged,
    never returned).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets as pysecrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
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
ABOUT_URL = "https://www.googleapis.com/drive/v3/about"

_ALLOWED_HOSTS = {
    "www.googleapis.com",
    "oauth2.googleapis.com",
    "accounts.google.com",
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
}

_CONNECT_LOCK = threading.Lock()


def token_expired(tok: dict[str, Any], now: float | None = None) -> bool:
    """True when the access token is missing or expires within 60 seconds."""
    if not isinstance(tok, dict) or not str(tok.get("access_token") or ""):
        return True
    try:
        expires_at = float(tok.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True
    return (now if now is not None else time.time()) >= expires_at - 60.0


# --------------------------------------------------------------------------- #
# Encrypted secret store (lazy app imports so this file stays runnable standalone).
# --------------------------------------------------------------------------- #


def _get_secret(name: str) -> str:
    from backend.agent.secrets import get_key

    return (get_key(name) or "").strip()


def _set_secret(name: str, value: str) -> None:
    from backend.agent.secrets import set_key

    set_key(name, value)


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


def _using_bundled_client() -> bool:
    """True when sign-in runs on a client shipped with the plugin (none ships today)."""
    return False


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
            f"{ABOUT_URL}?fields=user(emailAddress,displayName)",
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


def revoke_and_clear(revoke: bool = True) -> bool:
    """Revoke the stored token at Google (best effort), then erase it. True if Google took it."""
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
    return revoked
