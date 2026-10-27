"""plugin.json checks for the Google Workspace (id googledrive) manifest."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
CONTRIB = MANIFEST["contributes"]
TOOLS = CONTRIB["agent.tools"]


def test_identity_kept_for_existing_users():
    assert MANIFEST["id"] == "googledrive"
    assert MANIFEST["label"] == "Google Workspace"
    assert MANIFEST["version"] == "1.1.2"
    assert MANIFEST["min_app_version"] == "1.2.357"
    assert MANIFEST["data_sharing"] is False
    assert MANIFEST["default_enabled"] is False
    assert MANIFEST["backend"] == {"entry": "backend", "register": "register"}


def test_intent_pattern_covers_gmail_and_drive():
    pattern = re.compile(TOOLS["intent_pattern"], re.IGNORECASE)
    for text in (
        "check my gmail",
        "any new email from Sam?",
        "clean up my inbox",
        "search my emails for the invoice",
        "pull the duck from google drive",
        "here's a drive link",
        "connect my google workspace",
    ):
        assert pattern.search(text), text
    for text in ("build verse", "spawn a car", "drive the car"):
        assert not pattern.search(text), text


def test_plan_tools_are_read_only_and_listed():
    listed = set(TOOLS["tools"])
    plan = set(TOOLS["plan_tools"])
    destructive = set(TOOLS["destructive_tools"])
    assert plan <= listed
    assert destructive <= listed
    assert not plan & destructive
    write_words = ("send", "reply", "forward", "trash", "delete", "share", "modify", "move",
                   "rename", "upload", "create", "connect", "disconnect", "archive", "star", "mark")
    for name in plan:
        assert not any(w in name for w in write_words), name


def test_destructive_tools_ask_first():
    destructive = set(TOOLS["destructive_tools"])
    for name in ("gmail_send", "gmail_draft_send", "gmail_reply", "gmail_forward", "gmail_trash",
                 "gmail_label_delete", "gdrive_trash", "gdrive_share", "google_disconnect",
                 "gdrive_disconnect"):
        assert name in destructive, name


def test_old_drive_tools_still_listed():
    for name in ("gdrive_status", "gdrive_connect", "gdrive_disconnect", "gdrive_list", "gdrive_search",
                 "gdrive_file_info", "gdrive_download", "gdrive_import_to_uefn"):
        assert name in TOOLS["tools"], name


def test_every_gmail_tool_is_model_gated():
    sensitive = TOOLS["sensitive"]
    assert isinstance(sensitive, list)
    gmail = {name for name in TOOLS["tools"] if name.startswith("gmail_")}
    assert set(sensitive) == gmail
    assert len(sensitive) == len(set(sensitive))
    # Drive and account tools stay usable by every model.
    assert not any(n.startswith(("gdrive_", "google_")) for n in sensitive)


def test_settings_explain_allowed_models():
    sections = {s["id"]: s for s in CONTRIB["settings.sections"]}
    assert "Allowed models" in sections["access"]["description"]
    gmail = next(p for p in sections["access"]["properties"] if p["id"] == "gmail_enabled")
    assert "Allowed models" in gmail["description"]


def test_tab_panel_and_header_button():
    panels = {p["id"]: p for p in CONTRIB["ui.panels"]}
    assert panels["main"]["entry"] == "ui/index.html"
    buttons = CONTRIB["header.buttons"]
    assert any(b["action"] == "panel:main" for b in buttons)


def _props() -> dict:
    return {p["id"]: p for s in CONTRIB["settings.sections"] for p in s["properties"]}


def test_settings_tab_google():
    tabs = CONTRIB["settings.tabs"]
    assert [t["id"] for t in tabs] == ["Google"]
    sections = CONTRIB["settings.sections"]
    assert all(s["tab"] == "Google" for s in sections)
    ids = [s["id"] for s in sections]
    assert ids == ["accounts", "access", "gmail", "folder", "downloads", "advanced"]
    orders = [s["order"] for s in sections]
    assert orders == sorted(orders)


def test_settings_properties():
    props = _props()
    # Off until the person turns them on: each adds its Google access only then.
    assert props["gmail_enabled"]["type"] == "boolean" and props["gmail_enabled"]["default"] is False
    assert props["manage_drive"]["type"] == "boolean" and props["manage_drive"]["default"] is False
    assert props["gmail_load_remote_images"]["default"] is False
    poll = {o["value"] for o in props["gmail_poll_seconds"]["options"]}
    assert props["gmail_poll_seconds"]["default"] in poll and "0" in poll
    days = {o["value"] for o in props["gmail_sync_days"]["options"]}
    assert props["gmail_sync_days"]["default"] in days
    # Existing users keep their values: old ids unchanged.
    for old in ("target_folder", "max_download_mb", "gdrive_oauth_client_id",
                "gdrive_oauth_client_secret", "gdrive_api_key"):
        assert old in props, old


def test_secret_fields_are_declared():
    props = _props()
    secrets = {pid for pid, p in props.items() if p["type"] == "secret"}
    assert secrets == {"gdrive_oauth_client_id", "gdrive_oauth_client_secret", "gdrive_api_key"}
    assert secrets <= set(MANIFEST["secret_keys"])


def test_walkthrough_targets_google_tab():
    walk = CONTRIB["walkthrough"]
    assert walk["settings_tab"] == "Google"
    assert all(step["target"] == "settings.tab.google" for step in walk["steps"])
    assert "google-drive" not in json.dumps(walk)


SKILL = (ROOT / "skills" / "googledrive" / "SKILL.md").read_text(encoding="utf-8")


def test_skill_frontmatter_is_findable():
    front = SKILL.split("---")[1]
    desc = next(line for line in front.splitlines() if line.startswith("description:"))
    assert len(desc) > 60 and ">" not in desc.split(":", 1)[1][:3]
    assert "gmail" in desc.lower() and "drive" in desc.lower()
    assert "keywords:" in front and "inbox" in front


def test_skill_hard_rules_come_first():
    body = SKILL.split("---", 2)[2]
    head = body[: body.index("\n## ")]
    assert "untrusted" in head
    assert "Never send without asking" in head
    assert "No delete" in head and "confirmed=true" in head


def test_skill_names_every_tool_and_node():
    for name in TOOLS["tools"]:
        assert f"`{name}" in SKILL, name
    for node in CONTRIB["automations"]["nodes"]:
        assert node["id"] in SKILL, node["id"]
    assert "gmail.received" in SKILL
    sections = [line for line in SKILL.splitlines() if line.startswith("## ")]
    assert len(sections) >= 7


def test_no_secret_values_in_manifest():
    text = (ROOT / "plugin.json").read_text(encoding="utf-8")
    assert not re.search(r"GOCSPX-[A-Za-z0-9_-]{10,}", text)
    assert not re.search(r"AIza[0-9A-Za-z_-]{30,}", text)
    assert not re.search(r"\d{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com", text)
