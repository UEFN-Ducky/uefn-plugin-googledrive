"""No-delete hard rule: preview changes nothing, confirm trashes and records untrash, 51 is refused."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_PATH = Path(__file__).resolve().parents[1] / "backend" / "safety.py"
_spec = importlib.util.spec_from_file_location("gw_safety", _PATH)
safety = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(safety)


class FakeApi:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.changeset = SimpleNamespace(record=lambda **kw: self.records.append(kw))


def _messages(n: int) -> list[dict]:
    return [{"id": f"m{i}", "subject": f"Subject {i}"} for i in range(1, n + 1)]


def _trash(api, items, confirmed, trashed):
    return safety.guarded_remove(
        api,
        command="gmail_trash",
        kind="message",
        items=items,
        confirmed=confirmed,
        apply=lambda ids: trashed.extend(ids) or {"trashed": len(ids)},
        inverse=safety.untrash_inverse("gmail_untrash", account="me@example.com"),
        account="me@example.com",
    )


def test_preview_without_confirm_changes_nothing():
    api, trashed = FakeApi(), []
    out = _trash(api, _messages(14), False, trashed)
    assert out["ok"] and out["preview"] and not out["changed"]
    assert out["message"].startswith('would trash 14 messages: "Subject 1"')
    assert out["message"].endswith("… and 4 more")
    assert [it["id"] for it in out["items"]] == [f"m{i}" for i in range(1, 15)]
    assert "ducky_ask_user" in out["next"]
    assert trashed == [] and api.records == []


def test_confirmed_trashes_and_records_untrash():
    api, trashed = FakeApi(), []
    out = _trash(api, _messages(3), True, trashed)
    assert out["ok"] and out["changed"] and out["count"] == 3
    assert trashed == ["m1", "m2", "m3"]
    assert out["result"] == {"trashed": 3}
    assert len(api.records) == 1
    rec = api.records[0]
    assert rec["command"] == "gmail_trash" and rec["revertable"] == "auto"
    assert rec["inverse"] == [
        {"command": "gmail_untrash", "params": {"ids": ["m1", "m2", "m3"], "account": "me@example.com"}}
    ]
    assert [t["id"] for t in rec["targets"]] == ["m1", "m2", "m3"]
    assert rec["summary"] == "Trashed 3 messages"


def test_item_51_is_refused_even_when_confirmed():
    for confirmed in (False, True):
        api, trashed = FakeApi(), []
        out = _trash(api, _messages(51), confirmed, trashed)
        assert not out["ok"] and not out["changed"]
        assert out["count"] == 51 and out["max_items"] == 50
        assert trashed == [] and api.records == []
    api, trashed = FakeApi(), []
    assert _trash(api, _messages(50), True, trashed)["ok"] and len(trashed) == 50


def test_duplicates_and_blanks_do_not_count():
    api, trashed = FakeApi(), []
    out = _trash(api, ["a", "a", "", {"id": "b", "name": "Plan.png"}], True, trashed)
    assert trashed == ["a", "b"] and out["items"][1]["label"] == "Plan.png"


def test_nothing_to_trash():
    out = _trash(FakeApi(), [], True, [])
    assert not out["ok"] and "Nothing to trash" in out["error"]


def test_permanent_delete_is_refused():
    trashed: list[str] = []
    for verb in ("delete", "delete_forever", "permanent delete", "empty trash", "purge"):
        out = safety.guarded_remove(
            FakeApi(), command="gmail_delete", kind="message", items=["m1"], confirmed=True,
            apply=lambda ids: trashed.extend(ids), inverse=None, verb=verb,
        )
        assert not out["ok"] and "Permanent delete" in out["error"]
    assert trashed == []
    assert not safety.refuse_permanent()["ok"]


def test_manual_change_without_inverse():
    api = FakeApi()
    out = safety.guarded_remove(
        api, command="google_disconnect", kind="account", items=[{"id": "me@example.com"}],
        confirmed=True, apply=lambda ids: None, inverse=None, verb="disconnect",
    )
    assert out["ok"] and api.records[0]["revertable"] == "manual"
    assert "cannot be undone" in out["revert"]


def test_ledger_failure_never_fails_the_tool():
    def boom(**_kw):
        raise RuntimeError("ledger down")

    api = SimpleNamespace(changeset=SimpleNamespace(record=boom))
    assert _trash(api, _messages(1), True, [])["changed"]
    assert _trash(None, _messages(1), True, [])["changed"]


def test_workflow_node_needs_allow_trash():
    trashed: list[str] = []
    kw = dict(
        command="gmail_trash", kind="message", items=["m1"],
        apply=lambda ids: trashed.extend(ids), inverse=safety.untrash_inverse("gmail_untrash"),
    )
    for config in (None, {}, {"allow_trash": False}, {"allow_trash": "no"}):
        out = safety.guarded_node_remove(FakeApi(), config, confirmed=True, **kw)
        assert not out["ok"] and "Allow trash" in out["error"]
    assert trashed == []
    for config in ({"allow_trash": True}, {"allow_trash": "true"}):
        assert safety.guarded_node_remove(FakeApi(), config, **kw)["changed"]
    assert trashed == ["m1", "m1"]
