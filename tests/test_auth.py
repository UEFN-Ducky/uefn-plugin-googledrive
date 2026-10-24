"""auth.py: PKCE, state, incremental scope, Google Connect refresh, migration, own client, allowlist."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "backend" / "auth.py"
_spec = importlib.util.spec_from_file_location("gd_auth_test", _PATH)
auth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auth)

ME = "me@example.com"
READ, GMAIL, FULL = auth.SCOPE_DRIVE_READ, auth.SCOPE_GMAIL, auth.SCOPE_DRIVE
_CHECK_URL = auth._check_url


class Resp:
    def __init__(self, body) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def read(self, *_a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class FakeApi:
    def __init__(self, proof: dict | None = None) -> None:
        self.proof = proof if proof is not None else {"ok": True, "proof": "udp1_" + "a" * 64}
        self.calls: list[tuple] = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self.proof


class Google:
    """Fake Google + Google Connect: records every request, answers like the real ones."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.auth_urls: list[dict] = []
        self.redirect: dict = {}
        self.refresh_answer: dict = {"access_token": "at-fresh", "expires_in": 3600}
        self.email = ME

    def open(self, url, *, headers=None, data=None, method=None, timeout=60.0):
        _CHECK_URL(url)  # every request must pass the real host allowlist
        body = data.decode("utf-8") if data else ""
        try:
            parsed = json.loads(body) if body.startswith("{") else dict(urllib.parse.parse_qsl(body))
        except ValueError:
            parsed = {}
        self.requests.append({"url": url, "body": parsed})
        granted = " ".join(self.auth_urls[-1]["scope"].split()) if self.auth_urls else READ
        if url.endswith("/collect/config"):
            return Resp({"ok": True, "payload": {"clientId": "broker-client.apps.googleusercontent.com"}})
        if url.endswith("/collect/token"):
            return Resp({"ok": True, "payload": {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600, "scope": granted}})
        if url.endswith("/collect/refresh"):
            return Resp({"ok": True, "payload": self.refresh_answer})
        if url == auth.TOKEN_URL:
            if parsed.get("grant_type") == "refresh_token":
                return Resp(self.refresh_answer)
            return Resp({"access_token": "at-own", "refresh_token": "rt-own", "expires_in": 3600, "scope": granted})
        if url.startswith(auth.ABOUT_URL):
            return Resp({"user": {"emailAddress": self.email}})
        if url == auth.REVOKE_URL:
            return Resp({})
        raise AssertionError(f"unexpected request {url}")

    def browser(self, url: str) -> bool:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.auth_urls.append(q)
        params = {"code": "c1", "state": q["state"]}
        params.update(self.redirect)
        target = q["redirect_uri"] + "/?" + urllib.parse.urlencode(params)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        threading.Thread(target=lambda: opener.open(target, timeout=10).read(), daemon=True).start()
        return True

    def to(self, host: str) -> list[dict]:
        return [r for r in self.requests if urllib.parse.urlparse(r["url"]).hostname == host]


@pytest.fixture()
def env(monkeypatch):
    secrets: dict[str, str] = {}
    google = Google()
    monkeypatch.setattr(auth, "_get_secret", lambda k: secrets.get(k, ""))
    monkeypatch.setattr(auth, "_set_secret", lambda k, v: secrets.__setitem__(k, v))
    monkeypatch.setattr(auth, "_open", google.open)
    monkeypatch.setattr(auth, "_open_browser", google.browser)
    monkeypatch.setattr(auth, "_broker_client_id_cache", "")
    api = FakeApi()
    auth.bind(api)
    yield {"secrets": secrets, "google": google, "api": api}
    auth.bind(None)


def _store(env) -> list[dict]:
    return json.loads(env["secrets"][auth.KEY_ACCOUNTS])["accounts"]


def test_pkce_s256():
    verifier, challenge = auth.pkce_pair()
    assert 43 <= len(verifier) <= 128
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expect and "=" not in challenge
    assert auth.pkce_pair()[0] != verifier


