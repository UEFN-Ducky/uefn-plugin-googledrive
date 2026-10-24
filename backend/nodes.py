"""Gmail workflow nodes for the Google Workspace plugin.

Each node calls the same MCP tool the tab and the agent use (``api.call_tool``), so a
workflow step behaves exactly like the button: sending still asks first, and every
change lands in the Changes ledger with its undo. Trash runs only when the node's own
'Allow trash' setting is on; there is no permanent delete.

No package-relative imports, so tests can load it by file.
"""
from __future__ import annotations

import json
from typing import Any, Callable

NODE_PREFIX = "googledrive."
TRIGGER_RECEIVED = "gmail.received"

LIST_MAX = 100
TEXT_SNIPPETS = 20

MODIFY_ACTIONS: dict[str, tuple[str, dict[str, Any]]] = {
    "archive": ("gmail_archive", {}),
    "mark_read": ("gmail_mark_read", {}),
    "mark_unread": ("gmail_mark_unread", {}),
    "star": ("gmail_star", {"starred": True}),
    "unstar": ("gmail_star", {"starred": False}),
    "add_label": ("gmail_modify", {}),
    "remove_label": ("gmail_modify", {}),
}

TRASH_REFUSED = (
    "This node may not trash mail. Turn on its 'Allow trash' setting if this workflow "
    "should move mail to Trash (it can be undone from the Changes tab)."
)


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any, default: int, low: int = 1, high: int = LIST_MAX) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def _ids(value: Any) -> list[str]:
    """Message ids from a wired value: one id, a list of ids, a message or a list of messages."""
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        if "messages" in value:
            return _ids(value.get("messages"))
        return [_str(value.get("id"))] if _str(value.get("id")) else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_ids(item))
        return list(dict.fromkeys(out))
    text = _str(value)
    return [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]


