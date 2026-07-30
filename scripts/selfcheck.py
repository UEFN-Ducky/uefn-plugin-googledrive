#!/usr/bin/env python3
"""Dev self-check for the googledrive plugin backend (excluded from the shipped zip)."""
import importlib.util, sys
from pathlib import Path
_spec = importlib.util.spec_from_file_location("gd_backend", Path(__file__).resolve().parents[1]/"backend"/"__init__.py")
gd = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gd)
for _n in dir(gd):
    globals().setdefault(_n, getattr(gd, _n))

def _self_check() -> None:  # noqa: PLR0915 — linear assertions, clearer flat
    import tempfile

    # extract_drive_id
    fid = "1A2b3C4d5E6f7G8h9I0j_-XYZ"
    assert extract_drive_id(fid) == fid
    assert extract_drive_id(f"https://drive.google.com/file/d/{fid}/view?usp=sharing") == fid
    assert extract_drive_id(f"https://drive.google.com/drive/folders/{fid}?usp=drive_link") == fid
    assert extract_drive_id(f"https://drive.google.com/open?id={fid}") == fid
    assert extract_drive_id(f"https://drive.google.com/uc?export=download&id={fid}") == fid
    for bad in (
        "",
        "https://evil.example.com/file/d/1A2b3C4d5E6f7G8h9I0j/view",
        "https://docs.google.com/document/d/1A2b3C4d5E6f7G8h9I0j/edit",
        "short",
        "https://drive.google.com/drive/my-drive",
    ):
        try:
            extract_drive_id(bad)
            raise AssertionError(f"extract_drive_id should reject {bad!r}")
        except ValueError:
            pass

    # sanitize_filename
    assert sanitize_filename("model.FBX") == "model.fbx"
    assert sanitize_filename("..\\..\\evil.fbx") == "evil.fbx"
    assert sanitize_filename("dir/sub/tex.png") == "tex.png"
    assert sanitize_filename("con.png") == "_con.png"
    assert sanitize_filename('we<i>rd:"name".glb') == "we_i_rd__name_.glb"
    assert len(sanitize_filename("x" * 500 + ".fbx")) <= 124
    for bad_name in ("virus.exe", "run.bat", "script.py", "noext", "archive.rar"):
        try:
            sanitize_filename(bad_name)
            raise AssertionError(f"sanitize_filename should reject {bad_name!r}")
        except ValueError:
            pass
    assert sanitize_filename("tool.exe", require_allowed_ext=False) == "tool.exe"

    # classify
    assert classify_name("a.fbx") == "model"
    assert classify_name("a.png") == "texture"
    assert classify_name("a.wav") == "audio"
    assert classify_name("a.zip") == "archive"
    assert classify_name("a.bin") == "support"
    assert classify_name("whatever", FOLDER_MIME) == "folder"
    assert classify_name("a.xyz") == "other"

    # content-disposition / range parsing
    assert parse_content_disposition('attachment; filename="duck model.fbx"') == "duck model.fbx"
    assert parse_content_disposition("attachment; filename*=UTF-8''duck%20model.fbx") == "duck model.fbx"
    assert parse_content_range_total("bytes 0-0/123456") == 123456
    assert parse_content_range_total("junk") is None

    # confirm-form parsing
    page = (
        '<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
        '<input type="hidden" name="id" value="ABCDEFGHIJKLMNOP">'
        '<input type="hidden" name="confirm" value="t">'
        '<input type="hidden" name="uuid" value="u-1"></form>'
    )
    form = parse_public_confirm_form(page)
    assert form and form["id"] == "ABCDEFGHIJKLMNOP" and form["confirm"] == "t" and form["uuid"] == "u-1"
    assert parse_public_confirm_form("<html>sign in please</html>") is None
    evil = page.replace("drive.usercontent.google.com", "evil.example.com")
    assert parse_public_confirm_form(evil) is None

    # token expiry
    now = time.time()
    assert token_expired({}, now)
    assert token_expired({"access_token": "x", "expires_at": now + 10}, now)
    assert not token_expired({"access_token": "x", "expires_at": now + 3600}, now)

    # host guard
    assert _host_ok("www.googleapis.com") and _host_ok("doc-00-cdn.googleusercontent.com")
    assert not _host_ok("evil.com") and not _host_ok("googleusercontent.com.evil.com")
    try:
        _check_url("http://www.googleapis.com/x")  # http (not https) must be blocked
        raise AssertionError("plain http should be blocked")
    except ValueError:
        pass

    # safe_extract_zip
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        zpath = tmp / "pack.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("model/duck.fbx", b"F" * 100)
            zf.writestr("model/tex.png", b"P" * 50)
            zf.writestr("../../escape.fbx", b"E" * 10)
            zf.writestr("readme.txt", b"hello")
            zf.writestr("tool.exe", b"MZ")
            zf.writestr("nested.zip", b"zipzip")
        out = tmp / "out"
        info = safe_extract_zip(zpath, out, max_total_bytes=10_000)
        names = sorted(p.name for p in info["files"])
        assert names == ["duck.fbx", "escape.fbx", "tex.png"], names
        for extracted in info["files"]:
            assert extracted.resolve().is_relative_to(out.resolve())
        reasons = {n: why for n, why in info["skipped"]}
        assert "tool.exe" in reasons and "readme.txt" in reasons and "nested.zip" in reasons
        # size cap
        big = tmp / "big.zip"
        with zipfile.ZipFile(big, "w") as zf:
            zf.writestr("huge.fbx", b"B" * 5000)
        try:
            safe_extract_zip(big, tmp / "out2", max_total_bytes=1000)
            raise AssertionError("size cap should trip")
        except ValueError:
            pass
        # entry-count cap
        many = tmp / "many.zip"
        with zipfile.ZipFile(many, "w") as zf:
            for i in range(12):
                zf.writestr(f"f{i}.png", b"x")
        try:
            safe_extract_zip(many, tmp / "out3", max_total_bytes=10_000, max_entries=10)
            raise AssertionError("entry cap should trip")
        except ValueError:
            pass

    # misc
    assert _human_size(1536) == "1.5 KB"
    assert _q_escape("it's") == "it\\'s"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.fbx"
        p.write_bytes(b"x")
        assert _unique_path(p).name == "a-2.fbx"

    print("googledrive self-check OK")


if __name__ == "__main__":
    _self_check()
