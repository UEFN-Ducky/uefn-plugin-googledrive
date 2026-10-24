"""Local Gmail cache: message metadata per account, kept in sync with history.list.

Stdlib only and no package-relative imports, like ``gmail.py``.

    store = MailStore(api, "me@example.com", client)   # client: gmail.GmailClient
    res = store.sync()      # first call: full sync of the last ``days`` days
    res = store.sync()      # then: only what changed since the stored historyId

Every doc is written ``sensitive=True`` (Personal, never synced) and each message
is also an ``api.search`` record so the AI can find mail by meaning. Doc keys hold
a hash of the address, never the address itself. Nothing personal is logged.

Docs per account (``<acct>`` = first 16 hex of sha256(lowercased address)):
    gmail.<acct>.index      {account, historyId, ids, synced_at}
    gmail.<acct>.m.<id>     {id, threadId, from, subject, snippet, date, labels, unread}
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable

log = logging.getLogger("uefn.plugin.googledrive.mail_store")

DEFAULT_DAYS = 30
DEFAULT_LIMIT = 2000
META_HEADERS = ("From", "Subject", "Date")
HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")
GONE_LABELS = {"TRASH", "SPAM"}  # a full sync never lists these, so the cache drops them
_ID_RE = re.compile(r"^[a-z0-9]{1,64}$")


def account_key(account: str) -> str:
    return hashlib.sha256((account or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def _header(msg: dict[str, Any], name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if str(h.get("name") or "").lower() == name.lower():
            return str(h.get("value") or "")
    return ""


def _date_ms(msg: dict[str, Any]) -> int:
    try:
        return int(msg.get("internalDate") or 0)
    except (TypeError, ValueError):
        pass
    try:
        return int(parsedate_to_datetime(_header(msg, "Date")).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def summarize(msg: dict[str, Any]) -> dict[str, Any]:
    """Gmail metadata message → the cached record."""
    labels = [str(x) for x in msg.get("labelIds") or []]
    return {
        "id": str(msg.get("id") or ""),
        "threadId": str(msg.get("threadId") or ""),
        "from": _header(msg, "From"),
        "subject": _header(msg, "Subject"),
        "snippet": str(msg.get("snippet") or ""),
        "date": _date_ms(msg),
        "labels": labels,
        "unread": "UNREAD" in labels,
    }


class MailStore:
    def __init__(
        self,
        api: Any,
        account: str,
        client: Any,
        *,
        days: int = DEFAULT_DAYS,
        limit: int = DEFAULT_LIMIT,
        now: Callable[[], float] = time.time,
    ):
        self._data = api.data
        self._search = getattr(api, "search", None)
        self.account = (account or "").strip()
        self.acct = account_key(self.account)
        self.client = client
        self.days = max(1, int(days))
        self.limit = max(1, int(limit))
        self._now = now

    # ── keys ──────────────────────────────────────────────────────────────

    def _index_key(self) -> str:
        return f"gmail.{self.acct}.index"

    def _msg_key(self, message_id: str) -> str:
        return f"gmail.{self.acct}.m.{message_id}"

    def _record_id(self, message_id: str) -> str:
        return f"gmail.{self.acct}.{message_id}"

    @staticmethod
    def _valid_id(message_id: Any) -> str:
        mid = str(message_id or "").strip().lower()
        return mid if _ID_RE.match(mid) else ""

    # ── reads ─────────────────────────────────────────────────────────────

    def index(self) -> dict[str, Any]:
        doc = self._data.get(self._index_key(), None, sensitive=True)
        return doc if isinstance(doc, dict) else {}

    def history_id(self) -> str:
        return str(self.index().get("historyId") or "")

    def ids(self) -> list[str]:
        return list(self.index().get("ids") or [])

    def get(self, message_id: str) -> dict[str, Any] | None:
        mid = self._valid_id(message_id)
        if not mid:
            return None
        doc = self._data.get(self._msg_key(mid), None, sensitive=True)
        return doc if isinstance(doc, dict) else None

    def messages(self, *, unread_only: bool = False, label: str = "", limit: int = 0) -> list[dict[str, Any]]:
        """Cached messages, newest first, each with ``account`` set."""
        out = []
        for mid in self.ids():
            m = self.get(mid)
            if not m or (unread_only and not m.get("unread")) or (label and label not in m.get("labels", [])):
                continue
            out.append(dict(m, account=self.account))
        out.sort(key=lambda m: m.get("date") or 0, reverse=True)
        return out[:limit] if limit and limit > 0 else out

    # ── writes ────────────────────────────────────────────────────────────

    def _save_index(self, ids: list[str], history_id: str) -> None:
        self._data.put(
            self._index_key(),
            {"account": self.account, "historyId": history_id, "ids": ids, "synced_at": int(self._now())},
            sensitive=True,
        )

    def _put(self, rec: dict[str, Any]) -> None:
        self._data.put(self._msg_key(rec["id"]), rec, sensitive=True)
        if self._search is None:
            return
        try:
            self._search.upsert(
                self._record_id(rec["id"]),
                rec.get("subject") or "(no subject)",
                f"From: {rec.get('from', '')}\nSubject: {rec.get('subject', '')}\n{rec.get('snippet', '')}",
                {"account": self.account, "id": rec["id"], "threadId": rec.get("threadId", ""), "date": rec.get("date", 0)},
            )
        except Exception as exc:  # search is a nice-to-have; the cache still holds the message
            log.info("search upsert failed: %s", exc.__class__.__name__)

    def _drop(self, mid: str) -> None:
        try:
            self._data.delete(self._msg_key(mid))
        except Exception as exc:
            log.info("cache delete failed: %s", exc.__class__.__name__)
        if self._search is not None:
            try:
                self._search.delete(self._record_id(mid))
            except Exception as exc:
                log.info("search delete failed: %s", exc.__class__.__name__)

    def _fetch(self, mid: str) -> dict[str, Any] | None:
        from_api = self.client.get_message(mid, "metadata", metadata_headers=list(META_HEADERS))
        rec = summarize(from_api or {})
        rec["id"] = self._valid_id(rec["id"]) or mid
        return rec

    def clear(self) -> int:
        """Forget this account's cache (sign-out)."""
        ids = self.ids()
        for mid in ids:
            self._drop(mid)
        try:
            self._data.delete(self._index_key())
        except Exception:
            pass
        return len(ids)

    # ── sync ──────────────────────────────────────────────────────────────

    def sync(self) -> dict[str, Any]:
        """Incremental when a historyId is stored, else (or when it expired) full.

        → {mode, added, updated, deleted, historyId}. ``added`` lists only ids that
        were not in the cache before this call.
        """
        start = self.history_id()
        if start:
            try:
                return self._incremental(start)
            except Exception as exc:
                if getattr(exc, "status", None) != 404 and exc.__class__.__name__ != "HistoryExpired":
                    raise
                log.info("gmail history expired; full sync")
        return self.full_sync()

    def full_sync(self) -> dict[str, Any]:
        before = set(self.ids())
        # The profile's historyId is read first so a change made while listing is
        # picked up by the next incremental sync instead of being lost.
        history_id = str((self.client.get_profile() or {}).get("historyId") or "")
        page = self.client.list_messages(q=f"newer_than:{self.days}d", max=self.limit)
        ids: list[str] = []
        added, updated = [], []
        for ref in page.get("messages") or []:
            mid = self._valid_id((ref or {}).get("id"))
            if not mid or mid in ids:
                continue
            rec = self._fetch(mid)
            if not rec or GONE_LABELS & set(rec["labels"]):
                continue
            if self.get(mid) != rec:
                self._put(rec)
            ids.append(mid)
            (updated if mid in before else added).append(mid)
        deleted = [mid for mid in before if mid not in set(ids)]
        for mid in deleted:
            self._drop(mid)
        self._save_index(ids, history_id)
        return {"mode": "full", "added": added, "updated": updated, "deleted": deleted, "historyId": history_id}

    def _incremental(self, start: str) -> dict[str, Any]:
        res = self.client.list_history(start, history_types=list(HISTORY_TYPES))
        ids = self.ids()
        known = set(ids)
        new: list[str] = []
        relabeled: dict[str, list[str]] = {}
        gone: set[str] = set()
        for h in res.get("history") or []:
            for e in h.get("messagesAdded") or []:
                mid = self._valid_id((e.get("message") or {}).get("id"))
                if mid and mid not in known and mid not in new:
                    new.append(mid)
                gone.discard(mid)
            for e in h.get("messagesDeleted") or []:
                mid = self._valid_id((e.get("message") or {}).get("id"))
                if mid:
                    gone.add(mid)
            for kind in ("labelsAdded", "labelsRemoved"):
                for e in h.get(kind) or []:
                    m = e.get("message") or {}
                    mid = self._valid_id(m.get("id"))
                    if mid:
                        relabeled[mid] = [str(x) for x in m.get("labelIds") or []]
        added, updated = [], []
        for mid in new:
            if mid in gone:
                continue
            try:
                rec = self._fetch(mid)
            except Exception as exc:
                if getattr(exc, "status", None) == 404:  # deleted before we got to it
                    continue
                raise
            if not rec or GONE_LABELS & set(rec["labels"]):
                continue
            self._put(rec)
            ids.append(mid)
            known.add(mid)
            added.append(mid)
        for mid, labels in relabeled.items():
            if mid in gone or mid not in known or mid in added:
                continue
            if GONE_LABELS & set(labels):
                gone.add(mid)
                continue
            rec = self.get(mid)
            if rec is None:
                continue
            rec = dict(rec, labels=labels, unread="UNREAD" in labels)
            self._put(rec)
            updated.append(mid)
        deleted = [mid for mid in ids if mid in gone]
        for mid in deleted:
            self._drop(mid)
        ids = [mid for mid in ids if mid not in gone]
        history_id = str(res.get("historyId") or start)
        self._save_index(ids, history_id)
        return {"mode": "incremental", "added": added, "updated": updated, "deleted": deleted, "historyId": history_id}
