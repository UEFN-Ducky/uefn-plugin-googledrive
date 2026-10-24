"""Drive manage tools over a fake Drive HTTP server: organize, inverses, trash, share, usage."""
from __future__ import annotations

import importlib
import itertools
import json
import re
import sys
import types
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# backend/ as a bare package (its __init__ not run), so the relative imports resolve.
_pkg = sys.modules.setdefault("gw_backend_pkg", types.ModuleType("gw_backend_pkg"))
_pkg.__path__ = [str(ROOT / "backend")]
td = importlib.import_module("gw_backend_pkg.tools_drive")

ME = "me@example.com"
WORK = "work@example.com"
FOLDER_A = "folderAAAAAAAAAA"
FOLDER_B = "folderBBBBBBBBBB"
DOC = "fileDOC000000001"
PIC = "filePIC000000002"
SESSION = td.UPLOAD_URL + "?upload_id=sess1"


class FakeDrive:
    """In-memory Drive v3 behind the ``http(method, url, headers, data, timeout)`` the tools use."""

    _ids = itertools.count(1)

    def __init__(self):
        self.files = {
            "root": {"id": "root", "name": "My Drive", "mimeType": td.FOLDER_MIME, "parents": []},
            FOLDER_A: {"id": FOLDER_A, "name": "Assets", "mimeType": td.FOLDER_MIME, "parents": ["root"]},
            FOLDER_B: {"id": FOLDER_B, "name": "Archive", "mimeType": td.FOLDER_MIME, "parents": ["root"]},
            DOC: {"id": DOC, "name": "notes.txt", "mimeType": "text/plain", "parents": [FOLDER_A], "size": "12"},
            PIC: {"id": PIC, "name": "hero.png", "mimeType": "image/png", "parents": [FOLDER_A], "size": "2048"},
        }
        self.permissions: list[dict] = []
        self.requests: list[tuple[str, str, dict]] = []
        self.upload = None
        self.uploaded = b""

    def _reply(self, status, body=None, headers=None):
        return status, headers or {}, json.dumps(body or {}).encode()

    def _need(self, fid):
        return self.files.get(fid)

    def __call__(self, method, url, *, headers=None, data=None, timeout=60.0):
        parsed = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self.requests.append((method, parsed.path, q))
        assert headers["Authorization"] == f"Bearer tok-{ME}" or headers["Authorization"].startswith("Bearer tok-")
        assert q.get("supportsAllDrives") == "true" or url.startswith(SESSION)
        body = json.loads(data.decode()) if data and headers.get("Content-Type", "").startswith("application/json") else {}
        path = parsed.path.replace("/drive/v3", "")
        if url.startswith(SESSION):
            return self._chunk(headers, data)
        if path == "/upload/files" and method == "POST":
            assert q["uploadType"] == "resumable"
            self.upload = {"meta": body, "size": int(headers["X-Upload-Content-Length"]),
                           "mime": headers["X-Upload-Content-Type"]}
            return 200, {"location": SESSION}, b""
        if path == "/about":
            return self._reply(200, {"storageQuota": {"limit": "1073741824", "usage": "536870912",
                                                      "usageInDrive": "500000000", "usageInDriveTrash": "1000"}})
        m = re.fullmatch(r"/files/([^/]+)(/copy|/permissions)?", path)
        if path == "/files" and method == "POST":
            return self._reply(200, self._new(body))
        if not m:
            return self._reply(404, {"error": {"message": "no route"}})
        fid, sub = m.group(1), m.group(2)
        f = self._need(fid)
        if f is None:
            return self._reply(404, {"error": {"code": 404, "message": f"File not found: {fid}."}})
        if sub == "/copy":
            return self._reply(200, self._new({"name": body.get("name") or f"Copy of {f['name']}",
                                               "mimeType": f["mimeType"], "parents": body.get("parents") or f["parents"]}))
        if sub == "/permissions":
            perm = {"id": f"perm{len(self.permissions) + 1}", **body, "notify": q.get("sendNotificationEmail")}
            self.permissions.append(perm)
            return self._reply(200, perm)
        if method == "GET":
            return self._reply(200, f)
        if method == "PATCH":
            for p in filter(None, q.get("removeParents", "").split(",")):
                f["parents"].remove(p)
            if q.get("addParents"):
                f["parents"].append(q["addParents"])
            f.update(body)
            return self._reply(200, f)
        return self._reply(405)

    def _new(self, body):
        fid = f"new{next(self._ids):013d}"
        self.files[fid] = {"id": fid, "name": body.get("name", ""), "mimeType": body.get("mimeType", "text/plain"),
                           "parents": list(body.get("parents") or ["root"])}
        return self.files[fid]

    def _chunk(self, headers, data):
        rng = headers["Content-Range"]
        total = self.upload["size"]
        if rng == "bytes */0":
            return self._reply(200, self._new(self.upload["meta"]))
        start, end = map(int, re.match(r"bytes (\d+)-(\d+)/\d+", rng).groups())
        assert start == len(self.uploaded)
        self.uploaded += data
        if end + 1 < total:
            return 308, {"range": f"bytes=0-{end}"}, b""
        return self._reply(200, {**self._new(self.upload["meta"]), "size": str(total)})


