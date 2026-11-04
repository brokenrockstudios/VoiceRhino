"""One-click update of the portable build: download + checksum + unpack, then the file swap with
its self-test and rollback. Local files stand in for GitHub; no exe is started."""

from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from pathlib import Path

import pytest

from voicerhino import updater
from voicerhino.updates import Release


def release_zip(tmp_path: Path, version: str = "9.9.9") -> tuple[Path, str]:
    path = tmp_path / f"VoiceRhino-{version}-win64.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("VoiceRhino/VoiceRhino.exe", f"new exe {version}")
        zf.writestr("VoiceRhino/_internal/lib.dll", "new lib")
        zf.writestr("VoiceRhino/README.md", "new readme")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def old_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    (app / "VoiceRhino.exe").write_text("old exe")
    (app / "_internal" / "lib.dll").write_text("old lib")
    (app / "_internal" / "gone.dll").write_text("only in the old version")
    (app / "START-HERE.txt").write_text("old start")
    (app / "my notes.txt").write_text("the user's own file")
    return app


def test_download_checks_and_unpacks(tmp_path):
    app = old_app(tmp_path)
    zip_path, digest = release_zip(tmp_path)
    release = Release("9.9.9", "https://example.com", zip_url=zip_path.as_uri(), sha256=digest)
    new = updater.download(release, app=app)
    assert new == app / updater.STAGING / "new" / "VoiceRhino"
    assert (new / "VoiceRhino.exe").read_text() == "new exe 9.9.9"
    assert not list((app / updater.STAGING).glob("*.zip"))  # the zip is deleted once unpacked


def test_download_uses_the_sha256_file(tmp_path):
    app = old_app(tmp_path)
    zip_path, digest = release_zip(tmp_path)
    sums = tmp_path / (zip_path.name + ".sha256")
    sums.write_text(f"{digest}  {zip_path.name}\n")
    release = Release("9.9.9", "", zip_url=zip_path.as_uri(), sha256_url=sums.as_uri())
    assert (updater.download(release, app=app) / "README.md").exists()


def test_download_refuses_a_bad_or_missing_checksum(tmp_path):
    app = old_app(tmp_path)
    zip_path, _ = release_zip(tmp_path)
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.download(Release("9.9.9", "", zip_url=zip_path.as_uri(), sha256="0" * 64), app=app)
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.download(Release("9.9.9", "", zip_url=zip_path.as_uri()), app=app)
    with pytest.raises(updater.UpdateError, match="no Windows zip"):
        updater.download(Release("9.9.9", ""), app=app)


def staged(tmp_path: Path, app: Path) -> Path:
    zip_path, digest = release_zip(tmp_path)
    return updater.download(Release("9.9.9", "", zip_url=zip_path.as_uri(), sha256=digest), app=app)


def test_finish_swaps_files_and_keeps_the_users(tmp_path):
    app = old_app(tmp_path)
    new = staged(tmp_path, app)
    ok = [sys.executable, "-c", "raise SystemExit(0)"]
    assert updater.finish(app, wait_pid=0, source=new, self_test=ok, start=False) == 0
    assert (app / "VoiceRhino.exe").read_text() == "new exe 9.9.9"
    assert (app / "_internal" / "lib.dll").read_text() == "new lib"
    assert not (app / "_internal" / "gone.dll").exists()  # the old _internal is replaced whole
    assert not (app / "START-HERE.txt").exists()  # shipped before, not any more
    assert (app / "my notes.txt").read_text() == "the user's own file"
    assert not (app / updater.PREVIOUS).exists()


def test_finish_rolls_back_when_the_new_version_fails(tmp_path, monkeypatch):
    app = old_app(tmp_path)
    new = staged(tmp_path, app)
    messages = []
    monkeypatch.setattr(updater, "_message", messages.append)
    broken = [sys.executable, "-c", "raise SystemExit(3)"]
    assert updater.finish(app, wait_pid=0, source=new, self_test=broken, start=False) == 1
    assert (app / "VoiceRhino.exe").read_text() == "old exe"
    assert (app / "_internal" / "gone.dll").exists()
    assert (app / "START-HERE.txt").read_text() == "old start"
    assert not (app / "README.md").exists()  # the new version's extra file is removed again
    assert (app / "my notes.txt").exists()
    assert not (app / updater.PREVIOUS).exists()
    assert messages and "went back" in messages[0]


def test_finish_waits_for_the_old_app(tmp_path, monkeypatch):
    app = old_app(tmp_path)
    new = staged(tmp_path, app)
    waited = []
    monkeypatch.setattr(updater, "_wait_for_exit", lambda pid, timeout: waited.append(pid))
    ok = [sys.executable, "-c", "raise SystemExit(0)"]
    updater.finish(app, wait_pid=os.getpid(), source=new, self_test=ok, start=False)
    assert waited == [os.getpid()]


def test_unwritable_folder_says_what_to_do(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "write_bytes", deny)
    with pytest.raises(updater.UpdateError, match="Move the VoiceRhino folder"):
        updater.check_writable(tmp_path)


def test_only_the_portable_build_updates_itself():
    assert not updater.supported()