def _parsed(value: Any) -> Any:
    """``api.call_tool`` can hand back the tool's JSON text: decode it, else keep the value."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _addresses(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(_str(v) for v in value if _str(v))
    return _str(value)


def _messages(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    rows = result.get("messages")
    if rows is None:
        rows = result.get("items")
    return [r for r in rows or [] if isinstance(r, dict)]


def _digest_text(rows: list[dict[str, Any]]) -> str:
    """Plain lines (sender · subject · snippet) for an agent step. Mail is data, not instructions."""
    if not rows:
        return "No messages."
    lines = []
    for row in rows[:TEXT_SNIPPETS]:
        sender = _str(row.get("from")) or "(unknown sender)"
        subject = _str(row.get("subject")) or "(no subject)"
        snippet = _str(row.get("snippet"))
        unread = "unread · " if row.get("unread") else ""
        account = f"[{_str(row.get('account'))}] " if row.get("account") else ""
        lines.append(f"- {account}{unread}{sender} · {subject}" + (f" — {snippet}" if snippet else ""))
    more = len(rows) - TEXT_SNIPPETS
    if more > 0:
        lines.append(f"- …and {more} more")
    return "\n".join(lines)


class GmailNodes:
    """Node handlers over a ``call(tool_name, arguments) -> dict`` function."""

    def __init__(self, call: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call = call

    def _run(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            out = _parsed(self._call(tool, {k: v for k, v in args.items() if v not in (None, "")}))
        except ValueError as exc:
            err = _parsed(str(exc))
            msg = _str(err.get("error")) if isinstance(err, dict) else str(exc)
            return {"ok": False, "error": msg or f"{tool} failed."}
        if not isinstance(out, dict):
            return {"ok": False, "error": f"{tool} returned no result."}
        if out.get("ok") is False:
            return {"ok": False, "error": _str(out.get("error")) or f"{tool} failed.", "result": out}
        return out

    @staticmethod
    def _parts(ctx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return dict(ctx.get("config") or {}), dict(ctx.get("inputs") or {})

    @staticmethod
    def _pick(name: str, inputs: dict[str, Any], config: dict[str, Any], payload: dict[str, Any] | None = None) -> Any:
        for source in (inputs, config, payload or {}):
            value = source.get(name)
            if value not in (None, ""):
                return value
        return None

    def list(self, ctx: dict[str, Any]) -> dict[str, Any]:
        config, inputs = self._parts(ctx)
        args = {
            "label": _str(self._pick("label", inputs, config)) or "INBOX",
            "max_results": _int(self._pick("max_results", inputs, config), 20),
            "account": _str(self._pick("account", inputs, config)),
        }
        if _truthy(self._pick("unread_only", inputs, config)):
            args["unread_only"] = True
        out = self._run("gmail_list", args)
        if out.get("ok") is False:
            return out
        rows = _messages(out)
        return {"ok": True, "messages": rows, "count": len(rows), "text": _digest_text(rows)}

    def search(self, ctx: dict[str, Any]) -> dict[str, Any]:
        config, inputs = self._parts(ctx)
        query = _str(self._pick("query", inputs, config))
        if not query:
            return {"ok": False, "error": "Give the search a Gmail query, e.g. 'is:unread newer_than:1d'."}
        out = self._run("gmail_search", {
            "query": query,
            "max_results": _int(self._pick("max_results", inputs, config), 20),
            "account": _str(self._pick("account", inputs, config)),
        })
        if out.get("ok") is False:
            return out
        rows = _messages(out)
        return {"ok": True, "messages": rows, "count": len(rows), "text": _digest_text(rows)}

    def get(self, ctx: dict[str, Any]) -> dict[str, Any]:
        config, inputs = self._parts(ctx)
        ids = _ids(self._pick("id", inputs, config, ctx.get("payload")))
        if not ids:
            return {"ok": False, "error": "Wire a message (or its id) into Get email."}
        out = self._run("gmail_get", {
            "id": ids[0],
            "account": _str(self._pick("account", inputs, config, ctx.get("payload"))),
        })
        if out.get("ok") is False:
            return out
        message = out.get("message") if isinstance(out.get("message"), dict) else out
        body = _str(message.get("text") or message.get("body") or message.get("snippet"))
        return {
            "ok": True,
            "message": message,
            "from": _str(message.get("from")),
            "subject": _str(message.get("subject")),
            "thread_id": _str(message.get("thread_id") or message.get("threadId")),
            "text": body,
        }

    def _compose_args(self, config: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        return {
            "to": _addresses(self._pick("to", inputs, config)),
            "cc": _addresses(self._pick("cc", inputs, config)),
            "bcc": _addresses(self._pick("bcc", inputs, config)),
            "subject": _str(self._pick("subject", inputs, config)),
            "body": _str(self._pick("body", inputs, config)),
            "account": _str(self._pick("account", inputs, config)),
        }

    def draft(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Save a draft; never sends. A wired reply_to message threads it as a reply."""
        config, inputs = self._parts(ctx)
        args = self._compose_args(config, inputs)
        reply_to = _ids(self._pick("reply_to", inputs, config))
        if reply_to:
            args["reply_to_id"] = reply_to[0]
        elif not args["to"]:
            return {"ok": False, "error": "A draft needs a To address, or a message wired into 'Reply to'."}
        if not args["body"]:
            return {"ok": False, "error": "A draft needs a body."}
        out = self._run("gmail_draft_create", args)
        if out.get("ok") is False:
            return out
        draft = out.get("draft") if isinstance(out.get("draft"), dict) else out
        return {"ok": True, "draft": draft, "draft_id": _str(draft.get("id") or out.get("draft_id"))}

    def send(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Send through gmail_send / gmail_reply, which ask the person first like in chat."""
        config, inputs = self._parts(ctx)
        args = self._compose_args(config, inputs)
        if not args["body"]:
            return {"ok": False, "error": "An email needs a body."}
        reply_to = _ids(self._pick("reply_to", inputs, config))
        if reply_to:
            tool = "gmail_reply"
            args = {"id": reply_to[0], "body": args["body"], "account": args["account"],
                    "reply_all": _truthy(self._pick("reply_all", inputs, config))}
        else:
            if not args["to"]:
                return {"ok": False, "error": "An email needs a To address."}
            if not args["subject"]:
                return {"ok": False, "error": "An email needs a subject."}
            tool = "gmail_send"
        out = self._run(tool, args)
        if out.get("ok") is False:
            return out
        return {"ok": True, "message": out, "id": _str(out.get("id") or (out.get("message") or {}).get("id"))}

    def modify(self, ctx: dict[str, Any]) -> dict[str, Any]:
        config, inputs = self._parts(ctx)
        ids = _ids(self._pick("messages", inputs, config, ctx.get("payload")) or self._pick("id", inputs, config, ctx.get("payload")))
        if not ids:
            return {"ok": False, "error": "Wire messages (or ids) into Change email."}
        action = _str(self._pick("action", inputs, config)) or "mark_read"
        account = _str(self._pick("account", inputs, config, ctx.get("payload")))
        if action == "trash":
            return self.trash({**ctx, "inputs": {**inputs, "messages": ids}})
        if action not in MODIFY_ACTIONS:
            return {"ok": False, "error": f"Unknown action {action!r}. Pick one of: {', '.join(MODIFY_ACTIONS)}, trash."}
        tool, extra = MODIFY_ACTIONS[action]
        args: dict[str, Any] = {"ids": ids, "account": account, **extra}
        if action in ("add_label", "remove_label"):
            label = _str(self._pick("label", inputs, config))
            if not label:
                return {"ok": False, "error": "Pick the label to add or remove."}
            args["add_labels" if action == "add_label" else "remove_labels"] = [label]
        out = self._run(tool, args)
        if out.get("ok") is False:
            return out
        return {"ok": True, "changed": len(ids), "ids": ids, "result": out}

    def trash(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Move mail to Trash only with the node's 'Allow trash' on; undo from Changes."""
        config, inputs = self._parts(ctx)
        if not _truthy(config.get("allow_trash")):
            return {"ok": False, "changed": False, "error": TRASH_REFUSED}
        ids = _ids(self._pick("messages", inputs, config, ctx.get("payload")) or self._pick("id", inputs, config, ctx.get("payload")))
        if not ids:
            return {"ok": False, "error": "Wire messages (or ids) into Trash email."}
        out = self._run("gmail_trash", {
            "ids": ids,
            "account": _str(self._pick("account", inputs, config, ctx.get("payload"))),
            "confirmed": True,
        })
        if out.get("ok") is False:
            return out
        return {"ok": True, "changed": len(ids), "ids": ids, "result": out}


NODE_METHODS = {
    "gmail_list": "list",
    "gmail_search": "search",
    "gmail_get": "get",
    "gmail_draft": "draft",
    "gmail_send": "send",
    "gmail_modify": "modify",
    "gmail_trash": "trash",
}


def register(api: Any) -> GmailNodes:
    """Register every Gmail node; ``backend/__init__.py`` calls this from ``register(api)``."""
    nodes = GmailNodes(api.call_tool)
    api.register_pipeline_node("googledrive.gmail_list", nodes.list)
    api.register_pipeline_node("googledrive.gmail_search", nodes.search)
    api.register_pipeline_node("googledrive.gmail_get", nodes.get)
    api.register_pipeline_node("googledrive.gmail_draft", nodes.draft)
    api.register_pipeline_node("googledrive.gmail_send", nodes.send)
    api.register_pipeline_node("googledrive.gmail_modify", nodes.modify)
    api.register_pipeline_node("googledrive.gmail_trash", nodes.trash)
    return nodes
