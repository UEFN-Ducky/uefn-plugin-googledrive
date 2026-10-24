"""backend/mime.py: parse Gmail payloads and raw mail, build send/reply/forward."""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "backend" / "mime.py"
_spec = importlib.util.spec_from_file_location("gdrive_mime", _PATH)
mime = importlib.util.module_from_spec(_spec)
sys.modules["gdrive_mime"] = mime
_spec.loader.exec_module(mime)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def gmail_full_message() -> dict:
    """A Gmail API format=full message: alternative text/html + a PDF."""
    return {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Hi there &amp; welcome",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "=?utf-8?q?Jos=C3=A9_P=C3=A9rez?= <jose@example.com>"},
                {"name": "To", "value": "me@example.com, Ann <ann@example.com>"},
                {"name": "Cc", "value": "bob@example.com"},
                {"name": "Subject", "value": "Subject"},
                {"name": "Date", "value": "Mon, 19 Oct 2026 10:00:00 +0000"},
                {"name": "Message-ID", "value": "<orig-1@example.com>"},
                {"name": "References", "value": "<root-0@example.com>"},
            ],
            "parts": [
                {
                    "partId": "0",
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "partId": "0.0",
                            "mimeType": "text/plain",
                            "headers": [{"name": "Content-Type", "value": 'text/plain; charset="ISO-8859-1"'}],
                            "body": {"size": 10, "data": b64u("Olá mundo\nline 2".encode("latin-1"))},
                        },
                        {
                            "partId": "0.1",
                            "mimeType": "text/html",
                            "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
                            "body": {"size": 20, "data": b64u("<p>Olá <b>mundo</b></p>".encode())},
                        },
                    ],
                },
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "plan.pdf",
                    "headers": [{"name": "Content-Disposition", "value": 'attachment; filename="plan.pdf"'}],
                    "body": {"size": 1234, "attachmentId": "ATT1"},
                },
                {
                    "partId": "2",
                    "mimeType": "image/png",
                    "filename": "logo.png",
                    "headers": [
                        {"name": "Content-Disposition", "value": "inline"},
                        {"name": "Content-ID", "value": "<logo@x>"},
                    ],
                    "body": {"size": 99, "attachmentId": "ATT2"},
                },
            ],
        },
    }


def test_parse_gmail_payload_text_html_charset_and_attachments():
    p = mime.parse_message(gmail_full_message())
    assert p["id"] == "m1" and p["threadId"] == "t1"
    assert p["subject"] == "Subject"
    assert p["from"] == {"name": "José Pérez", "email": "jose@example.com"}
    assert [a["email"] for a in p["to"]] == ["me@example.com", "ann@example.com"]
    assert p["to"][1]["name"] == "Ann"
    assert p["text"] == "Olá mundo\nline 2"
    assert p["html"] == "<p>Olá <b>mundo</b></p>"
    assert p["snippet"] == "Hi there & welcome"
    assert p["message_id"] == "<orig-1@example.com>"
    atts = {a["filename"]: a for a in p["attachments"]}
    assert atts["plan.pdf"]["attachmentId"] == "ATT1"
    assert atts["plan.pdf"]["size"] == 1234 and atts["plan.pdf"]["inline"] is False
    assert atts["logo.png"]["inline"] is True and atts["logo.png"]["contentId"] == "logo@x"


def test_html_only_message_gets_text_and_single_part_body():
    msg = {
        "id": "m2",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Subject", "value": "Hello"}],
            "body": {"data": b64u(b"<html><head><style>p{}</style></head><body><p>One</p><p>Two &amp; three</p></body></html>")},
        },
    }
    p = mime.parse_message(msg)
    assert p["text"] == "One\nTwo & three"
    assert p["attachments"] == []


def test_metadata_only_message_has_headers_and_no_body():
    msg = {"id": "m3", "payload": {"headers": [{"name": "From", "value": "a@x.com"}, {"name": "Subject", "value": "S"}]}}
    p = mime.parse_message(msg)
    assert p["from"]["email"] == "a@x.com" and p["text"] == "" and p["attachments"] == []


def test_unknown_charset_falls_back_to_utf8():
    part = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=x-bogus"}],
        "body": {"data": b64u("ünï".encode())},
    }
    assert mime.decode_part(part) == "ünï"


def test_address_parsing_and_formatting():
    addrs = mime.parse_addresses('"Doe, Jane" <jane@x.com>, bob@y.org, not-an-address')
    assert addrs == [{"name": "Doe, Jane", "email": "jane@x.com"}, {"name": "", "email": "bob@y.org"}]
    assert mime.format_addresses(addrs) == '"Doe, Jane" <jane@x.com>, bob@y.org'
    assert mime.format_addresses(["a@x.com", {"name": "B", "email": "b@x.com"}]) == "a@x.com, B <b@x.com>"
    assert mime.parse_addresses(None) == []


