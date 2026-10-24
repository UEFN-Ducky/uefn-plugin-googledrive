"""Chat display for Gmail and Drive read results, and ``google_open``.

Read tools put a short Markdown table in their result's ``display`` field
(``with_display``) so the chat shows what was found without the agent retyping it:

  - Gmail: dot · from · subject (with its snippet) · date · [Open]
  - Drive: file · folder · size · [Open], then thumbnails for pictures

Each row's [Open](plugin:googledrive/main?view=gmail&id=…) link opens the Google tab on
that item. Only Gmail snippets are shown, never message bodies, and thumbnails are only
local media URLs (``api.media_url``) because the chat loads nothing from outside the app.

``google_open(api, view, item_id, query, account)`` opens the Google tab on screen
(ducky_ui_navigate) and leaves the request for the panel, which takes it with the
``google_open_pending`` panel call. ``register(api)`` adds both.

This module imports nothing from the rest of the package.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlparse

PLUGIN_ID = "googledrive"
PANEL_ID = "main"
VIEWS = ("gmail", "drive")
MAX_ROWS = 25
SNIPPET_CHARS = 90
CELL_CHARS = 60
PENDING_TTL_S = 120.0

UNREAD_DOT = "●"
READ_DOT = "○"


# --------------------------------------------------------------------------- #
# Links and cells.
# --------------------------------------------------------------------------- #


def open_link(view: str, item_id: str = "", *, query: str = "", account: str = "") -> str:
    """``plugin:googledrive/main?view=gmail&id=…`` — the target of an [Open] link."""
    parts = [f"view={quote(view, safe='')}"]
    if item_id:
        parts.append(f"id={quote(str(item_id), safe='')}")
    if query:
        parts.append(f"q={quote(query, safe='')}")
    if account:
        parts.append(f"account={quote(account, safe='')}")
    return f"plugin:{PLUGIN_ID}/{PANEL_ID}?" + "&".join(parts)


_ESCAPE = str.maketrans({c: "\\" + c for c in "\\|[]*_`<>#"})


def cell(text: Any, limit: int = CELL_CHARS) -> str:
    """One table cell: one line, Markdown escaped, cut to ``limit`` characters."""
    s = " ".join(str(text if text is not None else "").split())
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s.translate(_ESCAPE)


def _when(value: Any, today: date | None = None) -> str:
    """``Oct 24`` this year, ``2025-10-24`` before; Gmail ms, RFC 2822 or ISO in."""
    dt: datetime | None = None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            v = value.strip()
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(v)
    except (TypeError, ValueError, OverflowError):
        dt = None
    if dt is None:
        return ""
    today = today or date.today()
    return f"{dt:%b} {dt.day}" if dt.year == today.year else f"{dt:%Y-%m-%d}"


def _size(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# --------------------------------------------------------------------------- #
# Gmail.
# --------------------------------------------------------------------------- #


def _sender(raw: Any) -> str:
    name, addr = parseaddr(str(raw or ""))
    return name or addr or str(raw or "")


def _unread(msg: dict[str, Any]) -> bool:
    if "unread" in msg:
        return bool(msg["unread"])
    return "UNREAD" in (msg.get("labelIds") or msg.get("labels") or [])


def messages_markdown(
    messages: Iterable[dict[str, Any]],
    *,
    title: str = "",
    account: str = "",
    query: str = "",
    total: int | None = None,
    limit: int = MAX_ROWS,
    today: date | None = None,
) -> str:
    """A short table of messages: dot · from · subject — snippet · date · [Open].

    Reads ``id``, ``from``, ``subject``, ``snippet``, ``date`` (or ``internalDate``),
    ``unread`` (or ``UNREAD`` in ``labelIds``) and ``account``. Bodies are never shown.
    """
    rows = [m for m in messages if isinstance(m, dict)]
    shown = rows[: max(0, limit)]
    count = total if total is not None else len(rows)
    head = f"**{_plural(count, 'message')}**" + (f" · {cell(title)}" if title else "") + (f" · {cell(account)}" if account else "")
    if not rows:
        return head + "\n\nNo messages found."
    lines = [head, "", "| | From | Subject | Date | |", "|---|---|---|---|---|"]
    for m in shown:
        subject = cell(m.get("subject") or "(no subject)")
        snippet = cell(m.get("snippet") or "", SNIPPET_CHARS)
        subj = f"**{subject}**" if _unread(m) else subject
        if snippet:
            subj += f" — {snippet}"
        link = open_link("gmail", str(m.get("id") or ""), account=str(m.get("account") or account))
        lines.append(
            f"| {UNREAD_DOT if _unread(m) else READ_DOT} | {cell(_sender(m.get('from')), 32)} | {subj} "
            f"| {_when(m.get('date') or m.get('internalDate'), today)} | [Open]({link}) |"
        )
    if count > len(shown):
        more = open_link("gmail", query=query, account=account)
        lines += ["", f"Showing {len(shown)} of {count}. [Open all in Gmail]({more})"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Drive.
# --------------------------------------------------------------------------- #

FOLDER_MIME = "application/vnd.google-apps.folder"


def _local_thumb(f: dict[str, Any]) -> str:
    """Only app-served thumbnails; Google's remote ``thumbnailLink`` never loads in chat."""
    url = str(f.get("thumbnail_url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.hostname not in ("127.0.0.1", "localhost"):
        return ""
    return url


def files_markdown(
    files: Iterable[dict[str, Any]],
    *,
    title: str = "",
    account: str = "",
    query: str = "",
    total: int | None = None,
    limit: int = MAX_ROWS,
    thumbnail: Callable[[dict[str, Any]], str] | None = None,
) -> str:
    """A short table of Drive files: file · folder · size · [Open], then picture thumbnails.

    Reads ``id``, ``name``, ``mimeType``, ``size``, ``folder`` (or ``folder_name``) and
    ``account``. ``thumbnail(file)`` returns a local media URL (``api.media_url``) for a
    picture; by default a file's own ``thumbnail_url`` is used when it is app-served.
    """
    rows = [f for f in files if isinstance(f, dict)]
    shown = rows[: max(0, limit)]
    count = total if total is not None else len(rows)
    head = f"**{_plural(count, 'file')}**" + (f" · {cell(title)}" if title else "") + (f" · {cell(account)}" if account else "")
    if not rows:
        return head + "\n\nNo files found."
    thumb_of = thumbnail or _local_thumb
    lines = [head, "", "| File | Folder | Size | |", "|---|---|---|---|"]
    pictures: list[str] = []
    for f in shown:
        mime = str(f.get("mimeType") or "")
        is_folder = mime == FOLDER_MIME
        name = cell(f.get("name") or "(untitled)") + ("/" if is_folder else "")
        link = open_link("drive", str(f.get("id") or ""), account=str(f.get("account") or account))
        folder = cell(f.get("folder") or f.get("folder_name") or "—", 40)
        size = "—" if is_folder else _size(f.get("size"))
        lines.append(f"| {name} | {folder} | {size} | [Open]({link}) |")
        if mime.startswith("image/"):
            url = thumb_of(f)
            if url:
                pictures.append(f"[![{cell(f.get('name') or 'picture', 40)}]({url})]({link})")
    if pictures:
        lines += ["", " ".join(pictures)]
    if count > len(shown):
        more = open_link("drive", query=query, account=account)
        lines += ["", f"Showing {len(shown)} of {count}. [Open all in Drive]({more})"]
    return "\n".join(lines)


def with_display(result: dict[str, Any], markdown: str) -> dict[str, Any]:
    """Put the Markdown on a tool result as ``display`` and return the result."""
    result["display"] = markdown
    return result


# --------------------------------------------------------------------------- #
# google_open: put the Google tab on screen at a view / item.
# --------------------------------------------------------------------------- #

_pending_lock = threading.Lock()
_pending: dict[str, Any] | None = None


def take_pending_open() -> dict[str, Any] | None:
    """The last open request, once (the panel calls this as ``google_open_pending``)."""
    global _pending
    with _pending_lock:
        req, _pending = _pending, None
    if req and time.time() - float(req.get("at") or 0) > PENDING_TTL_S:
        return None
    return req


def google_open(api: Any, view: str, item_id: str = "", query: str = "", account: str = "") -> dict[str, Any]:
    """Open the Google tab on ``view`` (gmail | drive), optionally on one item or a search."""
    global _pending
    v = str(view or "").strip().lower()
    if v not in VIEWS:
        return {"ok": False, "error": f"unknown view: {view!r} (use one of {', '.join(VIEWS)})"}
    req = {
        "view": v,
        "id": str(item_id or "").strip(),
        "query": str(query or "").strip(),
        "account": str(account or "").strip(),
        "at": time.time(),
    }
    with _pending_lock:
        _pending = req
    navigated = False
    error = ""
    if api is not None:
        try:
            out = api.call_tool("ducky_ui_navigate", {"route": "plugin_panel", "item_id": f"{PLUGIN_ID}:{PANEL_ID}"})
            navigated = not (isinstance(out, dict) and (out.get("ok") is False or out.get("error")))
            if not navigated and isinstance(out, dict):
                error = str(out.get("error") or "")
        except Exception as exc:  # noqa: BLE001 - no panel open is a normal answer
            error = str(exc) or exc.__class__.__name__
    label = "Gmail" if v == "gmail" else "Drive"
    out: dict[str, Any] = {
        "ok": True,
        "view": v,
        "item_id": req["id"],
        "query": req["query"],
        "account": req["account"],
        "navigated": navigated,
        "link": open_link(v, req["id"], query=req["query"], account=req["account"]),
    }
    if navigated:
        out["display"] = f"Opened **{label}** in the Google tab."
    else:
        out["display"] = f"[Open {label}]({out['link']})"
        if error:
            out["note"] = f"Ducky's window did not open the tab: {error}"
    return out


_open_tab = google_open  # the tool below shadows the name inside register()


def register(api: Any) -> None:
    """Add the ``google_open`` tool and the panel's ``google_open_pending`` call."""

    @api.tool(listener=False)
    def google_open(view: str, item_id: str = "", query: str = "", account: str = "", pretty: bool = False) -> str:
        """Open the Google Workspace tab on screen at Gmail or Drive.

        view: gmail | drive. item_id: a message or file id to open. query: a Gmail search
        or Drive name filter to show. account: the Google account (email) when several are
        connected. Returns {ok, view, navigated, link, display}.
        """
        return json.dumps(_open_tab(api, view, item_id, query, account), indent=2 if pretty else None)

    api.register_panel_rpc("google_open_pending", lambda *_a, **_k: take_pending_open())
