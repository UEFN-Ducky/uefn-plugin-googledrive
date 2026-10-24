"""Account tools over a fake auth backend: status, connect (incremental), disconnect, aliases."""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gw_tools_account", ROOT / "backend" / "tools_account.py")
ta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ta)

ME = "me@example.com"
WORK = "work@example.com"
READ, FULL, GMAIL = ta.SCOPE_DRIVE_READ, ta.SCOPE_DRIVE, ta.SCOPE_GMAIL


class FakeAuth:
    """Accounts in memory; connect grants what was asked for (or less, when ``deny`` says so)."""

    def __init__(self, accounts=None, ready=True):
        self.accounts = dict(accounts or {})
        self.ready = ready
        self.connects: list[tuple] = []
        self.disconnects: list[tuple] = []
        self.next_email = ME
        self.deny: set[str] = set()

    def sign_in_ready(self):
        return self.ready

    def list_accounts(self):
        return [{"email": e, "name": e.split("@")[0], "scopes": list(s), "access_token": "secret"}
                for e, s in self.accounts.items()]

    def connect(self, scopes, account="", timeout_s=180.0):
        self.connects.append((list(scopes), account, timeout_s))
        email = account or self.next_email
        granted = [s for s in scopes if s not in self.deny]
        self.accounts[email] = list(dict.fromkeys(self.accounts.get(email, []) + granted))
        return {"email": email, "scopes": self.accounts[email]}

    def disconnect(self, account, revoke=True):
        self.disconnects.append((account, revoke))
        self.accounts.pop(account, None)
        return True


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


def setup(accounts=None, **kw):
    api, auth = FakeApi(), FakeAuth(accounts, **kw)
    tools = ta.register(api, auth=auth, extra_status=lambda: {"staging_dir": "C:/stage", "ok": False})
    return api, auth, tools


def test_register_adds_tools_rpcs_and_aliases():
    api, _auth, _tools = setup()
    names = set(ta.TOOL_NAMES + ta.ALIAS_NAMES)
    assert names <= set(api.tools) and names <= set(api.rpcs)
    api2 = FakeApi()
    ta.register(api2, auth=FakeAuth(), aliases=False)
    assert not set(ta.ALIAS_NAMES) & set(api2.tools)


def test_accounts_list_flags_and_no_tokens():
    api, _auth, _tools = setup({ME: [READ], WORK: [READ, GMAIL, FULL]})
    out = api.tools["google_accounts_list"]()
    assert out["ok"] and out["count"] == 2
    rows = {r["email"]: r for r in out["accounts"]}
    assert rows[ME]["drive"] and not rows[ME]["gmail"] and not rows[ME]["manage_drive"]
    assert rows[WORK]["gmail"] and rows[WORK]["manage_drive"]
    assert "secret" not in repr(out)


def test_status_merges_drive_details_and_hints():
    api, _auth, _tools = setup({ME: [READ]})
    out = api.tools["google_status"]()
    assert out["ok"] is True and out["connected"] and out["account"] == ME
    assert out["staging_dir"] == "C:/stage"
    assert any("gmail=true" in h and "manage_drive=true" in h for h in out["hints"])
    empty = setup()[0].tools["google_status"]()
    assert not empty["connected"] and "google_connect" in empty["hints"][0]
    assert setup(ready=False)[0].tools["gdrive_status"]()["sign_in_ready"] is False


def test_connect_asks_drive_read_only_by_default():
    api, auth, _tools = setup()
    out = api.tools["google_connect"]()
    assert out["ok"] and out["account"] == ME and not out["gmail"] and not out["manage_drive"]
    assert auth.connects[0][0] == [READ]


def test_connect_incremental_keeps_granted_scopes():
    api, auth, _tools = setup({ME: [READ]})
    out = api.tools["google_connect"](gmail=True, account=ME)
    assert out["gmail"] and not out["manage_drive"] and "Gmail" in out["message"]
    assert auth.connects[-1][0] == [READ, GMAIL] and auth.connects[-1][1] == ME
    out = api.tools["google_connect"](manage_drive=True, account=ME)
    assert out["gmail"] and out["manage_drive"]
    assert set(auth.connects[-1][0]) == {READ, GMAIL, FULL}


def test_connect_reports_scope_google_did_not_grant():
    api, auth, _tools = setup({ME: [READ]})
    auth.deny = {GMAIL}
    out = api.tools["google_connect"](gmail=True, account=ME)
    assert out["ok"] and not out["gmail"] and "didn't grant Gmail" in out["message"]


def test_connect_refusals_and_timeout_clamp():
    api, auth, _tools = setup({ME: [READ]})
    assert "not a connected" in api.tools["google_connect"](account="who@example.com")["error"]
    api.tools["gdrive_connect"](timeout_s=9999)
    assert auth.connects[-1][2] == ta.CONNECT_TIMEOUT_MAX_S
    off = setup(ready=False)[0].tools["google_connect"]()
    assert off["ok"] is False and "isn't set up" in off["error"]


def test_disconnect_previews_then_disconnects():
    api, auth, _tools = setup({ME: [READ]})
    preview = api.tools["google_disconnect"]()
    assert preview["ok"] and preview["preview"] and not preview["changed"]
    assert auth.disconnects == [] and ME in preview["message"]
    done = api.tools["google_disconnect"](confirmed=True, revoke=False)
    assert done["changed"] and done["account"] == ME and not done["connected"]
    assert auth.disconnects == [(ME, False)]
    rec = api.changeset.records[-1]
    assert rec["command"] == "google_disconnect" and rec["revertable"] == "manual"


def test_disconnect_needs_account_when_several():
    api, auth, _tools = setup({ME: [READ], WORK: [READ]})
    out = api.tools["gdrive_disconnect"](confirmed=True)
    assert out["ok"] is False and "Several" in out["error"] and auth.disconnects == []
    out = api.tools["gdrive_disconnect"](account=WORK, confirmed=True)
    assert out["changed"] and list(auth.accounts) == [ME]


def test_gmail_and_drive_account_helpers():
    _api, _auth, tools = setup({ME: [READ], WORK: [READ, GMAIL, FULL]})
    assert tools.gmail_accounts() == [WORK]
    assert tools.drive_accounts() == [ME, WORK]
    assert tools.drive_accounts(manage=True) == [WORK]


def test_module_auth_over_legacy_single_sign_in():
    mod = types.ModuleType("fake_auth")
    state = {"tok": {"email": ME, "scope": READ, "access_token": "x"}}
    mod._oauth_client = lambda: ("id.apps.googleusercontent.com", "s")
    mod._load_token = lambda: state["tok"]
    mod._run_oauth_flow = lambda timeout: {"account": ME}
    mod.revoke_and_clear = lambda revoke=True: state.update(tok=None) or True
    api = FakeApi()
    ta.register(api, auth=mod)
    assert api.tools["google_status"]()["accounts"][0]["email"] == ME
    assert "only sign in with Drive read" in api.tools["google_connect"](gmail=True)["error"]
    assert api.tools["google_connect"]()["ok"]
    assert api.tools["google_disconnect"](confirmed=True)["changed"]
    assert api.tools["google_accounts_list"]()["count"] == 0
