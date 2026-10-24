"""Drive manage MCP tools and the panel calls behind the Drive tab's buttons.

  - Organize: upload, new folder, move, rename, copy. Each records a Changes-ledger
    entry whose inverse undoes it (move back, rename back, trash the new copy/folder).
  - Trash / untrash: trash goes through ``safety.guarded_remove`` (preview until
    ``confirmed``, at most 50, trash only; Revert untrashes). Nothing deletes for good.
  - Share: adds one person's access; ``destructive_tools`` in plugin.json, so Ducky asks
    the person first. Revert can't take access back, so it is a manual change.
  - Storage usage: how full each Drive is (read access is enough).

Every change needs **Manage Drive** (the full ``drive`` scope) on that account; Drive is
read-only until the user turns it on. ``accounts()`` returns the connected accounts
(``{email, drive, manage_drive}``, tools_account rows), ``token_for(email)`` an access
token. Every request goes through ``http`` (default: auth.py's Google-only opener).

No package-relative imports: siblings are loaded by file, so tests can load this alone.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import mimetypes
import re
import sys
import urllib.error
import urllib.parse
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

API_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,parents,trashed,size,webViewLink,modifiedTime"
UPLOAD_MAX_BYTES = 5 * 1024 * 1024 * 1024
UPLOAD_CHUNK = 8 * 1024 * 1024  # a multiple of 256 KiB, as resumable uploads require
SHARE_ROLES = ("reader", "commenter", "writer")
NAME_MAX = 255

HttpFn = Callable[..., tuple[int, dict[str, str], bytes]]
AccountsFn = Callable[[], Iterable[Any]]
TokenFn = Callable[[str], str]


class ToolError(ValueError):
    """A request the tool refuses (bad input, Manage Drive off…): becomes ``{ok: False, error}``."""


class DriveError(ValueError):
    """Google answered a Drive call with an error."""

    def __init__(self, message: str, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_URL_ID = (
    re.compile(r"/(?:file|document|spreadsheets|presentation|drawings)/d/([A-Za-z0-9_-]{10,128})"),
    re.compile(r"/folders/([A-Za-z0-9_-]{10,128})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,128})"),
)
_DRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")
_EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")


def file_id(ref: Any) -> str:
    """A Drive id from an id, a file row (``{id}``) or a drive/docs.google.com link."""
    if isinstance(ref, dict):
        ref = ref.get("id")
    t = _str(ref).strip("<>\"'")
    if not t:
        raise ToolError("A Drive file is required: pass its id or share link.")
    if t.lower() == "root" or _ID_RE.match(t):
        return t
    host = urllib.parse.urlparse(t).netloc.lower()
    if host and host not in _DRIVE_HOSTS:
        raise ToolError(f"Not a Google Drive link (host {host!r}).")
    for pat in _URL_ID:
        m = pat.search(t)
        if m:
            return m.group(1)
    raise ToolError(f"No Drive file id in {t!r}.")


def file_ids(value: Any) -> list[str]:
    """Ids from one reference, a comma/newline list, or a list of ids / rows / links."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(file_ids(item))
        return list(dict.fromkeys(out))
    if isinstance(value, dict):
        return [file_id(value)]
    return list(dict.fromkeys(file_id(p) for p in str(value).replace("\n", ",").split(",") if p.strip()))


def clean_name(name: Any) -> str:
    """A Drive file or folder name: one line, no control characters, at most 255 characters."""
    n = re.sub(r"[\x00-\x1f\x7f]", "", _str(name)).strip()
    if not n:
        raise ToolError("A name is required.")
    return n[:NAME_MAX]


def human_size(n: Any) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{int(v)} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return "—"


def file_row(f: dict[str, Any], account: str = "") -> dict[str, Any]:
    """One result row from a Drive file resource."""
    mime = _str(f.get("mimeType"))
    return {
        "id": _str(f.get("id")),
        "name": _str(f.get("name")),
        "mimeType": mime,
        "folder": mime == FOLDER_MIME,
        "parents": list(f.get("parents") or []),
        "trashed": bool(f.get("trashed")),
        "size": int(f["size"]) if _str(f.get("size")).isdigit() else None,
        "webViewLink": _str(f.get("webViewLink")),
        "account": account,
    }


def _lower_headers(headers: Any) -> dict[str, str]:
    try:
        items = headers.items()
    except AttributeError:
        items = []
    return {str(k).lower(): str(v) for k, v in items}