def test_auth_url_asks_incrementally():
    url = auth.build_auth_url(client_id="cid", redirect_uri="http://127.0.0.1:5000", scopes=[READ, GMAIL],
                              state="st", challenge="ch", login_hint=ME)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["include_granted_scopes"] == "true" and q["code_challenge_method"] == "S256"
    assert q["scope"] == f"{READ} {GMAIL}" and q["login_hint"] == ME and q["access_type"] == "offline"


def test_connect_via_google_connect_then_turn_on_gmail(env):
    g = env["google"]
    first = auth.connect(timeout_s=10)
    assert first["email"] == ME and first["scopes"] == [READ]
    assert g.auth_urls[0]["scope"] == READ  # Drive read only at first
    assert g.auth_urls[0]["client_id"] == "broker-client.apps.googleusercontent.com"
    assert g.auth_urls[0]["redirect_uri"].startswith("http://127.0.0.1:")
    swap = [r for r in g.requests if r["url"].endswith("/collect/token")][0]["body"]
    assert swap["proof"].startswith("udp1_") and swap["code"] == "c1"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(swap["code_verifier"].encode()).digest()).decode().rstrip("=")
    assert challenge == g.auth_urls[0]["code_challenge"]
    assert not g.to("oauth2.googleapis.com")  # the secret side is Google Connect's
    assert env["api"].calls[0] == ("account_session_token", {"audience": "google-connect"})
    assert auth.gmail_accounts() == []

    more = auth.connect([GMAIL], account=ME, timeout_s=10)
    q = g.auth_urls[1]
    assert q["scope"].split() == [READ, GMAIL] and q["include_granted_scopes"] == "true" and q["login_hint"] == ME
    assert more["added"] == [GMAIL] and more["denied"] == []
    assert auth.gmail_accounts() == [ME]
    assert [a["email"] for a in auth.list_accounts()] == [ME]  # same account, not a second one
    assert "access_token" not in auth.list_accounts()[0] and "refresh_token" not in auth.list_accounts()[0]


def test_many_accounts(env):
    auth.connect(timeout_s=10)
    env["google"].email = "work@example.com"
    auth.connect([FULL], timeout_s=10)
    rows = auth.list_accounts()
    assert [r["email"] for r in rows] == [ME, "work@example.com"]
    assert FULL in rows[1]["scopes"] and FULL not in rows[0]["scopes"]


def test_state_mismatch_is_rejected(env):
    env["google"].redirect = {"state": "forged"}
    with pytest.raises(ValueError, match="state mismatch"):
        auth.connect(timeout_s=10)
    assert not any(r["url"].endswith("/collect/token") for r in env["google"].requests)
    assert auth.list_accounts() == []


def test_cancelled_sign_in(env):
    env["google"].redirect = {"error": "access_denied", "code": ""}
    with pytest.raises(ValueError, match="cancelled"):
        auth.connect(timeout_s=10)


def test_refresh_goes_through_google_connect(env):
    auth.connect(timeout_s=10)
    accts = _store(env)
    accts[0]["expires_at"] = 0
    env["secrets"][auth.KEY_ACCOUNTS] = json.dumps({"version": 1, "accounts": accts})
    assert auth.access_token(ME) == "at-fresh"
    ref = [r for r in env["google"].requests if r["url"].endswith("/collect/refresh")]
    assert len(ref) == 1 and ref[0]["body"]["refresh_token"] == "rt1" and ref[0]["body"]["proof"]
    assert "client_secret" not in ref[0]["body"]
    assert auth.access_token_getter(ME)() == "at-fresh"  # still fresh: no second refresh
    assert len([r for r in env["google"].requests if r["url"].endswith("/collect/refresh")]) == 1


