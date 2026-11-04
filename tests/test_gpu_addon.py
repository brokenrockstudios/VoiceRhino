"""The portable build's GPU add-on: pinned wheels downloaded, checked and unpacked (local files
stand in for PyPI), and the build script's list made from uv.lock."""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

import pytest

from voicerhino.asr import gpu_addon, storage

ROOT = Path(__file__).resolve().parents[1]


def make_wheel(folder: Path, name: str, files: dict[str, str]) -> dict:
    path = folder / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for rel, text in files.items():
            zf.writestr(rel, text)
    data = path.read_bytes()
    return {"name": name, "version": "1.0", "url": path.as_uri(), "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data)}


@pytest.fixture
def manifest(tmp_path):
    src = tmp_path / "pypi"
    src.mkdir()
    wheels = [make_wheel(src, "fakewhisper", {"fakewhisper/__init__.py": "VALUE = 42\n"}),
              make_wheel(src, "nvidia_fake", {"nvidia/cublas/bin/cublas64_12.dll": "not really a dll"})]
    return {"key": "abc123", "wheels": wheels}


def test_install_unpacks_checks_and_marks(tmp_path, manifest):
    root = tmp_path / "models"
    seen = []
    folder = gpu_addon.install(lambda d, t: seen.append((d, t)), manifest=manifest, root=root)
    assert (folder / "site" / "fakewhisper" / "__init__.py").exists()
    assert (folder / "site" / "nvidia" / "cublas" / "bin" / "cublas64_12.dll").exists()
    assert not (folder / "wheels").exists()  # the downloads are deleted once unpacked
    assert gpu_addon.is_installed(manifest, root)
    total = sum(w["size"] for w in manifest["wheels"])
    assert seen[-1] == (total, total) and all(t == total for _, t in seen)
    # A different set (a VoiceRhino update changed versions) counts as not installed.
    assert not gpu_addon.is_installed({**manifest, "key": "other"}, root)


def test_install_rejects_a_bad_checksum(tmp_path, manifest):
    manifest["wheels"][1]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="checksum"):
        gpu_addon.install(manifest=manifest, root=tmp_path / "models")
    assert not gpu_addon.is_installed(manifest, tmp_path / "models")
    good = gpu_addon.addon_dir(tmp_path / "models") / "wheels" / "fakewhisper-1.0-py3-none-any.whl"
    assert good.exists()  # kept, so trying again only downloads what's missing


def test_activate_makes_it_importable(tmp_path, manifest, monkeypatch):
    root = tmp_path / "models"
    gpu_addon.install(manifest=manifest, root=root)
    monkeypatch.setattr(gpu_addon, "load_manifest", lambda path=None: manifest)
    monkeypatch.setattr(gpu_addon, "_activated", False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PATH", "")
    dll_dirs = []
    monkeypatch.setattr(gpu_addon.os, "add_dll_directory", dll_dirs.append, raising=False)
    assert gpu_addon.activate(root)
    assert sys.path[-1] == str(gpu_addon.addon_dir(root) / "site")  # after the bundled packages
    assert dll_dirs == [str(gpu_addon.addon_dir(root) / "site" / "nvidia" / "cublas" / "bin")]
    import fakewhisper

    assert fakewhisper.VALUE == 42
    sys.modules.pop("fakewhisper", None)


def test_not_needed_outside_the_portable_build():
    assert not gpu_addon.needed()  # tests run from a venv: the gpu extra is used instead
    gpu_addon.ensure()  # does nothing


def test_ensure_refuses_without_an_nvidia_driver(monkeypatch):
    monkeypatch.setattr(gpu_addon, "needed", lambda: True)
    monkeypatch.setattr(gpu_addon, "is_installed", lambda *a, **k: False)
    monkeypatch.setattr(gpu_addon, "nvidia_driver_present", lambda: False)
    with pytest.raises(RuntimeError, match="NVIDIA graphics card"):
        gpu_addon.ensure()


def test_storage_lists_and_keeps_the_addon(tmp_path, manifest):
    from voicerhino.config import AppConfig

    root = tmp_path / "models"
    gpu_addon.install(manifest=manifest, root=root)
    [item] = [d for d in storage.list_downloads(root) if d.key == gpu_addon.KEY]
    assert "GPU add-on" in item.name and not item.unfinished
    config = AppConfig()
    config.asr.model = "fw-large-v3-turbo"
    assert gpu_addon.KEY in storage.used_keys(config)
    assert gpu_addon.KEY not in storage.used_keys(AppConfig())


def test_build_manifest_from_uv_lock(tmp_path):
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import build
    finally:
        sys.path.remove(str(ROOT / "packaging"))
    manifest = build.make_gpu_manifest(tmp_path / "gpu_addon.json")
    names = {w["name"] for w in manifest["wheels"]}
    assert {"faster-whisper", "nvidia-cublas-cu12", "nvidia-cudnn-cu12", "av", "tokenizers"} <= names
    assert not names & {"numpy", "ctranslate2", "onnxruntime"}  # bundled, or never imported
    for w in manifest["wheels"]:
        filename = w["url"].rsplit("/", 1)[-1]
        assert w["url"].startswith("https://files.pythonhosted.org/") and len(w["sha256"]) == 64
        assert "manylinux" not in filename and "macosx" not in filename
