"""Check GitHub for a newer release, only when the user asks (Settings › General).

Asks the GitHub API for the latest release; nothing about the user or their captions is sent.
The portable build can then install it in one click (updater.py); other installs get a link.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass

from . import __version__
from .paths import config_dir

RELEASES_PAGE = "https://github.com/brokenrockstudios/VoiceRhino/releases"
LATEST_API = "https://api.github.com/repos/brokenrockstudios/VoiceRhino/releases/latest"


ZIP_SUFFIX = "-win64.zip"  # the portable build's asset: VoiceRhino-<version>-win64.zip (+ .sha256)
_CHECK_FILE = "update_check.json"  # {"last_checked": unix time}, for "Last checked" in Settings


@dataclass(frozen=True)
class Release:
    version: str  # "0.1.2", from the tag
    url: str  # the release's page, where the zip is
    zip_url: str = ""  # the portable build's download, "" when the release has none
    zip_size: int = 0
    sha256: str = ""  # from GitHub's asset digest, when it lists one
    sha256_url: str = ""  # the .sha256 file published next to the zip


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
    assets = {str(a.get("name", "")): a for a in data.get("assets") or [] if isinstance(a, dict)}
    zip_name = next((n for n in assets if n.endswith(ZIP_SUFFIX)), "")
    asset = assets.get(zip_name, {})
    digest = str(asset.get("digest") or "")
    record_check()
    return Release(".".join(map(str, version)), str(data.get("html_url") or RELEASES_PAGE),
                   zip_url=str(asset.get("browser_download_url") or ""), zip_size=int(asset.get("size") or 0),
                   sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else "",
                   sha256_url=str(assets.get(zip_name + ".sha256", {}).get("browser_download_url") or ""))


def record_check(when: float | None = None) -> None:
    try:
        (config_dir() / _CHECK_FILE).write_text(json.dumps({"last_checked": when or time.time()}),
                                                encoding="utf-8")
    except OSError:
        pass


def last_checked() -> float | None:
    """Unix time of the last successful check, None if never."""
    try:
        value = json.loads((config_dir() / _CHECK_FILE).read_text(encoding="utf-8")).get("last_checked")
        return float(value) if value else None
    except (OSError, ValueError, AttributeError):
        return None
