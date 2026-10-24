"""backend/poller.py: one emit per new message, never for old mail, paused when disabled."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


poller = _load("gdrive_poller", _ROOT / "backend" / "poller.py")
# Reuse the Gmail / api.data fakes the cache tests drive.
fakes = _load("gdrive_test_mail_store_fakes", _ROOT / "tests" / "test_mail_store.py")
MailStore = fakes.mail_store.MailStore


class Api(fakes.FakeApi):
    def __init__(self):
        super().__init__()
        self.enabled = True
        self.emits = []

    def is_enabled(self):
        return self.enabled

    def emit_automation(self, trigger_id, payload=None):
        self.emits.append((trigger_id, payload))
        return {"ok": True}


def setup(accounts=("me@example.com",)):
    api = Api()
    boxes = {}
    for a in accounts:
        gm = fakes.FakeGmail()
        gm.mailbox["a1"] = fakes._msg("a1", "Old mail")
        boxes[a] = gm
    p = poller.Poller(api, accounts=lambda: list(boxes), store_for=lambda a: MailStore(api, a, boxes[a]))
    return api, boxes, p


def test_one_history_change_emits_once_and_old_mail_never():
    api, boxes, p = setup()
    gm = boxes["me@example.com"]
    first = p.poll_once()
    assert first["emitted"] == 0 and api.emits == []  # full sync: old mail stays quiet

    gm.arrive("b7", "Build failed")
    assert p.poll_once()["emitted"] == 1
    assert api.emits == [
        ("gmail.received", {"account": "me@example.com", "id": "b7", "from": "Ann <ann@example.com>", "subject": "Build failed"})
    ]
    # the same history page again (Gmail repeats it until historyId moves) never re-fires
    p.poll_once()
    p.poll_once()
    assert len(api.emits) == 1


def test_sent_and_draft_mail_do_not_fire():
    api, boxes, p = setup()
    gm = boxes["me@example.com"]
    p.poll_once()
    gm.arrive("c1", "My reply", labels=("SENT",))
    gm.arrive("c2", "Draft", labels=("DRAFT",))
    gm.arrive("c3", "Real one")
    p.poll_once()
    assert [e[1]["id"] for e in api.emits] == ["c3"]


def test_paused_while_disabled_then_catches_up_once():
    api, boxes, p = setup()
    gm = boxes["me@example.com"]
    p.poll_once()
    api.enabled = False
    gm.arrive("d1", "While off")
    gm.calls.clear()
    assert p.poll_once() == {"paused": True, "emitted": 0}
    assert gm.calls == [] and api.emits == []
    api.enabled = True
    p.poll_once()
    assert [e[1]["id"] for e in api.emits] == ["d1"]


def test_accounts_are_polled_separately_and_one_failure_does_not_stop_others():
    api, boxes, p = setup(("one@example.com", "two@example.com"))
    p.poll_once()
    boxes["two@example.com"].arrive("e1", "For two")

    def broken(*a, **k):
        raise RuntimeError("boom")

    boxes["one@example.com"].list_history = broken
    res = p.poll_once()
    assert res["accounts"] == {"one@example.com": 0, "two@example.com": 1}
    assert api.emits[0][1]["account"] == "two@example.com"


def test_interval_setting_has_a_floor_and_default():
    api, _, _ = setup()
    mk = lambda v: poller.Poller(api, accounts=list, store_for=lambda a: None, interval=v)  # noqa: E731
    assert mk(lambda: 120).interval() == 120
    assert mk(lambda: 1).interval() == poller.MIN_INTERVAL_S
    assert mk(lambda: "junk").interval() == poller.DEFAULT_INTERVAL_S
    assert mk(30).interval() == 30


def test_thread_polls_and_stops():
    api, boxes, p = setup()
    p._interval = 0  # floor applies; wake() drives the loop instead
    p.start()
    deadline = time.time() + 5
    while not (p._stores and p._stores["me@example.com"].history_id()) and time.time() < deadline:
        time.sleep(0.01)
    with p._lock:  # the first (full) sync has finished
        pass
    boxes["me@example.com"].arrive("f1", "Threaded")
    p.wake()
    while not api.emits and time.time() < deadline:
        time.sleep(0.01)
    p.stop()
    assert [e[1]["id"] for e in api.emits] == ["f1"]
    assert p._thread is None
