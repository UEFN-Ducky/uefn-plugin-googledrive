"""Gmail message parsing and building (stdlib ``email`` only).

Parse: a Gmail API message (format=full / metadata) or raw RFC 2822 bytes →
one flat dict: headers, subject, from/to/cc as {name, email}, text, html and
attachments. Build: new / reply / forward as RFC 2822 bytes plus the
``threadId`` to send them in, with In-Reply-To / References kept so Gmail and
other clients thread the reply.

No package-relative imports, so the host and tests can load it by file.
"""

from __future__ import annotations

import base64
import html as html_lib
import re
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from typing import Any, Iterable

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h[1-6])\b[^>]*>", re.I)
_DROP_RE = re.compile(r"<\s*(script|style|head)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_RE_PREFIX = re.compile(r"^\s*(re|aw|sv|antw)\s*:", re.I)
_FWD_PREFIX = re.compile(r"^\s*(fwd?|wg|tr)\s*:", re.I)
_CHARSET_RE = re.compile(r"charset\s*=\s*\"?([^\";\s]+)", re.I)


# ── small helpers ──────────────────────────────────────────────────────────


def b64url_decode(data: str) -> bytes:
    s = (data or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decode_header_value(value: Any) -> str:
    """RFC 2047 encoded-words → text. Plain strings pass through."""
    s = str(value or "")
    if "=?" not in s:
        return s
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def decode_bytes(data: bytes, charset: str = "") -> str:
    for cs in (charset or "utf-8", "utf-8"):
        try:
            return data.decode(cs, "replace")
        except LookupError:
            continue
    return data.decode("latin-1")


def html_to_text(html: str) -> str:
    """Rough readable text from an HTML body (for html-only mail)."""
    s = _DROP_RE.sub("", html or "")
    s = _BLOCK_RE.sub("\n", s)
    s = html_lib.unescape(_TAG_RE.sub("", s))
    lines = [ln.strip() for ln in s.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clean_header(value: str) -> str:
    s = str(value or "")
    if "\r" in s or "\n" in s:
        raise ValueError("header values may not contain line breaks")
    return s.strip()


# ── addresses ──────────────────────────────────────────────────────────────


def parse_addresses(value: Any) -> list[dict[str, str]]:
    """'A <a@x>, b@y' (or a list of such) → [{name, email}]; junk entries dropped."""
    if not value:
        return []
    values = [value] if isinstance(value, str) else [str(v) for v in value]
    out: list[dict[str, str]] = []
    for name, addr in getaddresses([decode_header_value(v) for v in values]):
        addr = addr.strip()
        if "@" not in addr:
            continue
        out.append({"name": name.strip(), "email": addr})
    return out


def format_addresses(value: Any) -> str:
    """[{name, email}] / 'a@x, B <b@y>' / ['a@x'] → one header value."""
    if not value:
        return ""
    if isinstance(value, dict):
        value = [value]
    items: list[dict[str, str]] = []
    if isinstance(value, str):
        items = parse_addresses(value)
    else:
        for v in value:
            if isinstance(v, dict) and v.get("email"):
                items.append({"name": str(v.get("name") or ""), "email": str(v["email"])})
            elif isinstance(v, str):
                items.extend(parse_addresses(v))
    return ", ".join(formataddr((a["name"], _clean_header(a["email"]))) for a in items)


def _emails(addrs: Iterable[dict[str, str]]) -> set[str]:
    return {a["email"].lower() for a in addrs}


# ── parse: Gmail API payload ───────────────────────────────────────────────


def _headers(part: dict[str, Any]) -> dict[str, str]:
    """Header name (lower case) → decoded value; first one wins."""
    out: dict[str, str] = {}
    for h in part.get("headers") or []:
        name = str(h.get("name") or "").lower()
        if name and name not in out:
            out[name] = decode_header_value(h.get("value"))
    return out


def walk_parts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Every MIME part of a Gmail payload, depth first."""
    stack = [payload or {}]
    while stack:
        part = stack.pop(0)
        yield part
        stack[0:0] = list(part.get("parts") or [])


def _part_charset(part: dict[str, Any]) -> str:
    m = _CHARSET_RE.search(_headers(part).get("content-type", ""))
    return m.group(1) if m else ""


def decode_part(part: dict[str, Any]) -> str:
    """Inline body text of one payload part (base64url + charset)."""
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    return decode_bytes(b64url_decode(data), _part_charset(part))


def _is_attachment(part: dict[str, Any]) -> bool:
    disp = _headers(part).get("content-disposition", "").lower()
    return bool(part.get("filename")) or disp.startswith("attachment") or bool(
        (part.get("body") or {}).get("attachmentId") and not str(part.get("mimeType", "")).startswith("text/")
    )


def _attachment_row(part: dict[str, Any]) -> dict[str, Any]:
    hdrs = _headers(part)
    body = part.get("body") or {}
    cid = hdrs.get("content-id", "").strip("<> ")
    return {
        "filename": decode_header_value(part.get("filename") or "") or "attachment",
        "mimeType": str(part.get("mimeType") or "application/octet-stream"),
        "size": int(body.get("size") or 0),
        "attachmentId": str(body.get("attachmentId") or ""),
        "partId": str(part.get("partId") or ""),
        "inline": hdrs.get("content-disposition", "").lower().startswith("inline") or bool(cid),
        "contentId": cid,
    }


def list_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [_attachment_row(p) for p in walk_parts(payload) if not p.get("parts") and _is_attachment(p)]


def _summary(headers: dict[str, str]) -> dict[str, Any]:
    return {
        "headers": headers,
        "subject": headers.get("subject", ""),
        "from": (parse_addresses(headers.get("from")) or [None])[0],
        "to": parse_addresses(headers.get("to")),
        "cc": parse_addresses(headers.get("cc")),
        "reply_to": parse_addresses(headers.get("reply-to")),
        "date": headers.get("date", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
    }


def parse_message(msg: dict[str, Any]) -> dict[str, Any]:
    """A Gmail API message (format=full or metadata) → flat dict."""
    payload = msg.get("payload") or {}
    text = html = ""
    for part in walk_parts(payload):
        if part.get("parts") or _is_attachment(part):
            continue
        mime = str(part.get("mimeType") or "").lower()
        if mime == "text/plain" and not text:
            text = decode_part(part)
        elif mime == "text/html" and not html:
            html = decode_part(part)
    out = {
        "id": msg.get("id", ""),
        "threadId": msg.get("threadId", ""),
        "labelIds": list(msg.get("labelIds") or []),
        "snippet": html_lib.unescape(str(msg.get("snippet") or "")),
        "internalDate": msg.get("internalDate", ""),
        "historyId": msg.get("historyId", ""),
        **_summary(_headers(payload)),
        "text": text or (html_to_text(html) if html else ""),
        "html": html,
        "attachments": list_attachments(payload),
    }
    return out


# ── parse: raw RFC 2822 ────────────────────────────────────────────────────


def parse_raw(raw: bytes | str, *, thread_id: str = "") -> dict[str, Any]:
    """RFC 2822 bytes (or Gmail's base64url ``raw``) → the same flat dict.

    Attachments here carry their ``data`` bytes, since the raw message has them.
    """
    if isinstance(raw, str):
        raw = b64url_decode(raw)
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    headers: dict[str, str] = {}
    for name, value in msg.items():
        headers.setdefault(name.lower(), decode_header_value(str(value)))
    text = html = ""
    attachments: list[dict[str, Any]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        filename = part.get_filename()
        if disp == "attachment" or filename:
            data = part.get_payload(decode=True) or b""
            cid = str(part.get("Content-ID") or "").strip("<> ")
            attachments.append(
                {
                    "filename": filename or "attachment",
                    "mimeType": part.get_content_type(),
                    "size": len(data),
                    "data": data,
                    "inline": disp == "inline" or bool(cid),
                    "contentId": cid,
                }
            )
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not text:
            text = _part_text(part)
        elif ctype == "text/html" and not html:
            html = _part_text(part)
    return {
        "id": "",
        "threadId": thread_id,
        **_summary(headers),
        "text": text or (html_to_text(html) if html else ""),
        "html": html,
        "attachments": attachments,
    }


def _part_text(part: Message) -> str:
    data = part.get_payload(decode=True) or b""
    return decode_bytes(data, part.get_content_charset() or "")


# ── build ──────────────────────────────────────────────────────────────────


def build_message(
    *,
    to: Any,
    subject: str = "",
    text: str = "",
    html: str = "",
    cc: Any = None,
    bcc: Any = None,
    sender: Any = None,
    reply_to: Any = None,
    attachments: Iterable[dict[str, Any]] | None = None,
    in_reply_to: str = "",
    references: str = "",
) -> bytes:
    """RFC 2822 bytes (CRLF). attachments: [{filename, data: bytes, mimeType?}]."""
    msg = EmailMessage(policy=policy.SMTP)
    for name, value in (("From", sender), ("To", to), ("Cc", cc), ("Bcc", bcc), ("Reply-To", reply_to)):
        formatted = format_addresses(value)
        if formatted:
            msg[name] = formatted
    if not (msg["To"] or msg["Cc"] or msg["Bcc"]):
        raise ValueError("at least one recipient (to, cc or bcc) is required")
    msg["Subject"] = _clean_header(subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="mail.gmail.com")
    if in_reply_to:
        msg["In-Reply-To"] = _clean_header(in_reply_to)
    if references:
        msg["References"] = _clean_header(references)
    msg.set_content(text or (html_to_text(html) if html else ""))
    if html:
        msg.add_alternative(html, subtype="html")
    for att in attachments or []:
        data = att.get("data")
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("attachment data must be bytes")
        mime = str(att.get("mimeType") or att.get("mime_type") or "application/octet-stream")
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(
            bytes(data),
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=_clean_header(str(att.get("filename") or "attachment")),
        )
    return msg.as_bytes()


def reply_subject(subject: str) -> str:
    s = (subject or "").strip()
    return s if _RE_PREFIX.match(s) else f"Re: {s}".rstrip()


def forward_subject(subject: str) -> str:
    s = (subject or "").strip()
    return s if _FWD_PREFIX.match(s) else f"Fwd: {s}".rstrip()


def _quote(original: dict[str, Any]) -> str:
    who = original.get("from") or {}
    name = formataddr((who.get("name", ""), who.get("email", ""))) if who else "someone"
    body = "\n".join(f"> {ln}" if ln else ">" for ln in (original.get("text") or "").splitlines())
    return f"On {original.get('date') or 'an earlier date'}, {name} wrote:\n{body}"


def build_reply(
    original: dict[str, Any],
    *,
    text: str = "",
    html: str = "",
    reply_all: bool = False,
    self_email: str = "",
    attachments: Iterable[dict[str, Any]] | None = None,
    quote: bool = True,
    sender: Any = None,
) -> dict[str, Any]:
    """Reply to a parsed message → {raw: bytes, threadId, to, cc}.

    To is Reply-To (else From); reply_all adds the original To/Cc minus
    ``self_email``. In-Reply-To / References point at the original.
    """
    me = (self_email or "").lower()
    to = list(original.get("reply_to") or ([original["from"]] if original.get("from") else []))
    if me and _emails(to) == {me}:
        # replying to my own message: go back to its recipients
        to = list(original.get("to") or [])
    cc: list[dict[str, str]] = []
    if reply_all:
        seen = _emails(to) | ({me} if me else set())
        for a in list(original.get("to") or []) + list(original.get("cc") or []):
            if a["email"].lower() not in seen:
                seen.add(a["email"].lower())
                cc.append(a)
    to = [a for a in to if a["email"].lower() != me] or to
    if not to:
        raise ValueError("the original message has no sender to reply to")
    mid = str(original.get("message_id") or "").strip()
    refs = " ".join(x for x in (str(original.get("references") or "").strip(), mid) if x)
    body = text or (html_to_text(html) if html else "")
    if quote and original.get("text"):
        body = f"{body}\n\n{_quote(original)}" if body else _quote(original)
    raw = build_message(
        to=to,
        cc=cc,
        sender=sender,
        subject=reply_subject(original.get("subject", "")),
        text=body,
        html=html,
        attachments=attachments,
        in_reply_to=mid,
        references=refs,
    )
    return {"raw": raw, "threadId": original.get("threadId", ""), "to": to, "cc": cc}


def build_forward(
    original: dict[str, Any],
    *,
    to: Any,
    text: str = "",
    cc: Any = None,
    attachments: Iterable[dict[str, Any]] | None = None,
    sender: Any = None,
) -> dict[str, Any]:
    """Forward a parsed message → {raw: bytes, threadId}.

    ``attachments`` are the files to carry (the original's, fetched by the
    caller, plus any new ones) as [{filename, data, mimeType}].
    """
    who = original.get("from") or {}
    header = [
        "---------- Forwarded message ---------",
        f"From: {formataddr((who.get('name', ''), who.get('email', ''))) if who else ''}",
        f"Date: {original.get('date', '')}",
        f"Subject: {original.get('subject', '')}",
        f"To: {format_addresses(original.get('to'))}",
    ]
    if original.get("cc"):
        header.append(f"Cc: {format_addresses(original.get('cc'))}")
    body = "\n".join(header) + "\n\n" + (original.get("text") or "")
    if text:
        body = f"{text}\n\n{body}"
    raw = build_message(
        to=to,
        cc=cc,
        sender=sender,
        subject=forward_subject(original.get("subject", "")),
        text=body,
        attachments=attachments,
    )
    return {"raw": raw, "threadId": original.get("threadId", "")}
