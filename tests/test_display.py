"""Chat display: Markdown tables and Open links for 3 messages and 2 files, and google_open."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "backend" / "display.py"
_spec = importlib.util.spec_from_file_location("gw_display", _PATH)
display = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(display)

TODAY = date(2026, 10, 24)
BODY = "SECRET BODY TEXT that must never reach the chat"

MESSAGES = [
    {
        "id": "18f1a",
        "from": "Ada Lovelace <ada@example.com>",
        "subject": "Island playtest | Friday",
        "snippet": "Can you join the playtest at 6?",
        "internalDate": "1792828800000",  # 2026-10-24 UTC
        "labelIds": ["INBOX", "UNREAD"],
        "body": BODY,
    },
    {
        "id": "18f1b",
        "from": "bob@example.com",
        "subject": "Invoice #42",
        "snippet": "Attached is the invoice",
        "date": "Mon, 6 Oct 2025 09:30:00 +0000",
        "labelIds": ["INBOX"],
    },
    {"id": "18f1c", "from": "", "subject": "", "snippet": "", "date": "2026-10-20T08:00:00Z", "unread": False},
]

FILES = [
    {
        "id": "1AbC",
        "name": "hero_texture.png",
        "mimeType": "image/png",
        "size": "2621440",
        "folder": "Textures",
        "thumbnail_url": "http://127.0.0.1:4199/media/thumb-1AbC.png",
        "thumbnailLink": "https://lh3.googleusercontent.com/remote",
    },
    {"id": "2DeF", "name": "Level Assets", "mimeType": "application/vnd.google-apps.folder", "folder": "My Drive"},
]


def test_three_messages_markdown_and_links():
    md = display.messages_markdown(MESSAGES, title="Inbox", account="me@example.com", today=TODAY)
    lines = md.splitlines()
    assert lines[0] == "**3 messages** · Inbox · me@example.com"
    assert lines[2] == "| | From | Subject | Date | |"
    rows = [ln for ln in lines if ln.startswith("| ●") or ln.startswith("| ○")]
    assert len(rows) == 3
    assert rows[0] == (
        "| ● | Ada Lovelace | **Island playtest \\| Friday** — Can you join the playtest at 6? | Oct 24 "
        "| [Open](plugin:googledrive/main?view=gmail&id=18f1a&account=me%40example.com) |"
    )
    assert rows[1].startswith("| ○ | bob@example.com | Invoice \\#42 — Attached is the invoice | 2025-10-06 |")
    assert "(no subject)" in rows[2] and "Oct 20" in rows[2]
    for mid in ("18f1a", "18f1b", "18f1c"):
        assert f"(plugin:googledrive/main?view=gmail&id={mid}&account=me%40example.com)" in md
    assert BODY not in md


def test_two_files_markdown_links_and_thumbnail():
    md = display.files_markdown(FILES, title="Search: hero", account="me@example.com")
    lines = md.splitlines()
    assert lines[0] == "**2 files** · Search: hero · me@example.com"
    assert lines[2] == "| File | Folder | Size | |"
    assert lines[4] == (
        "| hero\\_texture.png | Textures | 2.5 MB "
        "| [Open](plugin:googledrive/main?view=drive&id=1AbC&account=me%40example.com) |"
    )
    assert lines[5] == (
        "| Level Assets/ | My Drive | — | [Open](plugin:googledrive/main?view=drive&id=2DeF&account=me%40example.com) |"
    )
    assert lines[7] == (
        "[![hero\\_texture.png](http://127.0.0.1:4199/media/thumb-1AbC.png)]"
        "(plugin:googledrive/main?view=drive&id=1AbC&account=me%40example.com)"
    )
    assert "googleusercontent" not in md


def test_remote_thumbnail_is_never_shown():
    f = dict(FILES[0], thumbnail_url="https://lh3.googleusercontent.com/x")
    assert "![" not in display.files_markdown([f])
    md = display.files_markdown([FILES[0]], thumbnail=lambda _f: "http://127.0.0.1:1/t.png")
    assert "](http://127.0.0.1:1/t.png)" in md


def test_long_lists_are_cut_with_open_all():
    many = [dict(MESSAGES[1], id=f"m{i}") for i in range(30)]
    md = display.messages_markdown(many, query="from:bob", total=120, limit=25, today=TODAY)
    assert md.startswith("**120 messages**")
    assert md.count("[Open](") == 25
    assert md.endswith("Showing 25 of 120. [Open all in Gmail](plugin:googledrive/main?view=gmail&q=from%3Abob)")


def test_empty_results():
    assert display.messages_markdown([]).endswith("No messages found.")
    assert display.files_markdown([]).endswith("No files found.")


def test_snippet_is_cut_and_one_line():
    m = dict(MESSAGES[1], snippet="line one\nline two " + "x" * 200)
    row = display.messages_markdown([m], today=TODAY).splitlines()[4]
    assert "\n" not in row and "…" in row and "x" * 100 not in row


def test_with_display():
    out = display.with_display({"ok": True}, "**1 file**")
    assert out == {"ok": True, "display": "**1 file**"}


class FakeApi:
    def __init__(self, answer=None, raises=None):
        self.calls, self.answer, self.raises = [], answer, raises
        self.tools, self.rpcs = {}, {}

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.raises:
            raise self.raises
        return self.answer if self.answer is not None else {"ok": True, "route": "plugin_panel"}

    def tool(self, **_kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco

    def register_panel_rpc(self, name, fn):
        self.rpcs[name] = fn


def test_google_open_navigates_and_leaves_request_for_panel():
    api = FakeApi()
    out = display.google_open(api, "Gmail", "18f1a", account="me@example.com")
    assert out["ok"] and out["navigated"] and out["view"] == "gmail"
    assert api.calls == [("ducky_ui_navigate", {"route": "plugin_panel", "item_id": "googledrive:main"})]
    assert out["link"] == "plugin:googledrive/main?view=gmail&id=18f1a&account=me%40example.com"
    req = display.take_pending_open()
    assert req["view"] == "gmail" and req["id"] == "18f1a" and req["account"] == "me@example.com"
    assert display.take_pending_open() is None


def test_google_open_without_panel_gives_a_link():
    out = display.google_open(FakeApi(raises=RuntimeError("no panel")), "drive", query="hero")
    assert out["ok"] and not out["navigated"]
    assert out["display"] == "[Open Drive](plugin:googledrive/main?view=drive&q=hero)"
    assert "no panel" in out["note"]
    assert not display.google_open(FakeApi(), "calendar")["ok"]


def test_register_adds_tool_and_panel_call():
    api = FakeApi()
    display.register(api)
    assert set(api.tools) == {"google_open"} and set(api.rpcs) == {"google_open_pending"}
    out = json.loads(api.tools["google_open"]("drive", "1AbC"))
    assert out["navigated"] and out["item_id"] == "1AbC"
    assert api.rpcs["google_open_pending"]()["id"] == "1AbC"
