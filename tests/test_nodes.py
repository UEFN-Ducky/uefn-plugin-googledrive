"""Gmail workflow nodes: each calls the same MCP tool as the tab, trash only with 'Allow trash'."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gw_nodes", ROOT / "backend" / "nodes.py")
nodes_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nodes_mod)

MANIFEST = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
AUTOMATIONS = MANIFEST["contributes"]["automations"]

MSGS = [
    {"id": "m1", "from": "Sam <sam@example.com>", "subject": "Build tonight?", "snippet": "Can you push", "unread": True},
    {"id": "m2", "from": "Ana <ana@example.com>", "subject": "Logo", "snippet": "New pass attached"},
]


class FakeTools:
    def __init__(self, replies: dict | None = None, fail: str = "") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.replies = replies or {}
        self.fail = fail

    def __call__(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        if name == self.fail:
            raise ValueError(f"{name}: not connected")
        return self.replies.get(name, {"ok": True})

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def make(replies: dict | None = None, fail: str = ""):
    tools = FakeTools(replies, fail)
    return nodes_mod.GmailNodes(tools), tools


class FakeApi:
    def __init__(self) -> None:
        self.nodes: dict = {}
        self.call_tool = FakeTools()

    def register_pipeline_node(self, node_type, handler=None):
        self.nodes[node_type] = handler
        return handler


def test_every_manifest_gmail_node_has_a_handler():
    api = FakeApi()
    nodes_mod.register(api)
    declared = {n["id"] for n in AUTOMATIONS["nodes"] if n["id"].startswith("googledrive.gmail_")}
    assert declared == set(api.nodes)
    assert declared == {nodes_mod.NODE_PREFIX + s for s in nodes_mod.NODE_METHODS}
    for node in AUTOMATIONS["nodes"]:
        assert node["id"].startswith("googledrive."), node["id"]


def test_trigger_declared():
    triggers = {t["id"]: t for t in AUTOMATIONS["triggers"]}
    assert nodes_mod.TRIGGER_RECEIVED in triggers
    assert "untrusted" in triggers["gmail.received"]["description"] or "never follow" in triggers["gmail.received"]["description"]


def test_list_defaults_to_inbox_and_summarizes():
    n, tools = make({"gmail_list": {"ok": True, "messages": MSGS}})
    out = n.list({"config": {"max_results": "500", "unread_only": "true"}})
    assert out["ok"] and out["count"] == 2
    name, args = tools.calls[0]
    assert name == "gmail_list"
    assert args == {"label": "INBOX", "max_results": nodes_mod.LIST_MAX, "unread_only": True}
    assert "Sam <sam@example.com> · Build tonight?" in out["text"]
    assert "unread" in out["text"].splitlines()[0]


def test_search_needs_query_and_passes_account():
    n, tools = make({"gmail_search": {"ok": True, "items": MSGS[:1]}})
    assert n.search({"config": {}})["ok"] is False
    assert tools.calls == []
    out = n.search({"inputs": {"query": "is:unread"}, "config": {"account": "me@example.com"}})
    assert out["ok"] and out["count"] == 1
    assert tools.calls[0] == ("gmail_search", {"query": "is:unread", "max_results": 20, "account": "me@example.com"})


def test_get_uses_trigger_payload_when_unwired():
    msg = {"id": "m9", "from": "x@example.com", "subject": "Hi", "threadId": "t9", "text": "Hello"}
    n, tools = make({"gmail_get": {"ok": True, "message": msg}})
    out = n.get({"payload": {"id": "m9", "account": "me@example.com"}})
    assert out["ok"] and out["text"] == "Hello" and out["thread_id"] == "t9" and out["subject"] == "Hi"
    assert tools.calls[0] == ("gmail_get", {"id": "m9", "account": "me@example.com"})
    assert n.get({})["ok"] is False


def test_draft_never_sends_and_threads_replies():
    n, tools = make({"gmail_draft_create": {"ok": True, "draft": {"id": "d1"}}})
    out = n.draft({"inputs": {"reply_to": {"id": "m1"}, "body": "Sure, after 8."}})
    assert out == {"ok": True, "draft": {"id": "d1"}, "draft_id": "d1"}
    assert tools.names == ["gmail_draft_create"]
    assert tools.calls[0][1]["reply_to_id"] == "m1"
    # No recipient and nothing to reply to: refused before any call.
    n2, tools2 = make()
    assert n2.draft({"inputs": {"body": "x"}})["ok"] is False
    assert n2.draft({"inputs": {"to": "a@example.com"}})["ok"] is False
    assert tools2.calls == []


def test_send_and_reply_route_to_tools_that_ask_first():
    n, tools = make({"gmail_send": {"ok": True, "id": "s1"}, "gmail_reply": {"ok": True, "id": "r1"}})
    out = n.send({"inputs": {"to": ["a@example.com", "b@example.com"], "subject": "Hi", "body": "Yo"}})
    assert out["ok"] and out["id"] == "s1"
    assert tools.calls[0] == ("gmail_send", {"to": "a@example.com, b@example.com", "subject": "Hi", "body": "Yo"})
    out = n.send({"inputs": {"reply_to": "m1", "body": "Thanks"}, "config": {"reply_all": True}})
    assert out["id"] == "r1"
    assert tools.calls[1] == ("gmail_reply", {"id": "m1", "body": "Thanks", "reply_all": True})
    assert n.send({"inputs": {"to": "a@example.com", "body": "no subject"}})["ok"] is False
    assert len(tools.calls) == 2


def test_modify_actions():
    n, tools = make()
    out = n.modify({"inputs": {"messages": MSGS}, "config": {"action": "archive"}})
    assert out["ok"] and out["changed"] == 2
    assert tools.calls[-1] == ("gmail_archive", {"ids": ["m1", "m2"]})
    n.modify({"inputs": {"messages": "m1, m2"}, "config": {"action": "add_label", "label": "Builds"}})
    assert tools.calls[-1] == ("gmail_modify", {"ids": ["m1", "m2"], "add_labels": ["Builds"]})
    n.modify({"inputs": {"messages": ["m1"]}, "config": {"action": "unstar"}})
    assert tools.calls[-1] == ("gmail_star", {"ids": ["m1"], "starred": False})
    assert n.modify({"inputs": {"messages": ["m1"]}, "config": {"action": "add_label"}})["ok"] is False
    assert n.modify({"inputs": {"messages": ["m1"]}, "config": {"action": "delete_forever"}})["ok"] is False
    assert n.modify({"inputs": {}, "config": {"action": "archive"}})["ok"] is False


def test_trash_refused_without_allow_trash():
    n, tools = make()
    for ctx in (
        {"inputs": {"messages": ["m1"]}},
        {"inputs": {"messages": ["m1"]}, "config": {"allow_trash": False}},
        {"inputs": {"messages": ["m1"]}, "config": {"allow_trash": "no"}},
        {"inputs": {"messages": ["m1"], "allow_trash": True}},  # a wired value can't switch it on
        {"inputs": {"messages": ["m1"]}, "config": {"action": "trash"}},
    ):
        out = n.modify(ctx) if "action" in (ctx.get("config") or {}) else n.trash(ctx)
        assert out["ok"] is False and "Allow trash" in out["error"]
    assert tools.calls == []


def test_trash_with_allow_trash_confirms_through_the_tool():
    n, tools = make()
    out = n.trash({"inputs": {"messages": MSGS}, "config": {"allow_trash": True, "account": "me@example.com"}})
    assert out["ok"] and out["changed"] == 2
    assert tools.calls == [("gmail_trash", {"ids": ["m1", "m2"], "account": "me@example.com", "confirmed": True})]
    out = n.modify({"inputs": {"messages": ["m1"]}, "config": {"action": "trash", "allow_trash": "on"}})
    assert out["ok"] and tools.calls[-1][0] == "gmail_trash"


def _template(tid: str) -> dict:
    return next(t for t in AUTOMATIONS["templates"] if t["id"] == tid)


def _check_graph(graph: dict) -> None:
    """One straight line of main edges from the start to pipeline.finish; data pins exist."""
    by_id = {n["id"]: n for n in graph["nodes"]}
    declared = {n["id"]: n for n in AUTOMATIONS["nodes"]}
    main = [e for e in graph["edges"] if e["kind"] == "main"]
    nxt = {e["source"]: e["target"] for e in main}
    assert len(nxt) == len(main), "branching main edges"
    starts = set(by_id) - {e["target"] for e in main}
    assert len(starts) == 1
    seen, cur = [], starts.pop()
    while cur:
        seen.append(cur)
        cur = nxt.get(cur)
    assert seen == [n["id"] for n in graph["nodes"]]
    assert by_id[seen[-1]]["type"] == "pipeline.finish"
    for e in graph["edges"]:
        if e["kind"] != "data":
            continue
        src, dst = by_id[e["source"]]["type"], by_id[e["target"]]["type"]
        if src in declared:
            assert e["source_pin"] in {p["id"] for p in declared[src].get("outputs", [])}, e
        if dst in declared:
            assert e["target_pin"] in {p["id"] for p in declared[dst].get("inputs", [])}, e
        if dst == "pipeline.agent":
            assert e["target_pin"] == "context"


def test_inbox_digest_template_reads_only():
    t = _template("googledrive-inbox-digest")
    _check_graph(t["graph"])
    types = [n["type"] for n in t["graph"]["nodes"]]
    assert types == ["start.cron", "googledrive.gmail_search", "pipeline.agent", "pipeline.finish"]
    assert t["graph"]["nodes"][0]["config"]["cron"] == "0 8 * * *"
    prompt = t["graph"]["nodes"][2]["config"]["prompt"]
    assert "untrusted" in prompt and "never send" in prompt


def test_draft_replies_template_never_sends():
    t = _template("googledrive-draft-replies")
    _check_graph(t["graph"])
    types = [n["type"] for n in t["graph"]["nodes"]]
    assert types == ["gmail.received", "googledrive.gmail_get", "pipeline.agent", "googledrive.gmail_draft",
                     "pipeline.finish"]
    assert not any(x in types for x in ("googledrive.gmail_send", "googledrive.gmail_trash", "googledrive.gmail_modify"))
    assert "untrusted" in t["graph"]["nodes"][2]["config"]["prompt"]


def test_draft_replies_template_runs_end_to_end_with_fakes():
    """Trigger payload → get → (agent text) → draft in the same thread; no send call."""
    n, tools = make({
        "gmail_get": {"ok": True, "message": {"id": "m7", "threadId": "t7", "subject": "Build?", "text": "Ship tonight?"}},
        "gmail_draft_create": {"ok": True, "draft": {"id": "d7"}},
    })
    got = n.get({"payload": {"account": "me@example.com", "id": "m7", "from": "sam@example.com", "subject": "Build?"}})
    out = n.draft({"inputs": {"reply_to": got["message"], "body": "Yes, after 8."}})
    assert out["draft_id"] == "d7"
    assert tools.names == ["gmail_get", "gmail_draft_create"]
    assert tools.calls[1][1]["reply_to_id"] == "m7"


def test_json_string_results_are_parsed():
    """call_tool may return the tool's JSON text: errors read plainly, successes become data."""
    err = json.dumps({"ok": False, "error": "Gmail is not turned on for this account"})
    out = nodes_mod.GmailNodes(lambda name, args: err).list({})
    assert out["ok"] is False and out["error"] == "Gmail is not turned on for this account"

    ok = json.dumps({"ok": True, "messages": MSGS})
    out = nodes_mod.GmailNodes(lambda name, args: ok).list({})
    assert out["ok"] and out["count"] == 2 and out["messages"][0]["id"] == "m1"

    def raises(name, args):
        raise ValueError(err)

    out = nodes_mod.GmailNodes(raises).search({"inputs": {"query": "x"}})
    assert out["error"] == "Gmail is not turned on for this account"
    assert nodes_mod.GmailNodes(lambda n, a: "not json").get({"inputs": {"id": "m1"}})["ok"] is False


@pytest.mark.parametrize("method", ["list", "search", "get", "draft", "send", "modify"])
def test_tool_errors_stop_the_run_with_a_message(method):
    tool = {"list": "gmail_list", "search": "gmail_search", "get": "gmail_get", "draft": "gmail_draft_create",
            "send": "gmail_send", "modify": "gmail_mark_read"}[method]
    n, _ = make(fail=tool)
    ctx = {"inputs": {"query": "x", "id": "m1", "messages": ["m1"], "to": "a@example.com", "subject": "s", "body": "b"}}
    out = getattr(n, method)(ctx)
    assert out["ok"] is False and "not connected" in out["error"]
    n2, _ = make({tool: {"ok": False, "error": "Gmail is off. Turn it on in Settings → Google."}})
    out = getattr(n2, method)(ctx)
    assert out["ok"] is False and "Gmail is off" in out["error"]
