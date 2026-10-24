"""No-delete hard rule for every tool that removes something.

Gmail trash, label delete, draft delete, Drive trash and account remove all go through
``guarded_remove``:

  - Without ``confirmed=True`` it returns a preview ("would trash 14 messages: …") and
    changes nothing. The agent shows those exact items to the user with ducky_ask_user
    and calls again with ``confirmed=True`` only after they say yes.
  - At most ``MAX_ITEMS`` (50) items per confirmation; item 51 is refused outright.
  - Trash only. Nothing here ever deletes permanently (``PERMANENT_VERBS`` are refused).
  - A confirmed removal records a changeset whose inverse (untrash, recreate the label…)
    lets Revert undo it.

Workflow nodes have no one to ask, so they may remove only when the node's own
"Allow trash" setting is on (``node_may_trash``); ``guarded_node_remove`` wires that up.

This module imports nothing from the rest of the package, so the Gmail and Drive tools can
import it first and tests can load it alone.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Iterable

MAX_ITEMS = 50
PREVIEW_NAMES = 10
LABEL_CHARS = 60

# Verbs a guarded tool may use, and the past tense the result reports.
VERBS: dict[str, str] = {
    "trash": "trashed",
    "remove": "removed",
    "delete label": "deleted label",
    "disconnect": "disconnected",
}
# Never allowed, whatever a tool or node asks for.
PERMANENT_VERBS = frozenset({"delete", "delete forever", "permanent delete", "purge", "empty trash", "destroy"})

ASK_FIRST = (
    "Nothing changed. Show these exact items to the user with ducky_ask_user, then call "
    "again with confirmed=true only after they say yes."
)
NODE_NOT_ALLOWED = "This node may not trash anything: turn on 'Allow trash' in its settings."

Inverse = Callable[[list[str]], list[dict[str, Any]]]


def _item(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        ident = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or raw.get("name") or raw.get("subject") or ident).strip()
    else:
        ident = str(raw or "").strip()
        label = ident
    if len(label) > LABEL_CHARS:
        label = label[: LABEL_CHARS - 1] + "…"
    return {"id": ident, "label": label or ident}


def normalize_items(items: Iterable[Any] | None) -> list[dict[str, str]]:
    """``[{id, label}]`` from ids or dicts (``label`` / ``name`` / ``subject``), deduped, blanks dropped."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in items or []:
        it = _item(raw)
        if it["id"] and it["id"] not in seen:
            seen.add(it["id"])
            out.append(it)
    return out


def plural(n: int, noun: str) -> str:
    """``1 message`` / ``14 messages``."""
    if n == 1:
        return f"1 {noun}"
    if noun.endswith(("s", "x", "ch", "sh")):
        return f"{n} {noun}es"
    if noun.endswith("y") and noun[-2:-1] not in "aeiou":
        return f"{n} {noun[:-1]}ies"
    return f"{n} {noun}s"


def describe(verb: str, items: list[dict[str, str]], noun: str) -> str:
    """``trash 14 messages: "Invoice", "Hi" … and 4 more``."""
    names = ", ".join(f'"{it["label"]}"' for it in items[:PREVIEW_NAMES])
    more = len(items) - PREVIEW_NAMES
    tail = f" … and {more} more" if more > 0 else ""
    return f"{verb} {plural(len(items), noun)}: {names}{tail}"


def is_permanent(verb: str) -> bool:
    v = " ".join(str(verb or "").lower().replace("_", " ").split())
    return v in PERMANENT_VERBS or v.startswith("permanent") or "forever" in v


def refuse_permanent(what: str = "") -> dict[str, Any]:
    """The answer for any request to delete permanently or empty the trash."""
    subject = f"{what} " if what else ""
    return {
        "ok": False,
        "changed": False,
        "error": f"Permanent delete {subject}is not allowed: UEFN Ducky only moves things to the trash, "
        "where Google keeps them for 30 days.",
    }


def untrash_inverse(command: str, *, ids_param: str = "ids", account: str = "", **extra: Any) -> Inverse:
    """Inverse for a trash: call ``command`` (e.g. ``gmail_untrash``) with the same ids."""

    def build(ids: list[str]) -> list[dict[str, Any]]:
        params: dict[str, Any] = {ids_param: list(ids), **extra}
        if account:
            params["account"] = account
        return [{"command": command, "params": params}]

    return build


