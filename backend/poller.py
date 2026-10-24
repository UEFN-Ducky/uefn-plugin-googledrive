"""New-mail trigger: polls Gmail history per account and fires ``gmail.received``.

Stdlib only and no package-relative imports, like ``gmail.py``.

    poller = Poller(api, accounts=auth.gmail_accounts, store_for=lambda a: MailStore(api, a, client_for(a)),
                    interval=lambda: settings.get("gmail_poll_seconds", 60))
    poller.start()   # daemon thread; poller.stop() on unload

Each tick syncs every account's ``MailStore`` and emits
``api.emit_automation('gmail.received', {account, id, from, subject})`` once for
each message the cache had never seen. An account's first sync is a full sync
and emits nothing, so old mail never fires. Sent mail, drafts, spam and trash
never fire. Nothing runs while the plugin is disabled. Nothing personal is logged.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("uefn.plugin.googledrive.poller")

TRIGGER = "gmail.received"
DEFAULT_INTERVAL_S = 60.0
MIN_INTERVAL_S = 15.0
SILENT_LABELS = {"SENT", "DRAFT", "SPAM", "TRASH"}


def _interval(value: Any) -> float:
    try:
        v = float(value() if callable(value) else value)
    except (TypeError, ValueError):
        v = DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, v)


class Poller:
    def __init__(
        self,
        api: Any,
        *,
        accounts: Callable[[], list[str]],
        store_for: Callable[[str], Any],
        interval: Callable[[], Any] | float = DEFAULT_INTERVAL_S,
    ):
        self._api = api
        self._accounts = accounts
        self._store_for = store_for
        self._interval = interval
        self._stores: dict[str, Any] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()  # one tick at a time (thread + poll_now)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gmail-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        self._thread = None

    def wake(self) -> None:
        """Poll now instead of waiting out the interval (e.g. an account was added)."""
        self._wake.set()

    def interval(self) -> float:
        return _interval(self._interval)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # never let the thread die
                log.info("poll failed: %s", exc.__class__.__name__)
            self._wake.wait(self.interval())
            self._wake.clear()

    # ── one tick ──────────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        check = getattr(self._api, "is_enabled", None)
        try:
            return bool(check()) if callable(check) else True
        except Exception:
            return False

    def _store(self, account: str) -> Any:
        store = self._stores.get(account)
        if store is None:
            store = self._stores[account] = self._store_for(account)
        return store

    def poll_once(self) -> dict[str, Any]:
        """Sync every account once. → {paused} or {emitted, accounts: {account: n}}."""
        if not self._enabled():
            return {"paused": True, "emitted": 0}
        with self._lock:
            accounts = [a for a in (self._accounts() or []) if a]
            for gone in set(self._stores) - set(accounts):
                self._stores.pop(gone, None)
            per: dict[str, int] = {}
            for account in accounts:
                try:
                    per[account] = self._poll_account(account)
                except Exception as exc:  # one bad account never stops the others
                    log.info("gmail poll failed: %s", exc.__class__.__name__)
                    per[account] = 0
            return {"paused": False, "emitted": sum(per.values()), "accounts": per}

    def _poll_account(self, account: str) -> int:
        store = self._store(account)
        res = store.sync() or {}
        if res.get("mode") != "incremental":
            return 0  # a full sync is a fresh cache: everything in it is old mail
        sent = 0
        for mid in res.get("added") or []:
            rec = store.get(mid) or {}
            if not rec or SILENT_LABELS & set(rec.get("labels") or []):
                continue
            if not self._enabled():  # turned off mid-tick
                break
            try:
                self._api.emit_automation(
                    TRIGGER,
                    {"account": account, "id": mid, "from": rec.get("from", ""), "subject": rec.get("subject", "")},
                )
                sent += 1
            except Exception as exc:
                log.info("emit %s failed: %s", TRIGGER, exc.__class__.__name__)
        return sent
