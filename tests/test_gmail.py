"""backend/gmail.py against a fake http_json (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "backend" / "gmail.py"
_spec = importlib.util.spec_from_file_location("gdrive_gmail", _PATH)
gmail = importlib.util.module_from_spec(_spec)
sys.modules["gdrive_gmail"] = gmail
_spec.loader.exec_module(gmail)


class FakeHttpError(Exception):
    def __init__(self, status, detail=None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.detail = detail or {}


class FakeHttp:
    """Answers from a queue per (method, path); records every call."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def on(self, method, path, *responses):
        self.routes.setdefault((method, path), []).extend(responses)

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=60):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.split("/users/me/", 1)[1]
        query = urllib.parse.parse_qs(parsed.query)
        self.calls.append({"method": method, "path": path, "query": query, "body": json_body, "headers": headers})
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected {method} {path}")
        res = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(res, Exception):
            raise res
        return json.loads(json.dumps(res))


def client(http, **kw):
    kw.setdefault("sleep", lambda s: None)
    return gmail.GmailClient(http, lambda: "tok-1", **kw)


def test_list_unread_returns_ids_and_next_page_token():
    http = FakeHttp()
    msgs = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(20)]
    http.on("GET", "messages", {"messages": msgs, "nextPageToken": "NEXT", "resultSizeEstimate": 57})
    page = client(http).list_messages(q="is:unread", max=20)
    assert page["ids"] == [f"m{i}" for i in range(20)]
    assert page["nextPageToken"] == "NEXT"
    assert page["resultSizeEstimate"] == 57
    call = http.calls[0]
    assert call["query"] == {"q": ["is:unread"], "maxResults": ["20"]}
    assert call["headers"]["Authorization"] == "Bearer tok-1"
    assert len(http.calls) == 1


def test_list_pages_until_max():
    http = FakeHttp()
    http.on(
        "GET",
        "messages",
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "P2"},
        {"messages": [{"id": "c"}], "nextPageToken": ""},
    )
    page = client(http).list_messages(max=5, label_ids=["INBOX", "UNREAD"])
    assert page["ids"] == ["a", "b", "c"]
    assert page["nextPageToken"] == ""
    assert http.calls[0]["query"]["labelIds"] == ["INBOX", "UNREAD"]
    assert http.calls[1]["query"]["pageToken"] == ["P2"]
    assert http.calls[1]["query"]["maxResults"] == ["3"]


def test_get_message_metadata_headers_and_bad_format():
    http = FakeHttp()
    http.on("GET", "messages/m1", {"id": "m1", "payload": {}})
    c = client(http)
    assert c.get_message("m1", "metadata", metadata_headers=["From", "Subject"])["id"] == "m1"
    assert http.calls[0]["query"] == {"format": ["metadata"], "metadataHeaders": ["From", "Subject"]}
    with pytest.raises(gmail.GmailError):
        c.get_message("m1", "html")
    with pytest.raises(gmail.GmailError):
        c.get_thread("t1", "raw")


def test_threads_and_modify_and_trash():
    http = FakeHttp()
    http.on("GET", "threads", {"threads": [{"id": "t1"}, {"id": "t2"}]})
    http.on("GET", "threads/t1", {"id": "t1", "messages": [{"id": "m1"}]})
    http.on("POST", "threads/t1/modify", {"id": "t1"})
    http.on("POST", "messages/m1/modify", {"id": "m1"})
    http.on("POST", "messages/m1/trash", {"id": "m1", "labelIds": ["TRASH"]})
    http.on("POST", "messages/m1/untrash", {"id": "m1"})
    http.on("POST", "threads/t1/trash", {"id": "t1"})
    http.on("POST", "threads/t1/untrash", {"id": "t1"})
    c = client(http)
    assert c.list_threads(q="from:x")["ids"] == ["t1", "t2"]
    assert c.get_thread("t1")["messages"][0]["id"] == "m1"
    c.modify_thread("t1", remove="INBOX")
    c.modify_message("m1", add=["Label_1"], remove=["UNREAD"])
    assert c.trash_message("m1")["labelIds"] == ["TRASH"]
    c.untrash_message("m1")
    c.trash_thread("t1")
    c.untrash_thread("t1")
    assert http.calls[2]["body"] == {"addLabelIds": [], "removeLabelIds": ["INBOX"]}
    assert http.calls[3]["body"] == {"addLabelIds": ["Label_1"], "removeLabelIds": ["UNREAD"]}


def test_batch_modify_chunks_by_1000():
    http = FakeHttp()
    http.on("POST", "messages/batchModify", {})
    res = client(http).batch_modify([f"m{i}" for i in range(2500)], add=["STARRED"])
    assert res == {"ok": True, "count": 2500}
    assert [len(c["body"]["ids"]) for c in http.calls] == [1000, 1000, 500]


def test_labels_crud():
    http = FakeHttp()
    http.on("GET", "labels", {"labels": [{"id": "INBOX", "name": "INBOX"}]})
    http.on("POST", "labels", {"id": "Label_9", "name": "Clients"})
    http.on("PATCH", "labels/Label_9", {"id": "Label_9", "name": "Clients/Active"})
    http.on("DELETE", "labels/Label_9", {})
    http.on("GET", "labels/Label_9", {"id": "Label_9"})
    c = client(http)
    assert c.list_labels()[0]["id"] == "INBOX"
    assert c.create_label("Clients")["id"] == "Label_9"
    assert http.calls[1]["body"]["name"] == "Clients"
    assert c.update_label("Label_9", name="Clients/Active", color=None)["name"] == "Clients/Active"
    assert http.calls[2]["body"] == {"name": "Clients/Active"}
    assert c.get_label("Label_9")["id"] == "Label_9"
    assert c.delete_label("Label_9") == {"ok": True, "id": "Label_9"}
    with pytest.raises(gmail.GmailError):
        c.create_label("  ")


