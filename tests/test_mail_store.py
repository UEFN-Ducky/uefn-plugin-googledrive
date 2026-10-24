"""backend/mail_store.py against a fake Gmail client and fake api.data / api.search."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "backend" / "mail_store.py"
_spec = importlib.util.spec_from_file_location("gdrive_mail_store", _PATH)
mail_store = importlib.util.module_from_spec(_spec)
sys.modules["gdrive_mail_store"] = mail_store
_spec.loader.exec_module(mail_store)

ACCOUNT = "Me@Example.com"


class FakeData:
    def __init__(self):
        self.docs = {}
        self.sensitive = set()

    def get(self, key, default=None, *, sensitive=False):
        assert sensitive, f"{key} read without sensitive=True"
        return self.docs.get(key, default)

    def put(self, key, value, *, sensitive=False):
        assert sensitive, f"{key} written without sensitive=True"
        self.docs[key] = value
        self.sensitive.add(key)
        return {"ok": True}

    def delete(self, key):
        return self.docs.pop(key, None) is not None


class FakeSearch:
    def __init__(self):
        self.records = {}

    def upsert(self, rid, title, text, meta=None):
        assert ":" not in rid
        self.records[rid] = {"title": title, "text": text, "meta": meta}

    def delete(self, rid):
        return self.records.pop(rid, None) is not None


class FakeApi:
    def __init__(self):
        self.data = FakeData()
        self.search = FakeSearch()


class Expired(Exception):
    status = 404


def _msg(mid, subject, labels=("INBOX", "UNREAD"), sender="Ann <ann@example.com>"):
    return {
        "id": mid,
        "threadId": "t" + mid,
        "labelIds": list(labels),
        "snippet": f"snippet {mid}",
        "internalDate": str(1_700_000_000_000 + int(mid, 16)),
        "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]},
    }


class FakeGmail:
    def __init__(self):
        self.mailbox = {}
        self.history_id = "100"
        self.history = []
        self.expired = False
        self.calls = []

    def get_profile(self):
        self.calls.append(("profile",))
        return {"emailAddress": ACCOUNT, "historyId": self.history_id}

    def list_messages(self, q="", max=20, **kw):
        self.calls.append(("list", q, max))
        msgs = [m for m in self.mailbox.values() if not {"TRASH", "SPAM"} & set(m["labelIds"])]
        return {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in msgs], "ids": [m["id"] for m in msgs]}

    def get_message(self, mid, format="full", *, metadata_headers=None):
        self.calls.append(("get", mid, format))
        assert format == "metadata"
        return dict(self.mailbox[mid])

    def list_history(self, start, *, history_types=None, **kw):
        self.calls.append(("history", str(start)))
        if self.expired:
            raise Expired("expired")
        return {"history": list(self.history), "historyId": self.history_id}

    # helpers the tests drive
    def arrive(self, mid, subject, labels=("INBOX", "UNREAD")):
        self.mailbox[mid] = _msg(mid, subject, labels)
        self.history_id = str(int(self.history_id) + 1)
        self.history.append({"id": self.history_id, "messagesAdded": [{"message": {"id": mid, "labelIds": list(labels)}}]})

    def delete(self, mid):
        self.mailbox.pop(mid, None)
        self.history_id = str(int(self.history_id) + 1)
        self.history.append({"id": self.history_id, "messagesDeleted": [{"message": {"id": mid}}]})

    def relabel(self, mid, labels):
        self.mailbox[mid]["labelIds"] = list(labels)
        self.history_id = str(int(self.history_id) + 1)
        self.history.append({"id": self.history_id, "labelsRemoved": [{"message": {"id": mid, "labelIds": list(labels)}, "labelIds": ["UNREAD"]}]})

    def gets(self):
        return [c[1] for c in self.calls if c[0] == "get"]


@pytest.fixture
def env():
    api, gm = FakeApi(), FakeGmail()
    for i, subj in ((0xA1, "Hello"), (0xA2, "Invoice"), (0xA3, "Party")):
        gm.mailbox[f"{i:x}"] = _msg(f"{i:x}", subj)
    gm.history = []
    return api, gm, mail_store.MailStore(api, ACCOUNT, gm, days=14)


def test_full_sync_caches_metadata_as_sensitive_docs_and_search(env):
    api, gm, store = env
    res = store.sync()
    assert res["mode"] == "full"
    assert sorted(res["added"]) == ["a1", "a2", "a3"] and res["deleted"] == []
    assert ("list", "newer_than:14d", store.limit) in gm.calls
    assert store.history_id() == "100"
    m = store.get("a2")
    assert m["subject"] == "Invoice" and m["from"] == "Ann <ann@example.com>"
    assert m["snippet"] == "snippet a2" and m["unread"] is True and m["labels"] == ["INBOX", "UNREAD"]
    assert m["date"] == 1_700_000_000_000 + 0xA2
    # every doc went in sensitive and no key holds the address
    assert set(api.data.docs) == api.data.sensitive
    assert all("@" not in k and "example" not in k for k in api.data.docs)
    assert len(api.search.records) == 3
    rec = api.search.records[f"gmail.{store.acct}.a2"]
    assert rec["title"] == "Invoice" and "snippet a2" in rec["text"] and rec["meta"]["account"] == ACCOUNT
    assert [x["id"] for x in store.messages()] == ["a3", "a2", "a1"]
    assert store.messages()[0]["account"] == ACCOUNT


def test_second_sync_is_incremental_and_fetches_only_new(env):
    api, gm, store = env
    store.sync()
    gm.calls.clear()
    gm.arrive("b1", "New one")
    res = store.sync()
    assert res["mode"] == "incremental"
    assert res["added"] == ["b1"] and res["deleted"] == [] and res["updated"] == []
    assert gm.gets() == ["b1"]
    assert ("history", "100") in gm.calls and ("profile",) not in gm.calls
    assert store.history_id() == "101"
    assert store.get("b1")["subject"] == "New one"
    assert f"gmail.{store.acct}.b1" in api.search.records

    # nothing changed: no fetches, nothing added
    gm.calls.clear()
    gm.history = []
    res = store.sync()
    assert res["added"] == [] and gm.gets() == []
    assert ("history", "101") in gm.calls


def test_deleted_and_trashed_messages_leave_the_cache(env):
    api, gm, store = env
    store.sync()
    gm.delete("a1")
    gm.relabel("a2", ["TRASH"])
    res = store.sync()
    assert sorted(res["deleted"]) == ["a1", "a2"]
    assert store.get("a1") is None and store.get("a2") is None
    assert store.ids() == ["a3"]
    assert f"gmail.{store.acct}.a1" not in api.search.records
    assert f"gmail.{store.acct}.a2" not in api.search.records


def test_label_change_updates_unread(env):
    _, gm, store = env
    store.sync()
    gm.calls.clear()
    gm.relabel("a3", ["INBOX"])
    res = store.sync()
    assert res["updated"] == ["a3"] and gm.gets() == []
    assert store.get("a3")["unread"] is False and store.get("a3")["labels"] == ["INBOX"]


def test_added_then_deleted_in_one_window_is_never_fetched(env):
    _, gm, store = env
    store.sync()
    gm.calls.clear()
    gm.arrive("c1", "Gone fast")
    gm.delete("c1")
    res = store.sync()
    assert res["added"] == [] and gm.gets() == []
    assert store.get("c1") is None


def test_expired_history_falls_back_to_full_sync_and_drops_missing(env):
    _, gm, store = env
    store.sync()
    gm.expired = True
    gm.mailbox.pop("a1")
    gm.mailbox["d1"] = _msg("d1", "While away")
    gm.history_id = "500"
    res = store.sync()
    assert res["mode"] == "full"
    assert res["added"] == ["d1"] and res["deleted"] == ["a1"]
    assert store.history_id() == "500"
    assert sorted(store.ids()) == ["a2", "a3", "d1"]


def test_other_errors_are_not_swallowed(env):
    _, gm, store = env
    store.sync()

    class Boom(Exception):
        status = 500

    def fail(*a, **k):
        raise Boom("server")

    gm.list_history = fail
    with pytest.raises(Boom):
        store.sync()


def test_accounts_are_separate_and_clear_forgets(env):
    api, gm, store = env
    store.sync()
    other = mail_store.MailStore(api, "two@example.com", gm)
    assert other.acct != store.acct and other.ids() == []
    assert store.clear() == 3
    assert store.ids() == [] and store.history_id() == ""
    assert api.search.records == {}
    # a bad id never becomes a doc key
    assert store.get("../x") is None