class FakeChangeset:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, **kwargs):
        self.records.append(kwargs)


class FakeApi:
    def __init__(self):
        self.changeset = FakeChangeset()
        self.tools: dict = {}
        self.rpcs: dict = {}

    def tool(self, **_kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco

    def register_panel_rpc(self, name, fn):
        self.rpcs[name] = fn

    def revert(self, record):
        """What the Changes tab's Revert does: run each inverse tool call."""
        return [self.tools[step["command"]](**step["params"]) for step in record["inverse"]]


def setup(accounts=None, **options):
    api, drive = FakeApi(), FakeDrive()
    rows = accounts if accounts is not None else [{"email": ME, "drive": True, "manage_drive": True}]
    tools = td.register(api, accounts=lambda: rows, token_for=lambda e: f"tok-{e}", http=drive, **options)
    return api, drive, tools


def test_register_adds_every_tool_and_rpc():
    api, _drive, _tools = setup()
    assert set(td.TOOL_NAMES) <= set(api.tools) and set(td.TOOL_NAMES) <= set(api.rpcs)


def test_file_ids_from_links_rows_and_lists():
    assert td.file_id(f"https://drive.google.com/file/d/{DOC}/view") == DOC
    assert td.file_id(f"https://docs.google.com/document/d/{DOC}/edit") == DOC
    assert td.file_ids([DOC, {"id": PIC}, f"{DOC}, {PIC}"]) == [DOC, PIC]
    for bad in ("https://evil.example.com/file/d/" + DOC, "x", ""):
        try:
            td.file_id(bad)
        except td.ToolError:
            continue
        raise AssertionError(bad)


def test_read_only_account_changes_nothing():
    api, drive, _t = setup([{"email": ME, "drive": True, "manage_drive": False}])
    for name, kw in (("gdrive_move", {"file": DOC, "folder": FOLDER_B}), ("gdrive_rename", {"file": DOC, "name": "x"}),
                     ("gdrive_trash", {"files": DOC, "confirmed": True}), ("gdrive_folder_create", {"name": "New"}),
                     ("gdrive_share", {"file": DOC, "email": "a@b.co"})):
        out = api.tools[name](**kw)
        assert out["ok"] is False and "Manage Drive is off" in out["error"] and "manage_drive=true" in out["error"]
    assert drive.requests == [] and api.changeset.records == []
    assert api.tools["gdrive_storage_usage"]()["ok"]  # reading is fine


def test_several_accounts_need_account():
    rows = [{"email": ME, "manage_drive": True}, {"email": WORK, "manage_drive": True}]
    api, drive, _t = setup(rows)
    assert "Several" in api.tools["gdrive_rename"](file=DOC, name="x")["error"]
    assert api.tools["gdrive_rename"](file=DOC, name="x", account=WORK)["ok"]
    assert api.tools["gdrive_storage_usage"]()["count"] == 2


def test_move_records_inverse_and_revert_moves_back():
    api, drive, _t = setup()
    out = api.tools["gdrive_move"](file=[DOC, PIC], folder=FOLDER_B)
    assert out["ok"] and out["moved"] == 2
    assert drive.files[DOC]["parents"] == [FOLDER_B] and drive.files[PIC]["parents"] == [FOLDER_B]
    rec = api.changeset.records[-1]
    assert rec["revertable"] == "auto" and rec["before"]["parents"][DOC] == [FOLDER_A]
    assert rec["inverse"] == [
        {"command": "gdrive_move", "params": {"file": [DOC], "folder": FOLDER_A, "account": ME}},
        {"command": "gdrive_move", "params": {"file": [PIC], "folder": FOLDER_A, "account": ME}},
    ]
    assert all(r["ok"] for r in api.revert(rec))
    assert drive.files[DOC]["parents"] == [FOLDER_A] and drive.files[PIC]["parents"] == [FOLDER_A]


def test_move_into_same_folder_is_a_no_op():
    api, _drive, _t = setup()
    out = api.tools["gdrive_move"](file=DOC, folder=FOLDER_A)
    assert out["ok"] and out["moved"] == 0 and api.changeset.records == []


def test_rename_records_inverse_and_revert_renames_back():
    api, drive, _t = setup()
    out = api.tools["gdrive_rename"](file=DOC, name="  plan\n.txt ")
    assert out["ok"] and out["changed"] and drive.files[DOC]["name"] == "plan.txt"
    rec = api.changeset.records[-1]
    assert rec["inverse"] == [{"command": "gdrive_rename", "params": {"file": DOC, "name": "notes.txt", "account": ME}}]
    api.revert(rec)
    assert drive.files[DOC]["name"] == "notes.txt"
    assert api.tools["gdrive_rename"](file=DOC, name="notes.txt")["changed"] is False


def test_trash_previews_then_trashes_and_revert_untrashes():
    api, drive, _t = setup()
    preview = api.tools["gdrive_trash"](files=[DOC, PIC])
    assert preview["ok"] and preview["preview"] and not preview["changed"]
    assert '"notes.txt"' in preview["message"] and "confirmed=true" in preview["next"]
    assert not drive.files[DOC].get("trashed") and api.changeset.records == []
    done = api.tools["gdrive_trash"](files=[DOC, PIC], confirmed=True)
    assert done["changed"] and drive.files[DOC]["trashed"] and drive.files[PIC]["trashed"]
    rec = api.changeset.records[-1]
    assert rec["inverse"] == [{"command": "gdrive_untrash", "params": {"files": [DOC, PIC], "account": ME}}]
    api.revert(rec)
    assert drive.files[DOC]["trashed"] is False and drive.files[PIC]["trashed"] is False
    untrash_rec = api.changeset.records[-1]
    assert untrash_rec["command"] == "gdrive_untrash"
    assert untrash_rec["inverse"][0]["params"] == {"files": [DOC, PIC], "confirmed": True, "account": ME}


def test_trash_refuses_over_50_and_unknown_files():
    api, drive, _t = setup()
    many = [f"file{i:012d}" for i in range(51)]
    out = api.tools["gdrive_trash"](files=many, confirmed=True)
    assert out["ok"] is False and "over the limit of 50" in out["error"] and drive.requests == []
    out = api.tools["gdrive_trash"](files="missingFile0001", confirmed=True)
    assert out["ok"] is False and "Nothing was changed" in out["error"]
    assert not any(m == "PATCH" for m, _p, _q in drive.requests)


def test_folder_create_and_copy_revert_by_trash():
    api, drive, _t = setup()
    out = api.tools["gdrive_folder_create"](name="Renders", parent=FOLDER_A)
    fid = out["folder"]["id"]
    assert out["folder"]["folder"] and drive.files[fid]["parents"] == [FOLDER_A]
    api.revert(api.changeset.records[-1])
    assert drive.files[fid]["trashed"] is True
    out = api.tools["gdrive_copy"](file=PIC, folder=FOLDER_B)
    cid = out["file"]["id"]
    assert drive.files[cid]["name"] == "Copy of hero.png" and drive.files[cid]["parents"] == [FOLDER_B]
    assert api.changeset.records[-1]["inverse"][0]["command"] == "gdrive_trash"


def test_upload_resumable_in_chunks(tmp_path):
    api, drive, _t = setup(chunk=256 * 1024)
    src = tmp_path / "level.fbx"
    payload = bytes(range(256)) * 3000  # 768000 bytes → 3 chunks
    src.write_bytes(payload)
    out = api.tools["gdrive_upload"](local_path=str(src), folder=FOLDER_A)
    assert out["ok"] and out["file"]["name"] == "level.fbx" and out["file"]["size"] == len(payload)
    assert drive.uploaded == payload and drive.upload["meta"] == {"name": "level.fbx", "parents": [FOLDER_A]}
    assert sum(1 for m, p, _q in drive.requests if m == "PUT") == 3
    assert api.changeset.records[-1]["inverse"][0]["params"]["files"] == [out["file"]["id"]]


def test_upload_refusals(tmp_path):
    api, _drive, _t = setup(max_upload_bytes=10)
    assert "No file" in api.tools["gdrive_upload"](local_path=str(tmp_path / "nope.png"))["error"]
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * 11)
    assert "limit" in api.tools["gdrive_upload"](local_path=str(big))["error"]
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert api.tools["gdrive_upload"](local_path=str(empty))["ok"]


def test_share_validates_and_records_manual():
    api, drive, _t = setup()
    assert "not an email" in api.tools["gdrive_share"](file=DOC, email="sam")["error"]
    assert "role must be" in api.tools["gdrive_share"](file=DOC, email="sam@example.com", role="owner")["error"]
    assert drive.permissions == []
    out = api.tools["gdrive_share"](file=DOC, email="sam@example.com", role="writer", notify=False)
    assert out["ok"] and out["permission"]["role"] == "writer"
    perm = drive.permissions[0]
    assert perm["type"] == "user" and perm["emailAddress"] == "sam@example.com" and perm["notify"] == "false"
    rec = api.changeset.records[-1]
    assert rec["command"] == "gdrive_share" and rec["revertable"] == "manual" and rec["inverse"] == []


def test_storage_usage_and_drive_errors():
    api, _drive, _t = setup()
    out = api.tools["gdrive_storage_usage"]()
    row = out["accounts"][0]
    assert row["percent"] == 50.0 and row["usage_in_trash"] == 1000 and "of 1.0 GB" in row["text"]
    out = api.tools["gdrive_rename"](file="missingFile0001", name="x")
    assert out["ok"] is False and "404" in out["error"]
