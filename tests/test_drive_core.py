"""register(api): auth.py + drive.py tools, every Gmail / Drive / account module, nodes and the poller."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1] / "backend"
_spec = importlib.util.spec_from_file_location(
    "gd_core_backend", _PKG / "__init__.py", submodule_search_locations=[str(_PKG)]
)
gd = importlib.util.module_from_spec(_spec)
sys.modules["gd_core_backend"] = gd
_spec.loader.exec_module(gd)
auth = sys.modules["gd_core_backend.auth"]
drive = sys.modules["gd_core_backend.drive"]

OWN_TOOLS = {
    "google_allowed_models",
    "gdrive_preview",
    "gdrive_list",
    "gdrive_search",
    "gdrive_file_info",
    "gdrive_download",
    "gdrive_import_to_uefn",
    "google_open",
    "google_open_pending",
}
MODULE_TOOLS = (
    set(gd.tools_account.TOOL_NAMES)
    | set(gd.tools_account.ALIAS_NAMES)
    | set(gd.tools_drive.TOOL_NAMES)
    | set(gd.tools_gmail.TOOL_NAMES)
)
GMAIL_NODES = {f"googledrive.{n}" for n in gd.nodes.NODE_METHODS}


class FakeData:
    def __init__(self) -> None:
        self.docs: dict[str, object] = {}

    def get(self, key, default=None):
        return self.docs.get(key, default)

    def put(self, key, value, **_kw):
        self.docs[key] = value

    def delete(self, key, **_kw):
        self.docs.pop(key, None)

    def items(self, prefix=""):
        return {k: v for k, v in self.docs.items() if k.startswith(prefix)}


class FakeApi:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}
        self.nodes: dict[str, object] = {}
        self.rpcs: dict[str, object] = {}
        self.duplicates: list[str] = []
        self.data = FakeData()
        self.search = None

    def tool(self, fn=None, *, name=None, intent=None, listener=True):
        def deco(f):
            key = name or f.__name__
            if key in self.tools:
                self.duplicates.append(key)
            self.tools[key] = {"fn": f, "listener": listener}
            return f

        return deco(fn) if fn else deco

    def register_pipeline_node(self, node_type, handler=None):
        def deco(f):
            self.nodes[node_type] = f
            return f

        return deco(handler) if handler else deco

    def register_panel_rpc(self, name, fn):
        self.rpcs[name] = fn

    def call_tool(self, name, arguments=None):
        raise RuntimeError("no panel open")

    def http_json(self, *a, **k):
        raise AssertionError("no Gmail request expected")

    def listener(self, *a, **k):
        raise RuntimeError("listener offline")

    def is_enabled(self) -> bool:
        return False  # the poller stays paused in tests

    def emit_automation(self, *a, **k):
        return {}

    def changeset(self, *a, **k):
        return None

    def local_dir(self, name: str = "") -> str:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def media_url(self, path: str) -> str:
        return "http://127.0.0.1:4199/media?path=" + Path(path).name

    def log(self, msg: str) -> None:
        pass


PNG_ID = "1A2b3C4d5E6f7G8h9I0j"
DOC_ID = "9Z8y7X6w5V4u3T2s1R0q"


class Resp:
    def __init__(self, body: bytes, headers: dict | None = None) -> None:
        self.body = body
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        out, self.body = (self.body, b"") if n < 0 else (self.body[:n], self.body[n:])
        return out

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class FakeDrive:
    """Drive v3 for two accounts: records which bearer token each request carried."""

    FILES = {
        PNG_ID: {"id": PNG_ID, "name": "duck.png", "mimeType": "image/png", "size": "4"},
        DOC_ID: {"id": DOC_ID, "name": "notes.fbx", "mimeType": "application/octet-stream", "size": "4"},
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def open(self, url, *, headers=None, data=None, method=None, timeout=60.0):
        auth._check_url(url)
        self.calls.append((url, (headers or {}).get("Authorization", "")))
        path = url.split("/drive/v3/", 1)[1]
        if path.startswith("files?"):
            return Resp(json.dumps({"files": [self.FILES[PNG_ID]]}).encode())
        file_id = path.split("files/", 1)[1].split("?", 1)[0]
        if "alt=media" in url:
            return Resp(b"\x89PNG", {"Content-Length": "4"})
        return Resp(json.dumps(self.FILES[file_id]).encode())


@pytest.fixture()
def api(monkeypatch, tmp_path):
    secrets: dict[str, str] = {}
    monkeypatch.setattr(auth, "_get_secret", lambda k: secrets.get(k, ""))
    monkeypatch.setattr(auth, "_set_secret", lambda k, v: secrets.__setitem__(k, v))
    monkeypatch.setattr(drive, "_staging_dir", lambda: tmp_path)
    monkeypatch.setattr(drive, "_prefs", lambda: {})
    monkeypatch.setattr(gd, "_staging_dir", lambda: tmp_path)
    fake = FakeApi()
    fake.secrets = secrets
    fake.root = tmp_path
    gd.register(fake)
    yield fake
    gd.shutdown()
    auth.bind(None)


def _call(api, name, **kw):
    out = api.tools[name]["fn"](**kw)
    return json.loads(out) if isinstance(out, str) else out


def test_register_wires_every_module(api):
    assert api.duplicates == []
    assert set(api.tools) == OWN_TOOLS | MODULE_TOOLS
    assert len(api.tools) >= 30
    # Only the import tool talks to the UEFN editor.
    assert [n for n, t in api.tools.items() if t["listener"]] == ["gdrive_import_to_uefn"]
    assert set(api.nodes) == {"googledrive.list", "googledrive.download"} | GMAIL_NODES
    assert {"google_open_pending", "google_status", "gmail_list", "gdrive_move"} <= set(api.rpcs)


def test_poller_runs_once_and_restarts_cleanly(api):
    first = gd._POLLER
    assert first is not None and first._thread is not None and first._thread.is_alive()
    gd.register(FakeApi())  # a reinstall: the old poller stops, one new poller runs
    assert gd._POLLER is not first and first._thread is None
    assert first.poll_once() == {"paused": True, "emitted": 0}


@pytest.mark.parametrize("role", ["tool_server", "agent_host"])
def test_no_poller_outside_the_app(role, api, monkeypatch):
    import types

    fake = types.ModuleType("backend.process_role")
    fake.role = lambda: role
    monkeypatch.setitem(sys.modules, "backend.process_role", fake)
    gd.register(FakeApi())
    assert gd._POLLER is None


def test_old_core_falls_back_to_the_bridge_flag(monkeypatch):
    monkeypatch.setitem(sys.modules, "backend.process_role", None)  # import raises ImportError
    monkeypatch.setenv("UEFN_DUCKY_MCP_BRIDGE", "1")
    assert gd._in_app() is False
    monkeypatch.delenv("UEFN_DUCKY_MCP_BRIDGE")
    assert gd._in_app() is True


def test_google_open_gives_link_without_panel(api):
    out = _call(api, "google_open", view="gmail")
    assert out["ok"] and out["navigated"] is False
    assert out["link"] == "plugin:googledrive/main?view=gmail"
    assert api.rpcs["google_open_pending"]()["view"] == "gmail"


def test_modules_own_their_parts():
    assert gd.extract_drive_id is drive.extract_drive_id
    assert gd.token_expired is auth.token_expired
    assert drive.KEY_API == auth.KEY_API == "gdrive_api_key"
    assert auth._host_ok("oauth2.googleapis.com") and not auth._host_ok("evil.com")


def test_status_signed_out_has_drive_details(api, tmp_path):
    for name in ("google_status", "gdrive_status"):
        out = _call(api, name)
        assert out["ok"], out
        assert out["count"] == 0 and out["sign_in_ready"] is False
        # No own client and no Google Connect client id in this build yet.
        assert out["sign_in_kind"] == "none"
        assert out["access_modes"] == ["public"] and out["staging_dir"] == str(tmp_path)


def test_accounts_come_from_auth(api):
    api.secrets[auth.KEY_TOKEN] = json.dumps({"access_token": "a", "refresh_token": "r", "email": "me@example.com"})
    rows = _call(api, "google_accounts_list")["accounts"]
    assert [r["email"] for r in rows] == ["me@example.com"]
    assert rows[0]["gmail"] is False and "access_token" not in rows[0]
    assert _call(api, "gmail_list")["ok"] is False  # Gmail is off for that account


def test_list_needs_access(api):
    out = _call(api, "gdrive_list")
    assert not out["ok"] and "API key" in out["error"]
    node = api.nodes["googledrive.list"]({"config": {}})
    assert node["ok"] is False


def _two_accounts(api, monkeypatch) -> FakeDrive:
    far = 4102444800  # 2100-01-01: tokens stay fresh, no refresh call
    accounts = [
        {"email": e, "scopes": [auth.SCOPE_DRIVE_READ], "access_token": t, "refresh_token": "r", "expires_at": far, "via": "broker"}
        for e, t in (("a@gmail.com", "tok-a"), ("b@gmail.com", "tok-b"))
    ]
    api.secrets[auth.KEY_ACCOUNTS] = json.dumps({"version": 1, "accounts": accounts})
    fake = FakeDrive()
    monkeypatch.setattr(auth, "_open", fake.open)
    return fake


def test_list_uses_the_named_accounts_token(api, monkeypatch):
    google = _two_accounts(api, monkeypatch)
    out = json.loads(api.rpcs["gdrive_list"](folder="root", account="b@gmail.com"))  # the Drive tab's call
    assert out["ok"] and out["folder_id"] == "root" and [f["name"] for f in out["files"]] == ["duck.png"]
    url, bearer = google.calls[-1]
    assert bearer == "Bearer tok-b" and "%27root%27+in+parents" in url
    _call(api, "gdrive_list", folder="root")
    assert google.calls[-1][1] == "Bearer tok-a"  # no account: the first one with Drive
    info = _call(api, "gdrive_file_info", file=PNG_ID, account="b@gmail.com")
    assert info["ok"] and info["name"] == "duck.png" and google.calls[-1][1] == "Bearer tok-b"
    bad = _call(api, "gdrive_list", account="nobody@gmail.com")
    assert bad["ok"] is False and "No connected Google account" in bad["error"]
    assert {"gdrive_list", "gdrive_search", "gdrive_file_info", "gdrive_import_to_uefn", "gdrive_preview"} <= set(api.rpcs)


def test_preview_returns_a_media_url_for_a_picture(api, monkeypatch, tmp_path):
    google = _two_accounts(api, monkeypatch)
    out = json.loads(api.rpcs["gdrive_preview"](file=PNG_ID, account="b@gmail.com"))
    assert out["ok"], out
    assert out["url"] == f"http://127.0.0.1:4199/media?path={PNG_ID}.png" and out["mime"] == "image/png"
    saved = tmp_path / "drive-previews" / f"{PNG_ID}.png"
    assert saved.read_bytes() == b"\x89PNG"
    assert all(bearer == "Bearer tok-b" for _u, bearer in google.calls)
    n = len(google.calls)
    assert json.loads(api.rpcs["gdrive_preview"](file=PNG_ID, account="b@gmail.com"))["ok"]
    assert not any("alt=media" in u for u, _b in google.calls[n:])  # cached: no second download
    refused = _call(api, "gdrive_preview", file=DOC_ID)
    assert refused["ok"] is False and "pictures only" in refused["error"]


def test_allowed_models_rpc(api, monkeypatch):
    # Host without a model gate (no api call, app module missing): empty list plus the reason.
    monkeypatch.setitem(sys.modules, "backend.uefn_plugins.model_gate", None)
    out = json.loads(api.rpcs["google_allowed_models"]())
    assert out == {"ok": True, "models": [], "reason": "host has no model gate"}
    # The app's model gate.
    gate = type(sys)("model_gate")
    gate.allowed_models = lambda pid: ["claude-opus-5-5"] if pid == "googledrive" else []
    monkeypatch.setitem(sys.modules, "backend.uefn_plugins.model_gate", gate)
    assert json.loads(api.rpcs["google_allowed_models"]()) == {"ok": True, "models": ["claude-opus-5-5"]}
    # A host api that offers it wins.
    api.allowed_models = lambda pid: ["gpt-6-luna"]
    assert gd.allowed_models(api) == {"ok": True, "models": ["gpt-6-luna"]}


def test_download_goes_through_drive(api, monkeypatch, tmp_path):
    monkeypatch.setattr(drive, "_fetch_metadata", lambda fid, account="": ({"id": fid, "name": "duck.fbx", "size": 3}, "oauth", {}))

    def fake_stream(file_id, mode, tmp, cap, md5, account=""):
        Path(tmp).write_bytes(b"abc")
        return {"bytes": 3, "sha256": "x"}

    monkeypatch.setattr(drive, "_download_stream", fake_stream)
    out = _call(api, "gdrive_download", file="1A2b3C4d5E6f7G8h9I0j")
    assert out["ok"] and out["kind"] == "model"
    assert Path(out["path"]).read_bytes() == b"abc"
    assert Path(out["path"]).parent == tmp_path