def default_http(method: str, url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None,
                 timeout: float = 60.0) -> tuple[int, dict[str, str], bytes]:
    """One request through auth.py's Google-only opener → (status, lower-case headers, body)."""
    auth = _sibling("auth")
    try:
        with auth._open(url, headers=headers, data=data, method=method, timeout=timeout) as resp:
            return int(resp.status), _lower_headers(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            body = b""
        return int(exc.code), _lower_headers(exc.headers), body
    except urllib.error.URLError as exc:
        raise DriveError(f"Network error reaching Google: {exc.reason}") from exc


def _error_of(status: int, body: bytes) -> DriveError:
    message, reason = "", ""
    try:
        err = (json.loads(body.decode("utf-8", "replace")) or {}).get("error")
        if isinstance(err, dict):
            message = _str(err.get("message"))
            reason = _str(((err.get("errors") or [{}])[0] or {}).get("reason"))
        elif isinstance(err, str):
            message = err
    except (ValueError, AttributeError, TypeError):
        pass
    return DriveError(f"Google Drive error {status}: {message or 'request failed'}", status, reason)


class DriveClient:
    """The Drive v3 calls the manage tools use, for one account's access token."""

    def __init__(self, http: HttpFn, token: str) -> None:
        self.http = http
        self.token = token

    def _call(self, method: str, url: str, *, params: dict[str, Any] | None = None, body: Any = None,
              data: bytes | None = None, headers: dict[str, str] | None = None,
              ok: tuple[int, ...] = (200, 201, 204)) -> tuple[int, dict[str, str], bytes]:
        q = {"supportsAllDrives": "true", **{k: v for k, v in (params or {}).items() if v not in (None, "")}}
        full = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(q)}"
        h = {"Authorization": f"Bearer {self.token}", **(headers or {})}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json; charset=UTF-8")
        status, rh, rb = self.http(method, full, headers=h, data=data, timeout=120.0)
        if status not in ok:
            raise _error_of(status, rb)
        return status, rh, rb

    def _json(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        _s, _h, rb = self._call(method, url, **kw)
        try:
            return json.loads(rb.decode("utf-8")) if rb else {}
        except ValueError:
            return {}

    def get(self, fid: str, fields: str = FILE_FIELDS) -> dict[str, Any]:
        return self._json("GET", f"{API_BASE}/files/{fid}", params={"fields": fields})

    def update(self, fid: str, body: dict[str, Any] | None = None, *, add_parents: str = "",
               remove_parents: str = "") -> dict[str, Any]:
        return self._json("PATCH", f"{API_BASE}/files/{fid}", body=body or {},
                          params={"fields": FILE_FIELDS, "addParents": add_parents, "removeParents": remove_parents})

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", f"{API_BASE}/files", body=body, params={"fields": FILE_FIELDS})

    def copy(self, fid: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", f"{API_BASE}/files/{fid}/copy", body=body, params={"fields": FILE_FIELDS})

    def about(self) -> dict[str, Any]:
        return self._json("GET", f"{API_BASE}/about", params={"fields": "storageQuota,user(emailAddress)"})

    def share(self, fid: str, body: dict[str, Any], *, notify: bool, message: str = "") -> dict[str, Any]:
        return self._json("POST", f"{API_BASE}/files/{fid}/permissions", body=body,
                          params={"fields": "id,role,type,emailAddress",
                                  "sendNotificationEmail": "true" if notify else "false",
                                  "emailMessage": message if notify else ""})

    def upload(self, path: Path, meta: dict[str, Any], mime: str, *, chunk: int = UPLOAD_CHUNK) -> dict[str, Any]:
        """A resumable upload: open a session, then send the file in ``chunk``-byte pieces."""
        size = path.stat().st_size
        _s, rh, _b = self._call("POST", UPLOAD_URL, body=meta,
                                params={"uploadType": "resumable", "fields": FILE_FIELDS},
                                headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
        session = rh.get("location", "")
        if not session.startswith(UPLOAD_URL):
            raise DriveError("Google didn't open an upload session.")
        h = {"Authorization": f"Bearer {self.token}"}
        if size == 0:
            status, _h, rb = self.http("PUT", session, headers={**h, "Content-Range": "bytes */0"}, data=b"",
                                       timeout=120.0)
            if status not in (200, 201):
                raise _error_of(status, rb)
            return json.loads(rb.decode("utf-8") or "{}")
        offset = 0
        with path.open("rb") as fh:
            while offset < size:
                fh.seek(offset)
                piece = fh.read(chunk)
                end = offset + len(piece) - 1
                status, rh, rb = self.http("PUT", session, data=piece, timeout=300.0,
                                           headers={**h, "Content-Range": f"bytes {offset}-{end}/{size}"})
                if status in (200, 201):
                    return json.loads(rb.decode("utf-8") or "{}")
                if status != 308:
                    raise _error_of(status, rb)
                got = re.match(r"bytes=0-(\d+)$", rh.get("range", ""))
                offset = int(got.group(1)) + 1 if got else 0
        raise DriveError("The upload ended before Google confirmed the file.")


class DriveTools:
    """The Drive manage tools over connected accounts. Every public method returns a dict."""

    def __init__(self, api: Any, *, accounts: AccountsFn, token_for: TokenFn, http: HttpFn | None = None,
                 max_upload_bytes: int = UPLOAD_MAX_BYTES, chunk: int = UPLOAD_CHUNK) -> None:
        self.api = api
        self._accounts_fn = accounts
        self._token_for = token_for
        self.http = http or default_http
        self.max_upload_bytes = int(max_upload_bytes)
        self.chunk = int(chunk)

    # ── accounts ──────────────────────────────────────────────────────────

    def accounts(self) -> list[dict[str, Any]]:
        out = []
        for raw in self._accounts_fn() or []:
            r = {"email": raw, "drive": True, "manage_drive": False} if isinstance(raw, str) else dict(raw or {})
            r["email"] = _str(r.get("email") or r.get("account"))
            if r["email"]:
                out.append(r)
        return out

    def _account(self, account: str) -> dict[str, Any]:
        rows = self.accounts()
        if _str(account):
            for r in rows:
                if r["email"].lower() == _str(account).lower():
                    return r
            raise ToolError(f"{account} is not a connected Google account.")
        if not rows:
            raise ToolError("No Google account is connected: sign in with google_connect first.")
        if len(rows) > 1:
            raise ToolError(f"Several Google accounts are connected ({', '.join(r['email'] for r in rows)}): "
                            "say which one with account=. Nothing was changed.")
        return rows[0]

    def client(self, email: str) -> DriveClient:
        token = _str(self._token_for(email))
        if not token:
            raise ToolError(f"{email} needs to sign in again: run google_connect(account='{email}').")
        return DriveClient(self.http, token)

    def manage(self, account: str = "") -> tuple[str, DriveClient]:
        """(email, client) for a change: needs Manage Drive on that account."""
        r = self._account(account)
        if not r.get("manage_drive"):
            raise ToolError(f"Manage Drive is off for {r['email']}, so Drive is read-only: offer "
                            f"google_connect(manage_drive=true, account='{r['email']}'). Nothing was changed.")
        return r["email"], self.client(r["email"])

    # ── ledger ────────────────────────────────────────────────────────────

    def record(self, *, command: str, kind: str, account: str, summary: str, targets: list[dict[str, Any]],
               inverse: list[dict[str, Any]] | None = None, before: dict[str, Any] | None = None) -> None:
        """One Changes-ledger entry; ``inverse`` (tool calls) makes it revertable."""
        changeset = getattr(self.api, "changeset", None)
        if changeset is None:
            return
        ids = [t.get("id", "") for t in targets]
        try:
            changeset.record(
                command=command,
                kind=kind,
                ident=f"{account}:{','.join(ids)}" if len(ids) < 4 else f"{account}:{len(ids)}:{ids[0]}",
                before=before or {},
                inverse=list(inverse or []),
                targets=targets,
                summary=summary,
                revertable="auto" if inverse else "manual",
                params={"account": account},
            )
        except Exception:  # noqa: BLE001 - the ledger never fails the tool
            pass

    @staticmethod
    def _target(f: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "drive_file", "id": _str(f.get("id")), "label": _str(f.get("name")) or _str(f.get("id")),
                "path": _str(f.get("id"))}

    @staticmethod
    def _trash_back(fid: str, account: str) -> list[dict[str, Any]]:
        """Inverse of making something new: trash it again (Revert is the user's own click)."""
        return [{"command": "gdrive_trash", "params": {"files": [fid], "confirmed": True, "account": account}}]

    # ── organize ──────────────────────────────────────────────────────────

    def gdrive_upload(self, local_path: str, folder: str = "", name: str = "", account: str = "") -> dict[str, Any]:
        """Upload a file from this PC to Google Drive. Needs Manage Drive.

        local_path: the file on this PC. folder: a Drive folder id or link (empty = My
        Drive). name: the name in Drive (default: the file's own name). account: needed
        when several are connected. Revert trashes the upload. Returns {ok, file}.
        """
        path = Path(_str(local_path)).expanduser()
        if not _str(local_path) or not path.is_file():
            raise ToolError(f"No file at {local_path!r} on this PC.")
        size = path.stat().st_size
        if size > self.max_upload_bytes:
            raise ToolError(f"{path.name} is {human_size(size)}, over the {human_size(self.max_upload_bytes)} limit.")
        email, c = self.manage(account)
        meta: dict[str, Any] = {"name": clean_name(name or path.name)}
        if _str(folder):
            meta["parents"] = [file_id(folder)]
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        f = c.upload(path, meta, mime, chunk=self.chunk)
        self.record(command="gdrive_upload", kind="drive_file", account=email,
                    summary=f"Uploaded “{f.get('name') or meta['name']}” ({human_size(size)})",
                    targets=[self._target(f)], inverse=self._trash_back(_str(f.get("id")), email))
        return {"ok": True, "file": file_row(f, email), "account": email,
                "message": f"Uploaded {meta['name']} ({human_size(size)})."}

    def gdrive_folder_create(self, name: str, parent: str = "", account: str = "") -> dict[str, Any]:
        """Make a new Drive folder. Needs Manage Drive.

        parent: the folder to put it in (id or link; empty = My Drive). Revert trashes it.
        Returns {ok, folder}.
        """
        email, c = self.manage(account)
        body: dict[str, Any] = {"name": clean_name(name), "mimeType": FOLDER_MIME}
        if _str(parent):
            body["parents"] = [file_id(parent)]
        f = c.create(body)
        self.record(command="gdrive_folder_create", kind="drive_folder", account=email,
                    summary=f"Made folder “{f.get('name') or body['name']}”",
                    targets=[self._target(f)], inverse=self._trash_back(_str(f.get("id")), email))
        return {"ok": True, "folder": file_row(f, email), "account": email,
                "message": f"Made folder {body['name']}."}

    def gdrive_move(self, file: Any, folder: str, account: str = "") -> dict[str, Any]:
        """Move files into a Drive folder. Needs Manage Drive.

        file: one id or link, or a list. folder: the destination folder id or link
        (``root`` = My Drive). Revert moves each file back where it was.
        Returns {ok, files, moved}.
        """
        ids = file_ids(file)
        if not ids:
            raise ToolError("file is required")
        if len(ids) > safety.MAX_ITEMS:
            raise ToolError(f"At most {safety.MAX_ITEMS} files per move.")
        dest = file_id(folder) if _str(folder) else ""
        if not dest:
            raise ToolError("folder is required (a Drive folder id or link, or 'root').")
        email, c = self.manage(account)
        moved, inverse, targets = [], [], []
        before: dict[str, list[str]] = {}
        for fid in ids:
            cur = c.get(fid, "id,name,parents")
            old = [p for p in cur.get("parents") or [] if p != dest]
            if dest in (cur.get("parents") or []) and not old:
                continue  # already there
            f = c.update(fid, add_parents=dest, remove_parents=",".join(old))
            moved.append(file_row(f, email))
            targets.append(self._target(f or cur))
            before[fid] = old
            if old:
                inverse.append({"command": "gdrive_move", "params": {"file": [fid], "folder": old[0], "account": email}})
        if moved:
            self.record(command="gdrive_move", kind="drive_file", account=email,
                        summary=f"Moved {safety.plural(len(moved), 'file')}", targets=targets, inverse=inverse,
                        before={"parents": before})
        return {"ok": True, "files": moved, "moved": len(moved), "account": email,
                "message": f"Moved {safety.plural(len(moved), 'file')}." if moved else "Already in that folder."}

    def gdrive_rename(self, file: str, name: str, account: str = "") -> dict[str, Any]:
        """Rename a Drive file or folder. Needs Manage Drive. Revert renames it back."""
        fid = file_id(file)
        new = clean_name(name)
        email, c = self.manage(account)
        old = _str(c.get(fid, "id,name").get("name"))
        if old == new:
            return {"ok": True, "file": {"id": fid, "name": new}, "changed": False, "account": email,
                    "message": f"It is already called {new}."}
        f = c.update(fid, {"name": new})
        self.record(command="gdrive_rename", kind="drive_file", account=email,
                    summary=f"Renamed “{old}” to “{new}”", targets=[self._target(f or {"id": fid, "name": new})],
                    inverse=[{"command": "gdrive_rename", "params": {"file": fid, "name": old, "account": email}}],
                    before={"name": old})
        return {"ok": True, "file": file_row(f, email), "changed": True, "account": email,
                "message": f"Renamed {old} to {new}."}

    def gdrive_copy(self, file: str, name: str = "", folder: str = "", account: str = "") -> dict[str, Any]:
        """Copy a Drive file. Needs Manage Drive.

        name: the copy's name (default "Copy of …" from Google). folder: where the copy
        goes (default: next to the original). Revert trashes the copy. Returns {ok, file}.
        """
        fid = file_id(file)
        email, c = self.manage(account)
        body: dict[str, Any] = {}
        if _str(name):
            body["name"] = clean_name(name)
        if _str(folder):
            body["parents"] = [file_id(folder)]
        f = c.copy(fid, body)
        self.record(command="gdrive_copy", kind="drive_file", account=email,
                    summary=f"Copied to “{f.get('name') or fid}”", targets=[self._target(f)],
                    inverse=self._trash_back(_str(f.get("id")), email), before={"source": fid})
        return {"ok": True, "file": file_row(f, email), "source": fid, "account": email,
                "message": f"Made a copy: {f.get('name') or 'done'}."}

    # ── trash ─────────────────────────────────────────────────────────────

    def gdrive_trash(self, files: Any, confirmed: bool = False, account: str = "") -> dict[str, Any]:
        """Move Drive files to the trash (Google keeps them 30 days). Needs Manage Drive.

        Without confirmed=true this only previews ("would trash 3 files: …"); show those
        exact files to the user with ducky_ask_user and call again with confirmed=true
        after they say yes. At most 50 per call. Never deletes for good; Revert untrashes.
        """
        ids = file_ids(files)
        if len(ids) > safety.MAX_ITEMS:
            return safety.guarded_remove(self.api, command="gdrive_trash", kind="drive_file", noun="file",
                                         items=ids, confirmed=False, apply=lambda _ids: None, inverse=None)
        email, c = self.manage(account)
        items = []
        for fid in ids:
            try:
                items.append({"id": fid, "label": _str(c.get(fid, "id,name").get("name")) or fid})
            except DriveError as exc:
                if exc.status == 404:
                    raise ToolError(f"No Drive file {fid} in {email}. Nothing was changed.") from exc
                raise

        def apply(picked: list[str]) -> dict[str, Any]:
            for fid in picked:
                c.update(fid, {"trashed": True})
            return {"trashed": list(picked)}

        out = safety.guarded_remove(
            self.api, command="gdrive_trash", kind="drive_file", noun="file", items=items,
            confirmed=bool(confirmed), apply=apply, account=email, params={"account": email},
            inverse=safety.untrash_inverse("gdrive_untrash", ids_param="files", account=email),
        )
        out["account"] = email
        return out

    def gdrive_untrash(self, files: Any, account: str = "") -> dict[str, Any]:
        """Take Drive files back out of the trash. Needs Manage Drive. Revert trashes them again."""
        ids = file_ids(files)
        if not ids:
            raise ToolError("files is required")
        if len(ids) > safety.MAX_ITEMS:
            raise ToolError(f"At most {safety.MAX_ITEMS} files per call.")
        email, c = self.manage(account)
        restored = [file_row(c.update(fid, {"trashed": False}) or {"id": fid}, email) for fid in ids]
        self.record(command="gdrive_untrash", kind="drive_file", account=email,
                    summary=f"Restored {safety.plural(len(restored), 'file')} from the trash",
                    targets=[self._target(f) for f in restored],
                    inverse=[{"command": "gdrive_trash", "params": {"files": ids, "confirmed": True, "account": email}}])
        return {"ok": True, "files": restored, "restored": len(restored), "account": email,
                "message": f"Restored {safety.plural(len(restored), 'file')} from the trash."}

    # ── share & usage ─────────────────────────────────────────────────────

    def gdrive_share(self, file: str, email: str, role: str = "reader", notify: bool = True, message: str = "",
                     account: str = "") -> dict[str, Any]:
        """Share a Drive file or folder with one person. Ducky asks the person first. Needs Manage Drive.

        email: who gets access. role: reader (view), commenter or writer (edit); never
        owner. notify: Google emails them (message: an optional note). Revert can't take
        access back: remove it in Drive if needed. Returns {ok, permission}.
        """
        fid = file_id(file)
        who = _str(email)
        if not _EMAIL_RE.match(who):
            raise ToolError(f"{email!r} is not an email address.")
        r = _str(role).lower() or "reader"
        if r not in SHARE_ROLES:
            raise ToolError(f"role must be one of {', '.join(SHARE_ROLES)} (owner transfer isn't offered).")
        acct, c = self.manage(account)
        name = _str(c.get(fid, "id,name").get("name")) or fid
        perm = c.share(fid, {"type": "user", "role": r, "emailAddress": who}, notify=bool(notify),
                       message=_str(message)[:1000])
        self.record(command="gdrive_share", kind="drive_share", account=acct,
                    summary=f"Shared “{name}” with {who} ({r})",
                    targets=[{"kind": "drive_file", "id": fid, "label": name, "path": fid}],
                    before={"permission_id": _str(perm.get("id")), "email": who, "role": r})
        return {"ok": True, "file": {"id": fid, "name": name}, "account": acct,
                "permission": {"id": _str(perm.get("id")), "role": r, "email": who},
                "message": f"Shared {name} with {who} as {r}."}

    def gdrive_storage_usage(self, account: str = "") -> dict[str, Any]:
        """How full each Google Drive is: used, limit, in Drive, in trash. account: empty = every account."""
        rows = [self._account(account)] if _str(account) else self.accounts()
        if not rows:
            raise ToolError("No Google account is connected: sign in with google_connect first.")
        usage = []
        for r in rows:
            q = (self.client(r["email"]).about() or {}).get("storageQuota") or {}
            used, limit = int(q.get("usage") or 0), int(q.get("limit") or 0)
            usage.append({
                "account": r["email"],
                "usage": used,
                "limit": limit or None,
                "usage_in_drive": int(q.get("usageInDrive") or 0),
                "usage_in_trash": int(q.get("usageInDriveTrash") or 0),
                "percent": round(100.0 * used / limit, 1) if limit else None,
                "text": f"{human_size(used)} of {human_size(limit)}" if limit else f"{human_size(used)} (no limit)",
            })
        return {"ok": True, "accounts": usage, "count": len(usage)}


TOOL_NAMES = (
    "gdrive_upload",
    "gdrive_folder_create",
    "gdrive_move",
    "gdrive_rename",
    "gdrive_copy",
    "gdrive_trash",
    "gdrive_untrash",
    "gdrive_share",
    "gdrive_storage_usage",
)


def _safe(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """The tool / panel form of a method: errors become ``{ok: False, error}``."""

    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("pretty", None)
        try:
            return method(*args, **kwargs)
        except DriveError as exc:
            out: dict[str, Any] = {"ok": False, "error": str(exc)}
            if exc.status == 403 and "insufficient" in (exc.reason + str(exc)).lower():
                out["error"] += " (Drive is read-only for this account: offer google_connect(manage_drive=true).)"
            return out
        except (ToolError, ValueError) as exc:
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}
        except Exception as exc:  # noqa: BLE001 - a tool answers, never raises across the bridge
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

    return call


def register(api: Any, *, accounts: AccountsFn, token_for: TokenFn, http: HttpFn | None = None,
             **options: Any) -> DriveTools:
    """Add every Drive manage tool (``@api.tool(listener=False)``) and its panel RPC of the same name.

    accounts(): rows {email, drive, manage_drive} (tools_account.AccountTools.rows).
    token_for(email): that account's access token. http: the request function (tests).
    """
    tools = DriveTools(api, accounts=accounts, token_for=token_for, http=http, **options)
    for name in TOOL_NAMES:
        fn = _safe(getattr(tools, name))
        api.tool(listener=False)(fn)
        api.register_panel_rpc(name, fn)
    return tools
