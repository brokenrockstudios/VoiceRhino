"""Build the portable Windows zip: dist/VoiceRhino-<version>-win64.zip (+ .sha256).

    .venv/Scripts/python packaging/build.py            # windowed VoiceRhino.exe
    .venv/Scripts/python packaging/build.py --console  # debug build that prints to a terminal

Needs the build group in a CPU-only venv (`uv sync --group build`, without --extra gpu, so the CUDA
libraries never end up in the zip). Models are never bundled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "pyinstaller"
DIST = ROOT / "dist"
APP = DIST / "VoiceRhino"
EXTRAS = ["START-HERE.txt", "README.md", "LICENSE", "CHANGELOG.md", "docs"]


def version() -> str:
    text = (ROOT / "src" / "voicerhino" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def make_icon(dest: Path) -> None:
    """VoiceRhino_512.png -> a multi-size .ico (PNG entries) for the exe in Explorer."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 (needed for QImage scaling)
    source = QImage(str(ROOT / "VoiceRhino_512.png"))
    if source.isNull():
        raise SystemExit("VoiceRhino_512.png not found")
    images = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        scaled = source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        scaled.save(buffer, "PNG")
        images.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    dest.write_bytes(header + entries + blobs)


def make_version_file(dest: Path, ver: str) -> None:
    """Version resource: Explorer's Details tab, and a named publisher helps with antivirus."""
    nums = [int(n) for n in re.findall(r"\d+", ver)][:4]
    nums += [0] * (4 - len(nums))
    t = tuple(nums)
    dest.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Broken Rock Studios LLC'),
      StringStruct('FileDescription', 'VoiceRhino live captions'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'VoiceRhino'),
      StringStruct('LegalCopyright', 'Copyright (c) Broken Rock Studios LLC. MIT License.'),
      StringStruct('OriginalFilename', 'VoiceRhino.exe'),
      StringStruct('ProductName', 'VoiceRhino'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


# faster-whisper imports onnxruntime only for its own VAD, which VoiceRhino never uses.
GPU_SKIP = {"onnxruntime", "flatbuffers", "protobuf"}


def make_gpu_manifest(dest: Path) -> dict:
    """The wheels the `gpu` extra adds to the bundled packages, pinned by uv.lock (see asr/gpu_addon.py)."""
    import tomllib

    from packaging.markers import Marker
    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages: dict[str, list[dict]] = {}
    for p in lock["package"]:
        packages.setdefault(p["name"], []).append(p)

    def closure(roots: list[dict]) -> dict[str, dict]:
        seen: dict[str, dict] = {}
        stack = list(roots)
        while stack:
            dep = stack.pop()
            if "marker" in dep and not Marker(dep["marker"]).evaluate({"extra": ""}):
                continue
            found = [p for p in packages[dep["name"]] if p["version"] == dep.get("version", p["version"])]
            if found[0]["name"] not in seen:
                seen[found[0]["name"]] = found[0]
                stack += found[0].get("dependencies", [])
        return seen

    me = packages["voicerhino"][0]
    base = closure(me["dependencies"])
    gpu = closure(me["optional-dependencies"]["gpu"])
    ranking = {tag: i for i, tag in enumerate(sys_tags())}  # this interpreter: cp312, win_amd64
    wheels = []
    for name in sorted(set(gpu) - set(base) - GPU_SKIP):
        candidates = []
        for w in gpu[name].get("wheels", []):
            tags = parse_wheel_filename(w["url"].rsplit("/", 1)[-1])[3]
            best = min((ranking[t] for t in tags if t in ranking), default=None)
            if best is not None:
                candidates.append((best, w))
        if not candidates:
            raise SystemExit(f"no Windows wheel for {name} in uv.lock")
        w = min(candidates, key=lambda c: c[0])[1]
        wheels.append({"name": name, "version": gpu[name]["version"], "url": w["url"],
                       "sha256": w["hash"].removeprefix("sha256:"), "size": w["size"]})
    key = hashlib.sha256(json.dumps(wheels, sort_keys=True).encode()).hexdigest()[:16]
    manifest = {"key": key, "wheels": wheels}
    dest.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def make_zip(ver: str) -> Path:
    target = DIST / f"VoiceRhino-{ver}-win64.zip"
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in sorted(APP.rglob("*")):
            if f.is_file():
                zf.write(f, Path("VoiceRhino") / f.relative_to(APP))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--console", action="store_true", help="debug build with a console window")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()

    ver = version()
    BUILD.mkdir(parents=True, exist_ok=True)
    make_icon(BUILD / "VoiceRhino.ico")
    make_version_file(BUILD / "version_info.txt", ver)
    gpu = make_gpu_manifest(BUILD / "gpu_addon.json")
    print(f"GPU add-on: {len(gpu['wheels'])} wheels, {sum(w['size'] for w in gpu['wheels']) / 1e6:.0f} MB")

    env = dict(os.environ, VOICERHINO_BUILD_DIR=str(BUILD), VOICERHINO_CONSOLE="1" if args.console else "0")
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "voicerhino.spec"),
                    "--noconfirm", "--clean", "--distpath", str(DIST), "--workpath", str(BUILD)],
                   check=True, env=env, cwd=ROOT)

    for name in EXTRAS:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, APP / name, dirs_exist_ok=True)
        elif src.exists():
            shutil.copy2(src, APP / name)

    print(f"\n{APP}: {folder_size(APP) / 1e6:.0f} MB")
    if not args.no_zip:
        z = make_zip(ver)
        print(f"{z}: {z.stat().st_size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