def test_scope_check_and_invalid_grant(env, monkeypatch):
    auth.connect(timeout_s=10)
    with pytest.raises(ValueError, match="Turn on Gmail"):
        auth.access_token(ME, scope=GMAIL)
    accts = _store(env)
    accts[0]["expires_at"] = 0
    env["secrets"][auth.KEY_ACCOUNTS] = json.dumps({"version": 1, "accounts": accts})

    def revoked(event, body, proof=True):
        raise ValueError(auth._INVALID_GRANT_MSG)

    monkeypatch.setattr(auth, "_broker_post", revoked)
    with pytest.raises(ValueError, match="invalid_grant"):
        auth.access_token(ME)
    assert auth.list_accounts()[0]["needs_sign_in"] is True


def test_no_ducky_sign_in_means_no_google_sign_in(env):
    auth.bind(FakeApi(proof={"ok": False, "error": "not signed in"}))
    with pytest.raises(ValueError, match="Sign in to UEFN Ducky"):
        auth.connect(timeout_s=10)
    assert auth.list_accounts() == []


def test_migrates_the_old_single_sign_in(env):
    env["secrets"][auth.KEY_CLIENT_ID] = "own.apps.googleusercontent.com"
    env["secrets"][auth.KEY_CLIENT_SECRET] = "from-settings"
    old = {"access_token": "old-at", "refresh_token": "old-rt", "expires_at": 0, "scope": READ, "email": ME}
    env["secrets"][auth.KEY_TOKEN] = json.dumps(old)
    rows = auth.list_accounts()
    assert [(r["email"], r["scopes"], r["via"]) for r in rows] == [(ME, [READ], "own")]
    assert env["secrets"][auth.KEY_TOKEN]  # kept until the moved account is confirmed
    assert auth._load_token()["email"] == ME
    assert auth.access_token() == "at-fresh"  # refreshed with the own client
    assert env["secrets"][auth.KEY_TOKEN] == ""  # confirmed → old key dropped
    assert not env["google"].to("uefnducky.org")


def test_disconnect_drops_migrated_key_and_revokes_at_google(env):
    env["secrets"][auth.KEY_TOKEN] = json.dumps({"access_token": "a", "refresh_token": "r", "email": ME})
    assert auth.disconnect(ME) is True
    revoke = [r for r in env["google"].requests if r["url"] == auth.REVOKE_URL]
    assert revoke and revoke[0]["body"]["token"] == "r"
    assert auth.list_accounts() == [] and env["secrets"][auth.KEY_TOKEN] == ""


def test_own_client_skips_google_connect(env):
    env["secrets"][auth.KEY_CLIENT_ID] = "own.apps.googleusercontent.com"
    env["secrets"][auth.KEY_CLIENT_SECRET] = "from-settings"
    res = auth.connect([GMAIL], timeout_s=10)
    assert res["scopes"] == [READ, GMAIL] and auth.list_accounts()[0]["via"] == "own"
    assert env["google"].auth_urls[0]["client_id"] == "own.apps.googleusercontent.com"
    swap = [r for r in env["google"].requests if r["url"] == auth.TOKEN_URL][0]["body"]
    assert swap["grant_type"] == "authorization_code" and swap["code_verifier"]
    assert not env["google"].to("uefnducky.org") and env["api"].calls == []
    assert auth._using_bundled_client() is False


def test_host_allowlist():
    for host in ("gmail.googleapis.com", "uefnducky.org", "oauth2.googleapis.com", "doc-0s.googleusercontent.com"):
        assert auth._host_ok(host)
    for host in ("evil.com", "uefnducky.org.evil.com", "googleusercontent.com.evil.com", "www.uefnducky.org.example"):
        assert not auth._host_ok(host)
    with pytest.raises(ValueError):
        auth._check_url("http://uefnducky.org/api")  # https only
    assert auth.BROKER_BASE.startswith("https://uefnducky.org/api/v1/plugins/google-connect/")


def test_no_secret_in_code():
    text = _PATH.read_text(encoding="utf-8")
    assert "GOCSPX" not in text and "client_secret\": \"" not in text
