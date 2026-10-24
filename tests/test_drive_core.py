"""Backend split: auth.py + drive.py behind register(api) — 8 gdrive_* tools, 2 nodes, same behavior."""

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

TOOLS = {
    "gdrive_status",
    "gdrive_connect",
    "gdrive_disconnect",
    "gdrive_list",
    "gdrive_search",
    "gdrive_file_info",
    "gdrive_download",
    "gdrive_import_to_uefn",
    "google_open",
    "google_open_pending",
}


class FakeApi:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}
        self.nodes: dict[str, object] = {}
        self.rpcs: dict[str, object] = {}

    def tool(self, fn=None, *, name=None, intent=None, listener=True):
        def deco(f):
            self.tools[name or f.__name__] = {"fn": f, "listener": listener}
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

    def listener(self, *a, **k):
        raise RuntimeError("listener offline")

    def log(self, msg: str) -> None:
        pass


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
    gd.register(fake)
    return fake


def _call(api, name, **kw):
    return json.loads(api.tools[name]["fn"](**kw))


def test_register_keeps_tools_and_nodes(api):
    assert set(api.tools) == TOOLS
    # Only the import tool talks to the UEFN editor.
    assert [n for n, t in api.tools.items() if t["listener"]] == ["gdrive_import_to_uefn"]
    assert set(api.nodes) == {"googledrive.list", "googledrive.download"}
    assert "google_open_pending" in api.rpcs


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


def test_status_signed_out(api):
    out = _call(api, "gdrive_status")
    assert out["ok"], out
    assert out["connected"] is False
    # No own client and no Google Connect client id in this build yet.
    assert out["sign_in_kind"] == "none" and out["sign_in_ready"] is False
    assert out["access_modes"] == ["public"]


def test_disconnect_erases_token_without_network(api):
    api.secrets[auth.KEY_TOKEN] = json.dumps({"access_token": "a", "refresh_token": "r"})
    out = _call(api, "gdrive_disconnect", revoke=False)
    assert out["ok"] and out["revoked_at_google"] is False
    assert api.secrets[auth.KEY_TOKEN] == ""


def test_list_needs_access(api):
    out = _call(api, "gdrive_list")
    assert not out["ok"] and "API key" in out["error"]
    node = api.nodes["googledrive.list"]({"config": {}})
    assert node["ok"] is False


def test_download_goes_through_drive(api, monkeypatch, tmp_path):
    monkeypatch.setattr(drive, "_fetch_metadata", lambda fid: ({"id": fid, "name": "duck.fbx", "size": 3}, "oauth", {}))

    def fake_stream(file_id, mode, tmp, cap, md5):
        Path(tmp).write_bytes(b"abc")
        return {"bytes": 3, "sha256": "x"}

    monkeypatch.setattr(drive, "_download_stream", fake_stream)
    out = _call(api, "gdrive_download", file="1A2b3C4d5E6f7G8h9I0j")
    assert out["ok"] and out["kind"] == "model"
    assert Path(out["path"]).read_bytes() == b"abc"
    assert Path(out["path"]).parent == tmp_path