def test_drafts_and_send_raw_encode_bytes():
    raw = b"To: a@example.com\r\nSubject: Hi\r\n\r\nBody"
    http = FakeHttp()
    http.on("POST", "drafts", {"id": "d1", "message": {"id": "m1", "threadId": "t1"}})
    http.on("PUT", "drafts/d1", {"id": "d1"})
    http.on("GET", "drafts", {"drafts": [{"id": "d1"}]})
    http.on("GET", "drafts/d1", {"id": "d1"})
    http.on("POST", "drafts/send", {"id": "m2", "threadId": "t1", "labelIds": ["SENT"]})
    http.on("POST", "messages/send", {"id": "m3", "threadId": "t1"})
    http.on("DELETE", "drafts/d1", {})
    c = client(http)
    c.create_draft(raw, thread_id="t1")
    body = http.calls[0]["body"]
    assert body["message"]["threadId"] == "t1"
    assert gmail.b64url_decode(body["message"]["raw"]) == raw
    assert "=" not in body["message"]["raw"]
    c.update_draft("d1", raw)
    assert http.calls[1]["body"]["id"] == "d1"
    assert c.list_drafts()["ids"] == ["d1"]
    c.get_draft("d1")
    assert c.send_draft("d1")["labelIds"] == ["SENT"]
    assert http.calls[4]["body"] == {"id": "d1"}
    c.send_raw(raw, thread_id="t1")
    assert gmail.b64url_decode(http.calls[5]["body"]["raw"]) == raw
    # an already-encoded string passes through untouched
    c.send_raw("QUJD")
    assert http.calls[6]["body"] == {"raw": "QUJD"}
    assert c.delete_draft("d1")["ok"] is True


def test_attachment_is_decoded():
    data = bytes(range(256))
    http = FakeHttp()
    http.on("GET", "messages/m1/attachments/a%2F1", {"size": 256, "data": gmail.b64url_encode(data)})
    res = client(http).get_attachment("m1", "a/1")
    assert res == {"size": 256, "data": data}


def test_history_pages_and_expired():
    http = FakeHttp()
    http.on(
        "GET",
        "history",
        {"history": [{"id": "101"}], "historyId": "105", "nextPageToken": "H2"},
        {"history": [{"id": "104"}], "historyId": "106"},
    )
    c = client(http)
    res = c.list_history(100, history_types=["messageAdded", "labelRemoved"])
    assert [h["id"] for h in res["history"]] == ["101", "104"]
    assert res["historyId"] == "106"
    assert http.calls[0]["query"]["historyTypes"] == ["messageAdded", "labelRemoved"]
    assert http.calls[0]["query"]["startHistoryId"] == ["100"]

    old = FakeHttp()
    old.on("GET", "history", FakeHttpError(404, {"error": {"message": "Requested entity was not found."}}))
    with pytest.raises(gmail.HistoryExpired):
        client(old).list_history("1")


def test_retries_429_and_5xx_with_backoff():
    sleeps = []
    http = FakeHttp()
    http.on("GET", "profile", FakeHttpError(429), FakeHttpError(503), {"emailAddress": "x", "historyId": "9"})
    c = gmail.GmailClient(http, lambda: "tok", sleep=sleeps.append, backoff_s=1.0)
    assert c.get_profile()["historyId"] == "9"
    assert len(http.calls) == 3
    assert len(sleeps) == 2 and 1.0 <= sleeps[0] < 1.5 and 2.0 <= sleeps[1] < 2.5


def test_rate_limit_403_retries_but_other_403_does_not():
    rate = {"error": {"message": "Rate", "errors": [{"reason": "userRateLimitExceeded"}]}}
    http = FakeHttp()
    http.on("GET", "profile", FakeHttpError(403, rate), {"historyId": "1"})
    assert client(http).get_profile()["historyId"] == "1"

    denied = {"error": {"message": "Insufficient Permission", "errors": [{"reason": "insufficientPermissions"}]}}
    http2 = FakeHttp()
    http2.on("GET", "profile", FakeHttpError(403, denied))
    with pytest.raises(gmail.GmailError) as info:
        client(http2).get_profile()
    assert info.value.status == 403
    assert info.value.reason == "insufficientPermissions"
    assert str(info.value) == "Insufficient Permission"
    assert len(http2.calls) == 1


def test_gives_up_after_max_retries():
    http = FakeHttp()
    http.on("GET", "labels", FakeHttpError(500))
    with pytest.raises(gmail.GmailError) as info:
        client(http, max_retries=2).list_labels()
    assert info.value.status == 500
    assert len(http.calls) == 3


def test_no_token_and_new_token_each_try():
    http = FakeHttp()
    http.on("GET", "profile", {"historyId": "1"})
    with pytest.raises(gmail.GmailError) as info:
        gmail.GmailClient(http, lambda: "").get_profile()
    assert info.value.status == 401
    assert http.calls == []

    tokens = iter(["old", "new"])
    http2 = FakeHttp()
    http2.on("GET", "profile", FakeHttpError(502), {"historyId": "2"})
    gmail.GmailClient(http2, lambda: next(tokens), sleep=lambda s: None).get_profile()
    assert [c["headers"]["Authorization"] for c in http2.calls] == ["Bearer old", "Bearer new"]


def test_ids_are_quoted_and_required():
    http = FakeHttp()
    c = client(http)
    with pytest.raises(gmail.GmailError):
        c.get_message("")
    http.on("GET", "messages/..%2Fx", {"id": "..x"})
    c.get_message("../x")
    assert http.calls[0]["path"] == "messages/..%2Fx"
