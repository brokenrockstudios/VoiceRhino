"""Listing and removing downloaded models, on temporary folders only."""

from __future__ import annotations

from pathlib import Path

import pytest

from voicerhino.asr import storage
from voicerhino.asr.catalog import SPEAKER_MODELS, VAD_MODELS, get_model
from voicerhino.asr.download import local_name
from voicerhino.config import AppConfig

PARAKEET = local_name(get_model("parakeet-tdt-0.6b-v2-int8").source)
NEMOTRON = local_name(get_model("nemotron-streaming-en-160ms-int8").source)
MOONSHINE = local_name(get_model("moonshine-base-en-int8").source)
TURBO = "faster-whisper/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
PART = "sherpa-onnx-moonshine-tiny-en-int8.tar.bz2.part"


def fake_models(root: Path) -> Path:
    """A models folder laid out like a real one, with small files."""
    def put(rel: str, size: int) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    put(f"{PARAKEET}/encoder.int8.onnx", 5000)
    put(f"{PARAKEET}/.complete", 0)
    put(f"{NEMOTRON}/encoder.int8.onnx", 4000)
    put(f"{NEMOTRON}/.complete", 0)
    put(local_name(VAD_MODELS["silero"]), 100)
    put(local_name(SPEAKER_MODELS["titanet-small"]), 300)
    put(f"{TURBO}/snapshots/abc/model.bin", 6000)
    put("faster-whisper/.locks/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/x.lock", 0)
    put("faster-whisper/CACHEDIR.TAG", 10)
    put("some-old-model/encoder.onnx", 2000)  # not in this version's catalog
    put(PART, 700)  # an interrupted download
    put(f"{MOONSHINE}/encoder.onnx", 800)  # extracted, but never marked complete
    return root


def test_lists_names_sizes_and_states(tmp_path):
    fake_models(tmp_path)
    found = storage.list_downloads(tmp_path)
    items = {d.path.relative_to(tmp_path).as_posix(): d for d in found}
    assert set(items) == {PARAKEET, NEMOTRON, "silero_vad.onnx", "nemo_en_titanet_small.onnx", TURBO,
                          "some-old-model", PART, MOONSHINE}
    assert (items[PARAKEET].key, items[PARAKEET].size) == ("parakeet-tdt-0.6b-v2-int8", 5000)
    assert "Parakeet" in items[PARAKEET].name
    assert (items[TURBO].key, items[TURBO].size) == ("fw-large-v3-turbo", 6000)
    assert items["silero_vad.onnx"].key == storage.VAD_KEY
    assert items["nemo_en_titanet_small.onnx"].key == "speaker:titanet-small"
    assert items["some-old-model"].key == "" and not items["some-old-model"].unfinished
    assert items[PART].unfinished and "Moonshine Tiny" in items[PART].name
    assert items[MOONSHINE].unfinished
    assert [d.size for d in found] == sorted((d.size for d in found), reverse=True)


def test_used_keys_follow_the_settings():
    cfg = AppConfig()
    assert storage.used_keys(cfg) == {storage.VAD_KEY, "parakeet-tdt-0.6b-v2-int8"}
    cfg.asr.live_model = "nemotron-streaming-en-160ms-int8"
    cfg.speakers.voice_enabled = True
    assert storage.used_keys(cfg) == {storage.VAD_KEY, "parakeet-tdt-0.6b-v2-int8",
                                      "nemotron-streaming-en-160ms-int8", "speaker:titanet-small"}
    cfg.asr.model = "sense-voice-int8"  # not English: word-by-word is off
    assert "nemotron-streaming-en-160ms-int8" not in storage.used_keys(cfg)
    cfg.asr.model = "no-such-model"  # the app falls back to the default model
    assert "parakeet-tdt-0.6b-v2-int8" in storage.used_keys(cfg)


def test_translation_downloads(tmp_path):
    (tmp_path / "opus-mt-mul-en-ctranslate2").mkdir()
    (tmp_path / "opus-mt-mul-en-ctranslate2" / ".complete").touch()
    (tmp_path / "Hy-MT2-1.8B-Q4_K_M.gguf").write_bytes(b"x" * 50)
    (tmp_path / "llama-b11980-bin-win-cpu-x64").mkdir()
    (tmp_path / "llama-b11980-bin-win-cpu-x64" / ".complete").touch()
    items = {d.path.name: d for d in storage.list_downloads(tmp_path)}
    assert items["opus-mt-mul-en-ctranslate2"].name.startswith("Translation: Other languages")
    assert "llama.cpp" in items["llama-b11980-bin-win-cpu-x64"].name
    assert not any(d.unfinished for d in items.values())
    cfg = AppConfig()
    assert not any(k.startswith(storage.TRANSLATE_PREFIX) for k in storage.used_keys(cfg))
    cfg.translation.model = "opus-mt-pt-en"  # Portuguese uses the multilingual model
    assert items["opus-mt-mul-en-ctranslate2"].key in storage.used_keys(cfg)
    cfg.translation.model = "hy-mt2-1.8b-q4"
    used = storage.used_keys(cfg)
    assert {items["Hy-MT2-1.8B-Q4_K_M.gguf"].key, items["llama-b11980-bin-win-cpu-x64"].key} <= used
    assert items["opus-mt-mul-en-ctranslate2"].key not in used


def test_delete_only_inside_the_models_folder(tmp_path):
    root = fake_models(tmp_path / "models")
    precious = tmp_path / "precious.txt"
    precious.write_text("keep me", encoding="utf-8")
    for outside in (precious, root, root / "faster-whisper", root / ".." / "precious.txt", tmp_path):
        with pytest.raises(ValueError):
            storage.delete_download(outside, root)
    assert precious.exists() and (root / PARAKEET).exists()

    storage.delete_download(root / NEMOTRON, root)  # a folder
    storage.delete_download(root / "silero_vad.onnx", root)  # a file
    storage.delete_download(root / TURBO, root)  # inside the faster-whisper cache
    assert not (root / NEMOTRON).exists() and not (root / "silero_vad.onnx").exists()
    assert not (root / TURBO).exists() and (root / "faster-whisper" / "CACHEDIR.TAG").exists()
    assert (root / PARAKEET / "encoder.int8.onnx").exists()


def test_format_size():
    assert [storage.format_size(n) for n in (0, 1, 632 * 1024, 631 * 2**20, int(1.6 * 2**30))] == [
        "0 KB", "1 KB", "632 KB", "631 MB", "1.6 GB"]
