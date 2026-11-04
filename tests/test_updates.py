"""Manual update check against GitHub releases (no network: urlopen is faked)."""

from __future__ import annotations

import io
import json

import pytest

from voicerhino import updates
from voicerhino.updates import RELEASES_PAGE, Release, is_newer, latest_release, parse_version


def test_parse_and_compare_versions():
    assert parse_version("v0.1.2") == parse_version("0.1.2") == parse_version("VoiceRhino 0.1.2") == (0, 1, 2)
    assert parse_version("no number") == ()
    assert is_newer("0.1.10", "0.1.9") and is_newer("v0.2", "0.1.9")
    assert not is_newer("0.1.1", "0.1.1") and not is_newer("0.1.0", "0.1.1")


def fake_reply(monkeypatch, data: dict) -> list:
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return io.BytesIO(json.dumps(data).encode("utf-8"))

    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)
    return requests


def test_latest_release_reads_the_tag(monkeypatch):
    requests = fake_reply(monkeypatch, {"tag_name": "v0.2.0", "html_url": "https://github.com/x/releases/tag/v0.2.0"})
    assert latest_release() == Release("0.2.0", "https://github.com/x/releases/tag/v0.2.0")
    assert requests[0].full_url == updates.LATEST_API
    assert requests[0].get_header("User-agent").startswith("VoiceRhino/")
    fake_reply(monkeypatch, {"name": "VoiceRhino 0.3"})  # no tag or page: the name and the Releases page
    assert latest_release() == Release("0.3", RELEASES_PAGE)
    fake_reply(monkeypatch, {"tag_name": "latest"})
    with pytest.raises(ValueError):
        latest_release()


def test_latest_release_finds_the_portable_zip(monkeypatch):
    fake_reply(monkeypatch, {"tag_name": "v0.2.0", "html_url": "https://github.com/x/releases/tag/v0.2.0", "assets": [
        {"name": "VoiceRhino-0.2.0-win64.zip", "browser_download_url": "https://dl/zip", "size": 70_000_000,
         "digest": "sha256:" + "ab" * 32},
        {"name": "VoiceRhino-0.2.0-win64.zip.sha256", "browser_download_url": "https://dl/sha"},
        {"name": "Source code.zip", "browser_download_url": "https://dl/src"},
    ]})
    release = latest_release()
    assert (release.zip_url, release.zip_size, release.sha256, release.sha256_url) == (
        "https://dl/zip", 70_000_000, "ab" * 32, "https://dl/sha")
    fake_reply(monkeypatch, {"tag_name": "v0.1.2"})  # older releases: source zip only
    assert latest_release().zip_url == ""


def test_last_checked_is_remembered(monkeypatch):
    assert updates.last_checked() is None
    fake_reply(monkeypatch, {"tag_name": "v0.2.0"})
    latest_release()
    first = updates.last_checked()
    assert first is not None
    updates.record_check(1_700_000_000)
    assert updates.last_checked() == 1_700_000_000
