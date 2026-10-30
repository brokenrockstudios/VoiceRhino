"""Check GitHub for a newer release, only when the user asks (Settings › General).

Asks the GitHub API for the latest release's tag; nothing about the user or their captions is
sent. Updating itself stays manual: the user downloads the new zip from the release page.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass

from . import __version__

RELEASES_PAGE = "https://github.com/brokenrockstudios/VoiceRhino/releases"
LATEST_API = "https://api.github.com/repos/brokenrockstudios/VoiceRhino/releases/latest"


@dataclass(frozen=True)
class Release:
    version: str  # "0.1.2", from the tag
    url: str  # the release's page, where the zip is


def parse_version(text: str) -> tuple[int, ...]:
    """"v0.1.2", "0.1.2" or "VoiceRhino 0.1.2" -> (0, 1, 2); () when there's no number."""
    match = re.search(r"\d+(?:\.\d+)*", text)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def is_newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def latest_release(timeout: float = 10.0) -> Release:
    """The newest published release (drafts and pre-releases are skipped by GitHub). Raises on
    network errors, or ValueError when the reply has no version."""
    request = urllib.request.Request(LATEST_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"VoiceRhino/{__version__}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    version = parse_version(str(data.get("tag_name") or data.get("name") or ""))
    if not version:
        raise ValueError("the latest release has no version number")
    return Release(".".join(map(str, version)), str(data.get("html_url") or RELEASES_PAGE))
