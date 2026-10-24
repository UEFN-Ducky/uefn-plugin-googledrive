"""Thin Gmail REST client (users/me) over the host's ``api.http_json``.

Stdlib only and no package-relative imports, so the host can load it next to
``auth.py`` / ``drive.py`` and tests can load it straight from its file.

    client = GmailClient(api.http_json, auth.access_token_getter(account))
    page = client.list_messages(q="is:unread", max=20)

``http`` has ``api.http_json``'s shape: ``http(method, url, *, headers,
json_body, timeout)``; its errors carry ``.status`` and ``.detail``.
``token`` returns a current access token (refresh is auth.py's job).
Retries 429, 5xx and Gmail's rate-limit 403s with exponential backoff.
Nothing personal is logged: only method, path kind and status.
"""

from __future__ import annotations

import base64
import logging
import random
import time
import urllib.parse
from typing import Any, Callable

log = logging.getLogger("uefn.plugin.googledrive.gmail")

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
PAGE_MAX = 500  # Gmail's maxResults ceiling for messages/threads/drafts/history
FORMATS = ("full", "metadata", "minimal", "raw")
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}

HttpFn = Callable[..., Any]


class GmailError(Exception):
    """A Gmail call failed. ``status`` is the HTTP status (None for network errors)."""

    def __init__(self, message: str, *, status: int | None = None, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


class HistoryExpired(GmailError):
    """history.list 404: the start historyId is too old; do a full sync."""


def b64url_encode(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    s = (data or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _error_info(exc: Exception) -> tuple[int | None, str, str]:
    """(status, reason, message) from an http_json error."""
    status = getattr(exc, "status", None)
    detail = getattr(exc, "detail", None)
    reason, message = "", str(exc) or exc.__class__.__name__
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        err = detail["error"]
        message = str(err.get("message") or message)
        errors = err.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
        reason = reason or str(err.get("status") or "")
    return status, reason, message


def _retryable(status: int | None, reason: str) -> bool:
    if status == 429 or (status is not None and 500 <= status < 600):
        return True
    return status == 403 and reason in _RATE_REASONS


def _ids(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return [str(v) for v in values if str(v).strip()]


def _raw(raw: bytes | str) -> str:
    """A message for the API: bytes → base64url; a str is taken as already encoded."""
    return b64url_encode(raw) if isinstance(raw, (bytes, bytearray)) else str(raw)


class GmailClient:
    def __init__(
        self,
        http: HttpFn,
        token: Callable[[], str],
        *,
        max_retries: int = 4,
        backoff_s: float = 1.0,
        timeout: float = 60,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = http
        self._token = token
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = backoff_s
        self.timeout = timeout
        self._sleep = sleep

    # ── transport ─────────────────────────────────────────────────────────

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        query = {k: v for k, v in (params or {}).items() if v not in (None, "", [], ())}
        url = f"{API_BASE}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        attempt = 0
        while True:
            token = self._token() or ""
            if not token:
                raise GmailError("Not signed in to Gmail — turn on Gmail first", status=401)
            try:
                return self._http(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    json_body=body,
                    timeout=self.timeout,
                )
            except Exception as exc:
                status, reason, message = _error_info(exc)
                if attempt < self.max_retries and _retryable(status, reason):
                    delay = self.backoff_s * (2**attempt) + random.uniform(0, self.backoff_s / 2)
                    log.info("gmail %s %s → %s, retry in %.1fs", method, path.split("/")[0], status, delay)
                    self._sleep(delay)
                    attempt += 1
                    continue
                raise GmailError(message, status=status, reason=reason) from exc

    def _page(
        self,
        path: str,
        key: str,
        params: dict[str, Any],
        max: int,
        page_token: str = "",
    ) -> dict[str, Any]:
        """Collect up to ``max`` items of ``key`` across pages."""
        want = max if max and max > 0 else PAGE_MAX
        items: list[Any] = []
        token = page_token or ""
        last: dict[str, Any] = {}
        while True:
            q = dict(params, maxResults=min(PAGE_MAX, want - len(items)), pageToken=token)
            last = self._call("GET", path, q) or {}
            items.extend(last.get(key) or [])
            token = str(last.get("nextPageToken") or "")
            if not token or len(items) >= want:
                break
        out: dict[str, Any] = {key: items[:want], "nextPageToken": token}
        if "resultSizeEstimate" in last:
            out["resultSizeEstimate"] = last["resultSizeEstimate"]
        return out

    # ── profile ───────────────────────────────────────────────────────────

    def get_profile(self) -> dict[str, Any]:
        """{emailAddress, messagesTotal, threadsTotal, historyId}."""
        return self._call("GET", "profile")

    # ── messages ──────────────────────────────────────────────────────────

    def list_messages(
        self,
        q: str = "",
        max: int = 20,
        *,
        label_ids: Any = None,
        page_token: str = "",
        include_spam_trash: bool = False,
    ) -> dict[str, Any]:
        """{ids, messages: [{id, threadId}], nextPageToken, resultSizeEstimate}."""
        params: dict[str, Any] = {"q": q, "labelIds": _ids(label_ids)}
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
        out = self._page("messages", "messages", params, max, page_token)
        out["ids"] = [m.get("id") for m in out["messages"]]
        return out

    def get_message(
        self, message_id: str, format: str = "full", *, metadata_headers: Any = None
    ) -> dict[str, Any]:
        params = {"format": _format(format)}
        if format == "metadata":
            params["metadataHeaders"] = _ids(metadata_headers)
        return self._call("GET", f"messages/{_seg(message_id)}", params)

    def modify_message(self, message_id: str, add: Any = None, remove: Any = None) -> dict[str, Any]:
        return self._call(
            "POST",
            f"messages/{_seg(message_id)}/modify",
            body={"addLabelIds": _ids(add), "removeLabelIds": _ids(remove)},
        )

    def batch_modify(self, message_ids: Any, add: Any = None, remove: Any = None) -> dict[str, Any]:
        ids = _ids(message_ids)
        for i in range(0, len(ids), 1000):  # API limit per call
            self._call(
                "POST",
                "messages/batchModify",
                body={"ids": ids[i : i + 1000], "addLabelIds": _ids(add), "removeLabelIds": _ids(remove)},
            )
        return {"ok": True, "count": len(ids)}

    def trash_message(self, message_id: str) -> dict[str, Any]:
        return self._call("POST", f"messages/{_seg(message_id)}/trash")

    def untrash_message(self, message_id: str) -> dict[str, Any]:
        return self._call("POST", f"messages/{_seg(message_id)}/untrash")

    def send_raw(self, raw: bytes | str, thread_id: str = "") -> dict[str, Any]:
        """Send an RFC 2822 message (bytes, or already base64url). → {id, threadId, labelIds}."""
        body: dict[str, Any] = {"raw": _raw(raw)}
        if thread_id:
            body["threadId"] = thread_id
        return self._call("POST", "messages/send", body=body)

    def get_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]:
        """{size, data: bytes}."""
        res = self._call("GET", f"messages/{_seg(message_id)}/attachments/{_seg(attachment_id)}") or {}
        return {"size": int(res.get("size") or 0), "data": b64url_decode(str(res.get("data") or ""))}

    # ── threads ───────────────────────────────────────────────────────────

    def list_threads(
        self,
        q: str = "",
        max: int = 20,
        *,
        label_ids: Any = None,
        page_token: str = "",
        include_spam_trash: bool = False,
    ) -> dict[str, Any]:
        """{ids, threads: [{id, snippet, historyId}], nextPageToken, resultSizeEstimate}."""
        params: dict[str, Any] = {"q": q, "labelIds": _ids(label_ids)}
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
        out = self._page("threads", "threads", params, max, page_token)
        out["ids"] = [t.get("id") for t in out["threads"]]
        return out

    def get_thread(self, thread_id: str, format: str = "full", *, metadata_headers: Any = None) -> dict[str, Any]:
        params = {"format": _format(format, allow_raw=False)}
        if format == "metadata":
            params["metadataHeaders"] = _ids(metadata_headers)
        return self._call("GET", f"threads/{_seg(thread_id)}", params)

    def modify_thread(self, thread_id: str, add: Any = None, remove: Any = None) -> dict[str, Any]:
        return self._call(
            "POST",
            f"threads/{_seg(thread_id)}/modify",
            body={"addLabelIds": _ids(add), "removeLabelIds": _ids(remove)},
        )

    def trash_thread(self, thread_id: str) -> dict[str, Any]:
        return self._call("POST", f"threads/{_seg(thread_id)}/trash")

    def untrash_thread(self, thread_id: str) -> dict[str, Any]:
        return self._call("POST", f"threads/{_seg(thread_id)}/untrash")

    # ── labels ────────────────────────────────────────────────────────────

    def list_labels(self) -> list[dict[str, Any]]:
        return list((self._call("GET", "labels") or {}).get("labels") or [])

    def get_label(self, label_id: str) -> dict[str, Any]:
        return self._call("GET", f"labels/{_seg(label_id)}")

    def create_label(
        self,
        name: str,
        *,
        label_list_visibility: str = "labelShow",
        message_list_visibility: str = "show",
        color: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not (name or "").strip():
            raise GmailError("label name is required")
        body: dict[str, Any] = {
            "name": name.strip(),
            "labelListVisibility": label_list_visibility,
            "messageListVisibility": message_list_visibility,
        }
        if color:
            body["color"] = color
        return self._call("POST", "labels", body=body)

    def update_label(self, label_id: str, **fields: Any) -> dict[str, Any]:
        """PATCH: name, labelListVisibility, messageListVisibility, color."""
        body = {k: v for k, v in fields.items() if v is not None}
        return self._call("PATCH", f"labels/{_seg(label_id)}", body=body)

    def delete_label(self, label_id: str) -> dict[str, Any]:
        self._call("DELETE", f"labels/{_seg(label_id)}")
        return {"ok": True, "id": label_id}

    # ── drafts ────────────────────────────────────────────────────────────

    def list_drafts(self, q: str = "", max: int = 20, *, page_token: str = "") -> dict[str, Any]:
        """{ids, drafts: [{id, message: {id, threadId}}], nextPageToken}."""
        out = self._page("drafts", "drafts", {"q": q}, max, page_token)
        out["ids"] = [d.get("id") for d in out["drafts"]]
        return out

    def get_draft(self, draft_id: str, format: str = "full") -> dict[str, Any]:
        return self._call("GET", f"drafts/{_seg(draft_id)}", {"format": _format(format)})

    def create_draft(self, raw: bytes | str, thread_id: str = "") -> dict[str, Any]:
        return self._call("POST", "drafts", body={"message": _message(raw, thread_id)})

    def update_draft(self, draft_id: str, raw: bytes | str, thread_id: str = "") -> dict[str, Any]:
        return self._call(
            "PUT",
            f"drafts/{_seg(draft_id)}",
            body={"id": draft_id, "message": _message(raw, thread_id)},
        )

    def send_draft(self, draft_id: str) -> dict[str, Any]:
        """→ the sent message {id, threadId, labelIds}."""
        return self._call("POST", "drafts/send", body={"id": draft_id})

    def delete_draft(self, draft_id: str) -> dict[str, Any]:
        self._call("DELETE", f"drafts/{_seg(draft_id)}")
        return {"ok": True, "id": draft_id}

    # ── history ───────────────────────────────────────────────────────────

    def list_history(
        self,
        start_history_id: str | int,
        *,
        history_types: Any = None,
        label_id: str = "",
        max: int = 0,
    ) -> dict[str, Any]:
        """All changes since ``start_history_id``: {history, historyId, nextPageToken}.

        ``max`` 0 reads every page. Raises HistoryExpired when Gmail no longer
        has that id (404) — the caller falls back to a full sync.
        """
        params: dict[str, Any] = {
            "startHistoryId": str(start_history_id),
            "historyTypes": _ids(history_types),
            "labelId": label_id,
        }
        want = max if max and max > 0 else 0
        items: list[Any] = []
        token = ""
        history_id = str(start_history_id)
        while True:
            q = dict(params, maxResults=PAGE_MAX if not want else min(PAGE_MAX, want - len(items)))
            q["pageToken"] = token
            try:
                res = self._call("GET", "history", q) or {}
            except GmailError as exc:
                if exc.status == 404:
                    raise HistoryExpired(
                        "Gmail history expired; do a full sync", status=404, reason=exc.reason
                    ) from exc
                raise
            items.extend(res.get("history") or [])
            history_id = str(res.get("historyId") or history_id)
            token = str(res.get("nextPageToken") or "")
            if not token or (want and len(items) >= want):
                break
        return {"history": items[:want] if want else items, "historyId": history_id, "nextPageToken": token}


def _seg(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        raise GmailError("id is required")
    return urllib.parse.quote(s, safe="")


def _format(value: str, *, allow_raw: bool = True) -> str:
    fmt = (value or "full").strip().lower()
    if fmt not in FORMATS or (fmt == "raw" and not allow_raw):
        raise GmailError(f"format must be one of {', '.join(f for f in FORMATS if allow_raw or f != 'raw')}")
    return fmt


def _message(raw: bytes | str, thread_id: str) -> dict[str, Any]:
    msg: dict[str, Any] = {"raw": _raw(raw)}
    if thread_id:
        msg["threadId"] = thread_id
    return msg
