"""Google account MCP tools and the panel calls behind the tab's sign-in buttons.

  - ``google_status``: connected accounts, what each may do (Drive read, Gmail, Manage
    Drive) and whether sign-in is set up in this build.
  - ``google_connect(gmail, manage_drive, account)``: add an account, or add Gmail /
    full Drive access to one. Drive read access is always asked for; Gmail and full
    Drive only when turned on (incremental consent, ``include_granted_scopes``).
  - ``google_disconnect(account)``: through ``safety.guarded_remove`` (preview until
    ``confirmed``), revokes the account's token at Google and forgets it on this PC.
  - ``google_accounts_list``: ``{accounts: [{email, name, gmail, manage_drive, scopes}]}``.
  - ``gdrive_status`` / ``gdrive_connect`` / ``gdrive_disconnect`` stay as aliases.

The sign-in itself is auth.py's. ``AccountTools`` talks to it through a small backend
(``list_accounts``, ``connect``, ``disconnect``, ``sign_in_ready``); ``ModuleAuth``
adapts the auth module, so tests pass a fake backend instead.

No package-relative imports: siblings are loaded by file, so tests can load this alone.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

_HERE = Path(__file__).resolve().parent


def _sibling(name: str) -> Any:
    """``backend/<name>.py``, loaded once by file (the package may not be importable)."""
    key = f"_googledrive_{name}"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, _HERE / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


safety = _sibling("safety")

SCOPE_DRIVE_READ = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.modify"
CONNECT_TIMEOUT_S = 180.0
CONNECT_TIMEOUT_MAX_S = 300.0


class ToolError(ValueError):
    """A request the tool refuses (bad input, unknown account…): becomes ``{ok: False, error}``."""


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _scopes(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.split()
    return list(dict.fromkeys(_str(s) for s in value if _str(s)))


def has_gmail(scopes: Iterable[str]) -> bool:
    return any("/auth/gmail." in s or s.endswith("/auth/gmail") or s == "https://mail.google.com/" for s in scopes)


def has_manage_drive(scopes: Iterable[str]) -> bool:
    """Full Drive access (``auth/drive``), not just ``drive.readonly`` / ``drive.file``."""
    return any(s == SCOPE_DRIVE for s in scopes)


def wanted_scopes(*, gmail: bool = False, manage_drive: bool = False) -> list[str]:
    """Scopes to ask for: Drive read always, Gmail and full Drive only when turned on."""
    out = [SCOPE_DRIVE_READ]
    if gmail:
        out.append(SCOPE_GMAIL)
    if manage_drive:
        out.append(SCOPE_DRIVE)
    return out


def account_row(raw: Any) -> dict[str, Any]:
    """``{email, name, gmail, manage_drive, drive, scopes}`` from a backend account (never a token)."""
    if isinstance(raw, str):
        raw = {"email": raw}
    raw = raw if isinstance(raw, dict) else {}
    scopes = _scopes(raw.get("scopes") or raw.get("scope"))
    return {
        "email": _str(raw.get("email") or raw.get("account")),
        "name": _str(raw.get("name")),
        "gmail": has_gmail(scopes),
        "manage_drive": has_manage_drive(scopes),
        "drive": bool(scopes) and any("/auth/drive" in s for s in scopes),
        "scopes": scopes,
    }


class ModuleAuth:
    """The tools' auth backend over auth.py: its multi-account calls when it has them,
    else the single saved sign-in (Drive read only)."""

    def __init__(self, module: Any) -> None:
        self.m = module

    def sign_in_ready(self) -> bool:
        fn = getattr(self.m, "sign_in_ready", None)
        if callable(fn):
            return bool(fn())
        client_id, client_secret = self.m._oauth_client()
        return bool(client_id and client_secret)

    def list_accounts(self) -> list[dict[str, Any]]:
        fn = getattr(self.m, "list_accounts", None)
        if callable(fn):
            return list(fn() or [])
        tok = self.m._load_token()
        if not tok:
            return []
        return [{"email": _str(tok.get("email")) or "Google account",
                 "scopes": _str(tok.get("scope")) or SCOPE_DRIVE_READ}]

    def connect(self, scopes: list[str], account: str = "", timeout_s: float = CONNECT_TIMEOUT_S) -> dict[str, Any]:
        fn = getattr(self.m, "connect", None)
        if callable(fn):
            return dict(fn(scopes=scopes, account=account, timeout_s=timeout_s) or {})
        if any(s != SCOPE_DRIVE_READ for s in scopes):
            raise ToolError("This build can only sign in with Drive read access; Gmail and Manage Drive "
                            "come with the Google Workspace sign-in update.")
        res = self.m._run_oauth_flow(timeout_s)
        return {"email": _str(res.get("account")), "scopes": [SCOPE_DRIVE_READ]}

    def disconnect(self, account: str, revoke: bool = True) -> bool:
        fn = getattr(self.m, "disconnect", None)
        if callable(fn):
            return bool(fn(account, revoke=revoke))
        return bool(self.m.revoke_and_clear(bool(revoke)))


class AccountTools:
    """The account tools over an auth backend. Every public method returns a dict."""

    def __init__(self, api: Any, *, auth: Any, extra_status: Callable[[], dict[str, Any]] | None = None) -> None:
        self.api = api
        self.auth = auth
        self._extra_status = extra_status

    # ── accounts ──────────────────────────────────────────────────────────

    def rows(self) -> list[dict[str, Any]]:
        out, seen = [], set()
        for raw in self.auth.list_accounts() or []:
            r = account_row(raw)
            if r["email"] and r["email"].lower() not in seen:
                seen.add(r["email"].lower())
                out.append(r)
        return out

    def gmail_accounts(self) -> list[str]:
        """Emails with Gmail turned on (what the Gmail tools read and write)."""
        return [r["email"] for r in self.rows() if r["gmail"]]

    def drive_accounts(self, *, manage: bool = False) -> list[str]:
        """Emails with Drive access; ``manage=True``: only those with Manage Drive on."""
        return [r["email"] for r in self.rows() if (r["manage_drive"] if manage else r["drive"] or r["manage_drive"])]

    def find(self, account: str) -> dict[str, Any] | None:
        want = _str(account).lower()
        return next((r for r in self.rows() if r["email"].lower() == want), None)

    def _one(self, account: str) -> dict[str, Any]:
        """The account a change is for: the named one, else the only one."""
        rows = self.rows()
        if _str(account):
            row = self.find(account)
            if row is None:
                raise ToolError(f"{account} is not a connected Google account.")
            return row
        if not rows:
            raise ToolError("No Google account is connected.")
        if len(rows) > 1:
            raise ToolError(f"Several Google accounts are connected ({', '.join(r['email'] for r in rows)}): "
                            "say which one with account=. Nothing was changed.")
        return rows[0]

    # ── tools ─────────────────────────────────────────────────────────────

    def google_accounts_list(self) -> dict[str, Any]:
        """Connected Google accounts and what each may do.

        Returns {ok, accounts: [{email, name, gmail, manage_drive, drive, scopes}], count}.
        gmail / manage_drive false = that access is off; offer google_connect(gmail=true)
        or google_connect(manage_drive=true, account=...).
        """
        rows = self.rows()
        return {"ok": True, "accounts": rows, "count": len(rows)}

    def google_status(self) -> dict[str, Any]:
        """Google Workspace status: connected accounts, Gmail / Manage Drive per account,
        whether sign-in is set up. Call this first.

        Returns {ok, connected, accounts, sign_in_ready, hints} plus Drive details
        (staging folder, size cap, access modes) when available.
        """
        rows = self.rows()
        ready = bool(self.auth.sign_in_ready())
        out: dict[str, Any] = {
            "ok": True,
            "connected": bool(rows),
            "account": rows[0]["email"] if len(rows) == 1 else None,
            "accounts": rows,
            "count": len(rows),
            "sign_in_ready": ready,
        }
        if self._extra_status is not None:
            try:
                extra = self._extra_status() or {}
            except Exception:  # noqa: BLE001 - Drive details are a bonus, status still answers
                extra = {}
            for key, value in extra.items():
                out.setdefault(key, value)
        hints: list[str] = []
        if not rows:
            hints.append("Sign in: run google_connect — Google's sign-in opens in the browser; finish it there."
                         if ready else "Google sign-in isn't set up in this build. Public Drive share links "
                         "still work with gdrive_download.")
        for r in rows:
            off = [name for name, on in (("Gmail", r["gmail"]), ("Manage Drive", r["manage_drive"])) if not on]
            if off:
                hints.append(f"{r['email']}: {' and '.join(off)} off — google_connect("
                             + ", ".join(f"{k}=true" for k, on in (("gmail", r["gmail"]),
                                                                   ("manage_drive", r["manage_drive"])) if not on)
                             + f", account='{r['email']}') turns it on.")
        out["hints"] = hints
        return out

    def google_connect(self, gmail: bool = False, manage_drive: bool = False, account: str = "",
                       timeout_s: float = CONNECT_TIMEOUT_S) -> dict[str, Any]:
        """Sign in with Google, or add Gmail / full Drive access to a connected account.

        Opens Google's own sign-in in the browser: tell the user to finish it there.
        Asks for Drive read access, plus Gmail when gmail=true and full Drive (upload,
        move, rename, trash, share) when manage_drive=true; access already given is
        kept. account: the email to add access to (empty = sign in, any account).
        Blocks until done or timeout_s (max 300). Returns {ok, account, gmail, manage_drive}.
        """
        current = self.find(account) if _str(account) else None
        if _str(account) and current is None:
            raise ToolError(f"{account} is not a connected Google account: run google_connect without account= "
                            "to add it.")
        if not self.auth.sign_in_ready():
            raise ToolError("Google sign-in isn't set up in this build.")
        scopes = wanted_scopes(gmail=bool(gmail), manage_drive=bool(manage_drive))
        if current:
            scopes = list(dict.fromkeys(current["scopes"] + scopes))
        try:
            timeout = max(30.0, min(float(timeout_s or CONNECT_TIMEOUT_S), CONNECT_TIMEOUT_MAX_S))
        except (TypeError, ValueError):
            timeout = CONNECT_TIMEOUT_S
        res = self.auth.connect(scopes, _str(account), timeout) or {}
        email = _str(res.get("email") or res.get("account")) or _str(account)
        row = self.find(email) if email else None
        row = row or account_row({"email": email, "scopes": res.get("scopes") or scopes})
        added = [name for name, want, on in (("Gmail", gmail, row["gmail"]),
                                             ("Manage Drive", manage_drive, row["manage_drive"])) if want and on]
        missing = [name for name, want, on in (("Gmail", gmail, row["gmail"]),
                                               ("Manage Drive", manage_drive, row["manage_drive"])) if want and not on]
        msg = f"Connected {row['email'] or 'your Google account'}"
        msg += f" with {' and '.join(added)}." if added else "."
        if missing:
            msg += f" Google didn't grant {' and '.join(missing)}: tick its box on the sign-in page and try again."
        return {"ok": True, "connected": True, "account": row["email"] or None, "gmail": row["gmail"],
                "manage_drive": row["manage_drive"], "scopes": row["scopes"], "message": msg}

    def google_disconnect(self, account: str = "", revoke: bool = True, confirmed: bool = False) -> dict[str, Any]:
        """Disconnect a Google account: revoke its access at Google and forget it on this PC.

        Without confirmed=true this only previews; show it to the user with
        ducky_ask_user and call again with confirmed=true after they say yes. Mail and
        files stay in Google. account: needed when several are connected.
        """
        row = self._one(account)
        email = row["email"]
        revoked: dict[str, bool] = {}

        def apply(ids: list[str]) -> dict[str, Any]:
            for ident in ids:
                revoked[ident] = bool(self.auth.disconnect(ident, revoke=bool(revoke)))
            return {"revoked_at_google": all(revoked.values()) if revoked else False}

        out = safety.guarded_remove(
            self.api, command="google_disconnect", kind="account", noun="Google account",
            items=[{"id": email, "label": email}], confirmed=bool(confirmed), apply=apply,
            inverse=None, verb="disconnect", account=email, params={"account": email},
        )
        if out.get("changed"):
            out.update({"connected": bool(self.rows()), "account": email,
                        "revoked_at_google": revoked.get(email, False),
                        "message": f"Disconnected {email}. Its mail and files stay in Google."})
        return out

    # ── aliases ───────────────────────────────────────────────────────────

    def gdrive_status(self) -> dict[str, Any]:
        """Alias of google_status (older name)."""
        return self.google_status()

    def gdrive_connect(self, timeout_s: float = CONNECT_TIMEOUT_S, gmail: bool = False,
                       manage_drive: bool = False, account: str = "") -> dict[str, Any]:
        """Alias of google_connect (older name): sign in with Google, Drive read access."""
        return self.google_connect(gmail=gmail, manage_drive=manage_drive, account=account, timeout_s=timeout_s)

    def gdrive_disconnect(self, revoke: bool = True, account: str = "", confirmed: bool = False) -> dict[str, Any]:
        """Alias of google_disconnect (older name): previews until confirmed=true."""
        return self.google_disconnect(account=account, revoke=revoke, confirmed=confirmed)


TOOL_NAMES = ("google_status", "google_connect", "google_disconnect", "google_accounts_list")
ALIAS_NAMES = ("gdrive_status", "gdrive_connect", "gdrive_disconnect")


def _safe(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """The tool / panel form of a method: errors become ``{ok: False, error}``."""

    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("pretty", None)
        try:
            return method(*args, **kwargs)
        except (ToolError, ValueError) as exc:
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}
        except Exception as exc:  # noqa: BLE001 - a tool answers, never raises across the bridge
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

    return call


def register(api: Any, *, auth: Any = None, extra_status: Callable[[], dict[str, Any]] | None = None,
             aliases: bool = True) -> AccountTools:
    """Add the account tools (``@api.tool(listener=False)``) and their panel RPCs of the same name.

    auth: a backend with list_accounts / connect / disconnect / sign_in_ready, or the
    auth module (wrapped in ModuleAuth); None loads backend/auth.py. extra_status():
    Drive details merged into google_status. aliases: also add the gdrive_* names.
    """
    if auth is None:
        auth = _sibling("auth")
    if not callable(getattr(auth, "list_accounts", None)) or not callable(getattr(auth, "sign_in_ready", None)) \
            or isinstance(auth, type(sys)):
        auth = ModuleAuth(auth)
    tools = AccountTools(api, auth=auth, extra_status=extra_status)
    for name in TOOL_NAMES + (ALIAS_NAMES if aliases else ()):
        fn = _safe(getattr(tools, name))
        api.tool(listener=False)(fn)
        api.register_panel_rpc(name, fn)
    return tools
