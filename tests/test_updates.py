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