def _ident(command: str, account: str, ids: list[str]) -> str:
    if len(ids) == 1:
        return f"{account}:{ids[0]}" if account else ids[0]
    digest = hashlib.sha1("\n".join([command, account, *ids]).encode("utf-8")).hexdigest()[:12]
    return f"{account}:{len(ids)}:{digest}" if account else f"{len(ids)}:{digest}"


def _record(
    api: Any,
    *,
    command: str,
    kind: str,
    items: list[dict[str, str]],
    inverse: list[dict[str, Any]],
    summary: str,
    account: str,
    params: dict[str, Any] | None,
) -> None:
    changeset = getattr(api, "changeset", None) if api is not None else None
    if changeset is None:
        return
    ids = [it["id"] for it in items]
    try:
        changeset.record(
            command=command,
            kind=kind,
            ident=_ident(command, account, ids),
            before={"ids": ids, "account": account},
            inverse=inverse,
            targets=[{"kind": kind, "id": it["id"], "label": it["label"], "path": it["id"]} for it in items],
            summary=summary,
            revertable="auto" if inverse else "manual",
            params=params or {},
        )
    except Exception:  # noqa: BLE001 - the ledger never fails the tool
        pass


def guarded_remove(
    api: Any,
    *,
    command: str,
    kind: str,
    items: Iterable[Any],
    confirmed: bool,
    apply: Callable[[list[str]], Any],
    inverse: Inverse | list[dict[str, Any]] | None,
    verb: str = "trash",
    noun: str = "",
    account: str = "",
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview, or (confirmed) remove up to ``MAX_ITEMS`` items and record how to undo it.

    ``items``: ids or ``{id, label}`` dicts — the exact things the user will see.
    ``apply(ids)``: does the removal (a trash, never a permanent delete); called only when
    ``confirmed`` is true. ``inverse``: ``untrash_inverse(...)`` or a fixed changeset inverse
    list; ``None`` records a manual-only change (e.g. disconnecting an account).

    Returns ``{ok, changed, preview, verb, count, items, message}`` plus ``result`` /
    ``revert`` once done, or ``{ok: False, error}`` when refused.
    """
    noun = noun or kind
    if is_permanent(verb):
        return refuse_permanent(plural(2, noun).split(" ", 1)[1])
    if verb not in VERBS:
        return {"ok": False, "changed": False, "error": f"unknown removal verb: {verb}"}
    picked = normalize_items(items)
    if not picked:
        return {"ok": False, "changed": False, "error": f"Nothing to {verb}: no {plural(2, noun).split(' ', 1)[1]} given."}
    if len(picked) > MAX_ITEMS:
        return {
            "ok": False,
            "changed": False,
            "count": len(picked),
            "max_items": MAX_ITEMS,
            "error": f"Refused: {plural(len(picked), noun)} is over the limit of {MAX_ITEMS} per confirmation. "
            f"Split it into batches of {MAX_ITEMS} or fewer and confirm each one.",
        }
    if not confirmed:
        return {
            "ok": True,
            "changed": False,
            "preview": True,
            "verb": verb,
            "count": len(picked),
            "items": picked,
            "message": "would " + describe(verb, picked, noun),
            "next": ASK_FIRST,
        }
    ids = [it["id"] for it in picked]
    result = apply(ids)
    inv = inverse(ids) if callable(inverse) else list(inverse or [])
    done = VERBS[verb]
    summary = f"{done[:1].upper()}{done[1:]} {plural(len(picked), noun)}"
    _record(api, command=command, kind=kind, items=picked, inverse=inv, summary=summary, account=account, params=params)
    out: dict[str, Any] = {
        "ok": True,
        "changed": True,
        "preview": False,
        "verb": verb,
        "count": len(picked),
        "items": picked,
        "message": summary + ".",
        "result": result,
    }
    out["revert"] = "Revert in the Changes tab undoes this." if inv else "This cannot be undone from the Changes tab."
    return out


def node_may_trash(config: dict[str, Any] | None) -> bool:
    """True only when a workflow node's explicit 'Allow trash' setting is on."""
    value = (config or {}).get("allow_trash")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def guarded_node_remove(api: Any, config: dict[str, Any] | None, **kwargs: Any) -> dict[str, Any]:
    """``guarded_remove`` for a workflow node: confirmed by its 'Allow trash' setting, refused without it."""
    kwargs.pop("confirmed", None)
    if not node_may_trash(config):
        return {"ok": False, "changed": False, "error": NODE_NOT_ALLOWED}
    return guarded_remove(api, confirmed=True, **kwargs)
