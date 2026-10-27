"""Google sign-in for the googledrive plugin: accounts, the Google-only HTTP core, OAuth.

Sign-in (``connect``) is a loopback OAuth flow on 127.0.0.1:<free port> with ``state``
and PKCE (S256). It always asks for Drive read access; Gmail (``gmail.modify``) and
full Drive (``drive``) are added to an account only when turned on, with
``include_granted_scopes=true`` so Google keeps what was granted before.

Who holds the client secret:
  - **Google Connect** (default): the secret lives only on uefnducky.org. The code swap
    and every refresh go through ``POST {BROKER_BASE}/token`` / ``/refresh`` with a
    UEFN Ducky sign-in proof from the Account plugin
    (``api.call_tool("account_session_token", {"audience": "google-connect"})``).
    The public client id comes from Google Connect too (``collect/client``, set once in
    Admin), so no build carries one. Answers are
    ``{ok, payload: {tokens}}``; ``google_reauth_required`` means sign in again.
  - **Your own client** (Settings → Advanced: client id + secret): Google Connect is
    skipped and the plugin talks to Google's token endpoint itself.

Accounts live in the app's encrypted secret store under ``google_accounts``, one entry
per Google account (tokens never logged or returned). The single sign-in of 1.0.x
(``gdrive_oauth_token``) is moved into it on first use; the old key is kept until the
moved account refreshes once, signs in again or is disconnected. Revoking goes
straight to Google.

Every request goes only to Google hosts and uefnducky.org, redirects included.
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
from typing import Any, Callable

log = logging.getLogger("uefn.plugin.googledrive")

PLUGIN_ID = "googledrive"
KEY_API = "gdrive_api_key"
KEY_CLIENT_ID = "gdrive_oauth_client_id"
KEY_CLIENT_SECRET = "gdrive_oauth_client_secret"
KEY_TOKEN = "gdrive_oauth_token"  # 1.0.x single sign-in, migrated into KEY_ACCOUNTS
KEY_ACCOUNTS = "google_accounts"

SCOPE_DRIVE_READ = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.modify"
OAUTH_SCOPE = SCOPE_DRIVE_READ

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about"

BROKER_ORIGIN = "https://uefnducky.org"
BROKER_BASE = f"{BROKER_ORIGIN}/api/v1/plugins/google-connect/collect"
BROKER_AUDIENCE = "google-connect"
PROOF_TOOL = "account_session_token"
_BROKER_CLIENT_ID = ""  # Google Connect's public client id, asked once per run

CONNECT_TIMEOUT_MAX_S = 300.0

_ALLOWED_HOSTS = {
    "www.googleapis.com",
    "oauth2.googleapis.com",
    "accounts.google.com",
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
    "gmail.googleapis.com",
    "uefnducky.org",
}

_CONNECT_LOCK = threading.Lock()
_STORE_LOCK = threading.RLock()
_API: Any = None


def bind(api: Any) -> None:
    """Give auth the plugin host api (for the Account plugin's sign-in proof)."""
    global _API
    _API = api


def token_expired(tok: dict[str, Any], now: float | None = None) -> bool:
    """True when the access token is missing or expires within 60 seconds."""
    if not isinstance(tok, dict) or not str(tok.get("access_token") or ""):
        return True
    try:
        expires_at = float(tok.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True
    return (now if now is not None else time.time()) >= expires_at - 60.0


def normalize_scopes(value: Any) -> list[str]:
    """Scopes from a space-separated string or a list, deduplicated, order kept."""
    if not value:
        return []
    if isinstance(value, str):
        value = value.split()
    return list(dict.fromkeys(str(s).strip() for s in value if str(s).strip()))


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) for one sign-in."""
    verifier = pysecrets.token_urlsafe(64)[:100]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


def build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    challenge: str,
    login_hint: str = "",
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


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


_INVALID_GRANT_MSG = (
    "Google rejected the saved sign-in (invalid_grant) — it was revoked or expired. "
    "Sign in again with google_connect."
)
_REAUTH_MARKERS = ("invalid_grant", "google_reauth_required")


def _needs_reauth(text: str) -> bool:
    return any(m in text for m in _REAUTH_MARKERS)


def _token_post(form: dict[str, str]) -> dict[str, Any]:
    """Google's token endpoint directly (your own client only: it needs the secret)."""
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
            raise ValueError(_INVALID_GRANT_MSG) from exc
        raise ValueError(f"Google token endpoint error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Network error reaching Google: {exc.reason}") from exc


# --------------------------------------------------------------------------- #
# Google Connect (uefnducky.org holds the client secret).
# --------------------------------------------------------------------------- #


def _session_proof() -> str:
    """A short-lived UEFN Ducky sign-in proof for Google Connect, from the Account plugin."""
    if _API is None or not callable(getattr(_API, "call_tool", None)):
        raise ValueError("Google sign-in isn't ready: the plugin host is not connected yet")
    try:
        out = _API.call_tool(PROOF_TOOL, {"audience": BROKER_AUDIENCE})
    except Exception as exc:  # noqa: BLE001 — missing Account plugin, not signed in…
        raise ValueError(
            "Google sign-in needs the Ducky Account plugin and a UEFN Ducky sign-in "
            "(Settings → Account), or your own OAuth client in Settings → Google → Advanced."
        ) from exc
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            out = {}
    proof = str((out or {}).get("proof") or "") if isinstance(out, dict) else ""
    if not proof or (isinstance(out, dict) and out.get("ok") is False):
        raise ValueError(
            "Sign in to UEFN Ducky first (Settings → Account) to use Google sign-in, or set "
            "your own OAuth client in Settings → Google → Advanced."
        )
    return proof


def _broker_post(event: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["proof"] = _session_proof()
    try:
        with _open(
            f"{BROKER_BASE}/{event}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            # Core refuses a collect POST without a same-site Origin (403 forbidden).
            headers={"Content-Type": "application/json", "Accept": "application/json", "Origin": BROKER_ORIGIN},
            timeout=30,
        ) as resp:
            out = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if _needs_reauth(detail):
            raise ValueError(_INVALID_GRANT_MSG) from exc
        if exc.code == 401:
            raise ValueError(
                "Google Connect refused the UEFN Ducky sign-in — sign in again in Settings → Account"
            ) from exc
        raise ValueError(f"Google Connect error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Network error reaching uefnducky.org: {exc.reason}") from exc
    except ValueError as exc:
        raise ValueError("Google Connect sent an answer that isn't JSON") from exc
    if not isinstance(out, dict):
        raise ValueError("Google Connect sent an unexpected answer")
    if out.get("ok") is False or out.get("error"):
        err = str(out.get("error") or "request failed")
        if _needs_reauth(err):
            raise ValueError(_INVALID_GRANT_MSG)
        raise ValueError(f"Google Connect: {err[:300]}")
    result = out.get("payload", out)
    if isinstance(result, dict) and isinstance(result.get("tokens"), dict):
        result = result["tokens"]
    return result if isinstance(result, dict) else {}


def _broker_client_id() -> str:
    """The Desktop app client id Google Connect signs in with (public; the secret stays there)."""
    global _BROKER_CLIENT_ID
    if not _BROKER_CLIENT_ID:
        client_id = str(_broker_post("client", {}).get("client_id") or "").strip()
        if not client_id.endswith(".apps.googleusercontent.com"):
            raise ValueError(
                "One-click Google sign-in isn't set up on uefnducky.org yet. Use your own OAuth "
                "client in Settings → Google → Advanced, or public share links with gdrive_download."
            )
        _BROKER_CLIENT_ID = client_id
    return _BROKER_CLIENT_ID


# --------------------------------------------------------------------------- #
# Clients and the account store.
# --------------------------------------------------------------------------- #


def _oauth_client() -> tuple[str, str]:
    """The user's own OAuth client (client_id, client_secret) from Settings."""
    return _get_secret(KEY_CLIENT_ID).strip(), _get_secret(KEY_CLIENT_SECRET).strip()


def _uses_own_client() -> bool:
    client_id, client_secret = _oauth_client()
    return bool(client_id and client_secret)


def _using_bundled_client() -> bool:
    """True when sign-in goes through Google Connect (no own client set in Settings)."""
    return not _uses_own_client() and _API is not None


def sign_in_ready() -> bool:
    return _uses_own_client() or _API is not None


def _legacy_token() -> dict[str, Any] | None:
    raw = _get_secret(KEY_TOKEN)
    if not raw:
        return None
    try:
        tok = json.loads(raw)
        return tok if isinstance(tok, dict) else None
    except (ValueError, TypeError):
        return None


def _clear_token() -> None:
    """Erase the 1.0.x single sign-in."""
    _set_secret(KEY_TOKEN, "")


def _read_store() -> list[dict[str, Any]]:
    raw = _get_secret(KEY_ACCOUNTS)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    accts = data.get("accounts") if isinstance(data, dict) else data
    return [a for a in (accts or []) if isinstance(a, dict) and str(a.get("email") or "")]


def _write_store(accts: list[dict[str, Any]]) -> None:
    _set_secret(KEY_ACCOUNTS, json.dumps({"version": 1, "accounts": accts}))


def _migrate_legacy(accts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move the 1.0.x sign-in into the account store (the old key stays until confirmed)."""
    if accts:
        return accts
    tok = _legacy_token()
    if not tok or not (tok.get("refresh_token") or tok.get("access_token")):
        return accts
    accts = [
        {
            "email": str(tok.get("email") or "") or "Google account",
            "name": "",
            "scopes": normalize_scopes(tok.get("scope")) or [SCOPE_DRIVE_READ],
            "access_token": str(tok.get("access_token") or ""),
            "refresh_token": str(tok.get("refresh_token") or ""),
            "expires_at": float(tok.get("expires_at") or 0),
            "via": "own",  # 1.0.x only ever signed in with the user's own client
            "migrated": True,
            "obtained_at": float(tok.get("obtained_at") or time.time()),
        }
    ]
    _write_store(accts)
    log.info("google sign-in: moved the saved 1.0 sign-in into the account store")
    return accts


def _accounts() -> list[dict[str, Any]]:
    with _STORE_LOCK:
        return _migrate_legacy(_read_store())


def _find(accts: list[dict[str, Any]], account: str) -> dict[str, Any] | None:
    want = str(account or "").strip().lower()
    for a in accts:
        if str(a.get("email") or "").lower() == want:
            return a
    return None


def _confirm_migration(acct: dict[str, Any]) -> None:
    """The moved 1.0.x account works (or is gone): drop the old key."""
    if acct.pop("migrated", None):
        _clear_token()


def _public(acct: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": str(acct.get("email") or ""),
        "name": str(acct.get("name") or ""),
        "scopes": normalize_scopes(acct.get("scopes")),
        "via": str(acct.get("via") or "broker"),
        "needs_sign_in": bool(acct.get("needs_sign_in")),
    }


def list_accounts() -> list[dict[str, Any]]:
    """Connected Google accounts in connection order: {email, name, scopes, via, needs_sign_in}."""
    return [_public(a) for a in _accounts()]


def gmail_accounts() -> list[str]:
    return [a["email"] for a in list_accounts() if SCOPE_GMAIL in a["scopes"] and not a["needs_sign_in"]]


def _has_drive(acct: dict[str, Any]) -> bool:
    return any("/auth/drive" in s for s in normalize_scopes(acct.get("scopes")))


# --------------------------------------------------------------------------- #
# Access tokens.
# --------------------------------------------------------------------------- #


def _refresh_tokens(acct: dict[str, Any]) -> dict[str, Any]:
    refresh = str(acct.get("refresh_token") or "")
    if not refresh:
        raise ValueError(_INVALID_GRANT_MSG)
    if acct.get("via") == "own":
        client_id, client_secret = _oauth_client()
        if not client_id or not client_secret:
            raise ValueError(
                "This account was signed in with your own OAuth client, which is no longer "
                "set in Settings — sign in again with google_connect"
            )
        return _token_post(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            }
        )
    return _broker_post("refresh", {"refresh_token": refresh})


def access_token(account: str = "", *, scope: str = "") -> str:
    """A current access token for one account (refreshed when needed). Raises ValueError.

    account: the email; empty → the first account that has ``scope`` (or the first one).
    """
    with _STORE_LOCK:
        accts = _accounts()
        if not accts:
            raise ValueError("No Google account is connected — sign in with google_connect")
        if account:
            acct = _find(accts, account)
            if acct is None:
                raise ValueError(f"No connected Google account matches {account!r}")
        else:
            acct = next((a for a in accts if not scope or scope in normalize_scopes(a.get("scopes"))), accts[0])
        if scope and scope not in normalize_scopes(acct.get("scopes")):
            what = "Gmail" if scope == SCOPE_GMAIL else "Manage Drive" if scope == SCOPE_DRIVE else scope
            raise ValueError(f"Turn on {what} for this account first (google_connect)")
        if not token_expired(acct):
            return str(acct.get("access_token") or "")
        try:
            fresh = _refresh_tokens(acct)
        except ValueError as exc:
            if "invalid_grant" in str(exc):
                acct["access_token"] = ""
                acct["needs_sign_in"] = True
                _write_store(accts)
            raise
        token = str(fresh.get("access_token") or "")
        if not token:
            raise ValueError("Google returned no access token on refresh — sign in again")
        acct["access_token"] = token
        acct["expires_at"] = time.time() + float(fresh.get("expires_in") or 3600) - 30.0
        if fresh.get("refresh_token"):
            acct["refresh_token"] = str(fresh["refresh_token"])
        if fresh.get("scope"):
            acct["scopes"] = normalize_scopes(fresh["scope"])
        acct.pop("needs_sign_in", None)
        _confirm_migration(acct)
        _write_store(accts)
        return token


def access_token_getter(account: str = "", *, scope: str = "") -> Callable[[], str]:
    """``token()`` for one account, for clients that call it before each request."""
    return lambda: access_token(account, scope=scope)


def _load_token() -> dict[str, Any] | None:
    """The first account with Drive access as {email, scope} (no tokens), else None."""
    acct = next((a for a in _accounts() if _has_drive(a) and not a.get("needs_sign_in")), None)
    if acct is None:
        return None
    return {"email": str(acct.get("email") or ""), "scope": " ".join(normalize_scopes(acct.get("scopes")))}


def _access_token() -> str:
    """Drive access token of the first Drive account; empty string when none is connected."""
    tok = _load_token()
    return access_token(tok["email"]) if tok else ""


# --------------------------------------------------------------------------- #
# Sign-in (loopback + state + PKCE), disconnect.
# --------------------------------------------------------------------------- #


# The page Google's redirect lands on, in UEFN Ducky's look. Self-contained: no outside
# fonts, scripts or pictures. Its script only takes the one-time code out of the address bar.
_DONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>UEFN Ducky × Google</title>
<style>
:root{--bg:#0a0a0a;--card:#141414;--border:#262626;--fg:#ededed;--muted:#a1a1a1;--tone:__TONE__}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
body{display:grid;place-items:center;overflow:hidden}
.glow{position:fixed;width:560px;height:560px;border-radius:50%;
  background:radial-gradient(circle,color-mix(in srgb,var(--tone) 22%,transparent),transparent 65%);
  filter:blur(10px);animation:breathe 5s ease-in-out infinite}
.card{position:relative;width:min(420px,calc(100vw - 32px));padding:36px 32px 28px;text-align:center;
  background:var(--card);border:1px solid var(--border);border-radius:20px;
  box-shadow:0 24px 64px rgba(0,0,0,.55);animation:rise .6s cubic-bezier(.2,.8,.2,1) both}
.brand{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:26px;
  font-weight:600;letter-spacing:.2px;color:var(--muted);font-size:13px;text-transform:uppercase}
.brand b{color:var(--fg);font-weight:700}
.badge{width:84px;height:84px;margin:0 auto 22px;animation:pop .5s .25s cubic-bezier(.3,1.6,.5,1) both}
.badge circle{fill:color-mix(in srgb,var(--tone) 14%,transparent);stroke:var(--tone);stroke-width:3;
  stroke-dasharray:240;stroke-dashoffset:240;animation:draw .7s .3s ease-out forwards}
.badge path{fill:none;stroke:var(--tone);stroke-width:5;stroke-linecap:round;stroke-linejoin:round;
  stroke-dasharray:60;stroke-dashoffset:60;animation:draw .45s .85s ease-out forwards}
h1{margin:0 0 8px;font-size:22px;font-weight:700;animation:fade .5s .5s both}
p{margin:0;color:var(--muted);animation:fade .5s .65s both}
.hint{margin-top:22px;padding:10px 14px;border-radius:12px;background:#1b1b1b;border:1px solid var(--border);
  font-size:13px;color:var(--muted);animation:fade .5s .8s both}
.hint b{color:var(--tone);font-weight:600}
@keyframes rise{from{opacity:0;transform:translateY(18px) scale(.98)}to{opacity:1;transform:none}}
@keyframes pop{from{transform:scale(.6);opacity:0}to{transform:none;opacity:1}}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes breathe{0%,100%{transform:scale(1);opacity:.9}50%{transform:scale(1.08);opacity:1}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important}
  .badge circle,.badge path{stroke-dashoffset:0}}
</style></head>
<body>
<div class="glow" aria-hidden="true"></div>
<main class="card" role="status">
  <div class="brand"><span aria-hidden="true">\U0001f986</span><b>UEFN Ducky</b><span>× Google</span></div>
  <svg class="badge" viewBox="0 0 84 84" aria-hidden="true"><circle cx="42" cy="42" r="38"/><path d="__MARK__"/></svg>
  <h1>__TITLE__</h1>
  <p>__TEXT__</p>
  <div class="hint">__HINT__</div>
</main>
<script>history.replaceState(null, "", location.pathname);</script>
</body></html>
"""


def done_page(ok: bool) -> str:
    """The redirect page: a check on amber when signed in, a cross on red when not."""
    if ok:
        parts = {
            "__TONE__": "#fbb034",
            "__MARK__": "M27 43 L37 53 L58 31",
            "__TITLE__": "You're connected",
            "__TEXT__": "Google is linked to UEFN Ducky. Your mail and files stay on this PC.",
            "__HINT__": "You can close this tab and go back to <b>UEFN Ducky</b>.",
        }
    else:
        parts = {
            "__TONE__": "#ef4444",
            "__MARK__": "M31 31 L53 53 M53 31 L31 53",
            "__TITLE__": "Sign-in didn't finish",
            "__TEXT__": "Google sign-in was cancelled or failed. Nothing was changed.",
            "__HINT__": "Close this tab and press <b>Sign in with Google</b> in UEFN Ducky again.",
        }
    page = _DONE_PAGE
    for key, value in parts.items():
        page = page.replace(key, value)
    return page


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        if any(k in params for k in ("code", "error", "state")):
            self.server.oauth_result = params  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")  # the address holds the one-time code
        self.end_headers()
        self.wfile.write(done_page("code" in params).encode("utf-8"))

    def log_message(self, *args: Any) -> None:  # silence per-request stderr noise
        return


def _open_browser(url: str) -> bool:
    import webbrowser

    return bool(webbrowser.open(url))


def _fetch_account_email(access: str) -> str:
    try:
        with _open(
            f"{ABOUT_URL}?fields=user(emailAddress,displayName)",
            headers={"Authorization": f"Bearer {access}"},
            timeout=20,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        user = data.get("user") if isinstance(data, dict) else {}
        return str((user or {}).get("emailAddress") or "")
    except Exception:  # noqa: BLE001 — cosmetic only
        return ""


def _loopback_consent(client_id: str, scopes: list[str], login_hint: str, timeout_s: float) -> dict[str, str]:
    """Open Google's consent page and wait for the redirect → {code, verifier, redirect_uri}."""
    if not _CONNECT_LOCK.acquire(blocking=False):
        raise ValueError("A Google sign-in is already in progress — finish it in the browser")
    try:
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        server.oauth_result = None  # type: ignore[attr-defined]
        server.timeout = 1.0
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        state = pysecrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        auth_url = build_auth_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            state=state,
            challenge=challenge,
            login_hint=login_hint,
        )
        try:
            opened = _open_browser(auth_url)
            log.info("google sign-in: browser opened=%s", opened)
            deadline = time.monotonic() + max(1.0, min(float(timeout_s or 0), CONNECT_TIMEOUT_MAX_S))
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
            if result["error"] == "access_denied":
                raise ValueError("Google sign-in was cancelled — nothing was changed")
            raise ValueError(f"Google sign-in failed: {result['error']}")
        if result.get("state") != state:
            raise ValueError("OAuth state mismatch — sign-in rejected (possible tampering), try again")
        code = str(result.get("code") or "")
        if not code:
            raise ValueError("Google returned no auth code — try again")
        return {"code": code, "verifier": verifier, "redirect_uri": redirect_uri}
    finally:
        _CONNECT_LOCK.release()


def connect(scopes: list[str] | None = None, account: str = "", timeout_s: float = 180.0) -> dict[str, Any]:
    """Sign in a Google account, or add scopes to one (incremental consent).

    Drive read access is always included; pass SCOPE_GMAIL / SCOPE_DRIVE to turn those on.
    Returns {email, name, scopes, added, denied} — never tokens.
    """
    wanted = normalize_scopes([SCOPE_DRIVE_READ] + list(scopes or []))
    existing = _find(_accounts(), account) if account else None
    if existing:
        wanted = normalize_scopes(normalize_scopes(existing.get("scopes")) + wanted)
    via = "own" if _uses_own_client() else "broker"
    client_id = _oauth_client()[0] if via == "own" else _broker_client_id()
    got = _loopback_consent(client_id, wanted, str(account or "").strip(), timeout_s)
    if via == "own":
        client_id, client_secret = _oauth_client()
        tokens = _token_post(
            {
                "code": got["code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": got["redirect_uri"],
                "grant_type": "authorization_code",
                "code_verifier": got["verifier"],
            }
        )
    else:
        tokens = _broker_post(
            "token",
            {
                "code": got["code"],
                "code_verifier": got["verifier"],
                "redirect_uri": got["redirect_uri"],
                "client_id": client_id,
            },
        )
    access = str(tokens.get("access_token") or "")
    if not access:
        raise ValueError("Google returned no access token — try signing in again")
    granted = normalize_scopes(tokens.get("scope")) or wanted
    email = _fetch_account_email(access) or str(account or "").strip() or "Google account"
    with _STORE_LOCK:
        accts = _accounts()
        acct = _find(accts, email)
        before = normalize_scopes(acct.get("scopes")) if acct else []
        if acct is None:
            acct = {"email": email, "name": ""}
            accts.append(acct)
        acct.update(
            {
                "scopes": granted,
                "access_token": access,
                "refresh_token": str(tokens.get("refresh_token") or acct.get("refresh_token") or ""),
                "expires_at": time.time() + float(tokens.get("expires_in") or 3600) - 30.0,
                "via": via,
                "obtained_at": time.time(),
            }
        )
        acct.pop("needs_sign_in", None)
        _confirm_migration(acct)
        _write_store(accts)
    log.info("google sign-in: account connected (%d scopes, via %s)", len(granted), via)
    return {
        "email": email,
        "name": str(acct.get("name") or ""),
        "scopes": granted,
        "added": [s for s in granted if s not in before],
        "denied": [s for s in wanted if s not in granted],
    }


def _revoke_at_google(acct: dict[str, Any]) -> bool:
    for field in ("refresh_token", "access_token"):
        value = str(acct.get(field) or "")
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
                return True
        except (urllib.error.URLError, ValueError):
            continue
    return False


def disconnect(account: str, revoke: bool = True) -> bool:
    """Revoke one account at Google (best effort) and forget it. True if Google took the revoke."""
    with _STORE_LOCK:
        accts = _accounts()
        acct = _find(accts, account)
        if acct is None:
            raise ValueError(f"No connected Google account matches {account!r}")
        revoked = _revoke_at_google(acct) if revoke else False
        accts.remove(acct)
        _confirm_migration(acct)
        _write_store(accts)
    return revoked


def revoke_and_clear(revoke: bool = True) -> bool:
    """Disconnect every account (and the 1.0.x key). True if Google took any revoke."""
    revoked = False
    for acct in list_accounts():
        revoked = disconnect(acct["email"], revoke=revoke) or revoked
    _clear_token()
    return revoked


def _run_oauth_flow(timeout_s: float) -> dict[str, Any]:
    """1.0.x entry point (gdrive_connect): sign in with Drive read access."""
    res = connect([SCOPE_DRIVE_READ], "", timeout_s)
    return {"account": res["email"]}