def test_build_rejects_header_injection_and_no_recipient():
    with pytest.raises(ValueError):
        mime.build_message(to="a@x.com", subject="hi\r\nBcc: evil@x.com", text="x")
    with pytest.raises(ValueError):
        mime.build_message(to="", subject="hi", text="x")


def test_reply_with_attachment_round_trips_and_stays_in_thread():
    original = mime.parse_message(gmail_full_message())
    pdf = b"%PDF-1.4\n" + bytes(range(256)) * 4
    reply = mime.build_reply(
        original,
        text="Thanks!",
        html="<p>Thanks!</p>",
        reply_all=True,
        self_email="me@example.com",
        sender="me@example.com",
        attachments=[{"filename": "notes.pdf", "data": pdf, "mimeType": "application/pdf"}],
    )
    assert reply["threadId"] == "t1"
    assert isinstance(reply["raw"], bytes) and b"\r\n" in reply["raw"]

    back = mime.parse_raw(reply["raw"], thread_id=reply["threadId"])
    assert back["threadId"] == "t1"
    assert back["subject"] == "Re: Subject"
    assert [a["email"] for a in back["to"]] == ["jose@example.com"]
    assert back["to"][0]["name"] == "José Pérez"
    assert sorted(a["email"] for a in back["cc"]) == ["ann@example.com", "bob@example.com"]
    assert back["in_reply_to"] == "<orig-1@example.com>"
    assert back["references"] == "<root-0@example.com> <orig-1@example.com>"
    assert back["text"].startswith("Thanks!")
    assert "> Olá mundo" in back["text"]
    assert back["html"].strip() == "<p>Thanks!</p>"
    assert len(back["attachments"]) == 1
    att = back["attachments"][0]
    assert att["filename"] == "notes.pdf" and att["mimeType"] == "application/pdf"
    assert att["data"] == pdf

    # Gmail's base64url "raw" form parses the same; a second reply stays "Re: Subject"
    again = mime.parse_raw(base64.urlsafe_b64encode(reply["raw"]).decode())
    assert again["subject"] == "Re: Subject"
    second = mime.build_reply(again, text="ok", self_email="jose@example.com")
    assert [a["email"] for a in second["to"]] == ["me@example.com"]
    assert second["raw"].count(b"Subject: Re: Subject") == 1
    assert mime.parse_raw(second["raw"])["references"].endswith(again["message_id"])


def test_reply_prefers_reply_to_and_own_message_goes_to_recipients():
    original = {
        "threadId": "t9",
        "subject": "RE: Plans",
        "from": {"name": "", "email": "me@example.com"},
        "reply_to": [],
        "to": [{"name": "Kim", "email": "kim@example.com"}],
        "cc": [],
        "message_id": "<x@y>",
        "text": "",
    }
    r = mime.build_reply(original, text="follow-up", self_email="Me@Example.com")
    assert [a["email"] for a in r["to"]] == ["kim@example.com"]
    back = mime.parse_raw(r["raw"])
    assert back["subject"] == "RE: Plans"
    assert back["references"] == "<x@y>"

    original2 = dict(original, **{"from": {"name": "", "email": "list@x.com"}, "reply_to": [{"name": "", "email": "owner@x.com"}]})
    assert [a["email"] for a in mime.build_reply(original2, text="hi")["to"]] == ["owner@x.com"]


def test_forward_keeps_attachments_and_prefix():
    original = mime.parse_message(gmail_full_message())
    fwd = mime.build_forward(
        original,
        to="team@example.com",
        text="FYI",
        attachments=[{"filename": "plan.pdf", "data": b"PDFDATA", "mimeType": "application/pdf"}],
    )
    back = mime.parse_raw(fwd["raw"])
    assert back["subject"] == "Fwd: Subject"
    assert back["text"].startswith("FYI")
    assert "---------- Forwarded message ---------" in back["text"]
    assert "Olá mundo" in back["text"]
    assert [a["filename"] for a in back["attachments"]] == ["plan.pdf"]
    assert back["attachments"][0]["data"] == b"PDFDATA"
    assert mime.forward_subject("Fwd: x") == "Fwd: x"
    assert mime.reply_subject("") == "Re:"


def test_build_new_message_with_bcc_and_unicode_subject():
    raw = mime.build_message(
        to=[{"name": "Zoë", "email": "zoe@example.com"}],
        bcc="hidden@example.com",
        subject="Grüße 👋",
        text="hello",
    )
    back = mime.parse_raw(raw)
    assert back["subject"] == "Grüße 👋"
    assert back["to"] == [{"name": "Zoë", "email": "zoe@example.com"}]
    assert back["headers"]["bcc"] == "hidden@example.com"
    assert back["text"].strip() == "hello"
