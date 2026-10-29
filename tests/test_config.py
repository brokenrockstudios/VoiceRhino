"""Config file loading: tolerant of old and odd files, and migrates old defaults."""

from __future__ import annotations

import json
from dataclasses import asdict

from voicerhino import config as config_io
from voicerhino.config import CONFIG_VERSION, AppConfig
from voicerhino.pipeline import PipelineSettings


def write(tmp_path, data) -> object:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_round_trip(tmp_path):
    cfg = AppConfig()
    cfg.pipeline.min_silence = 0.8
    cfg.sources[0].kind = "app"
    path = tmp_path / "config.json"
    config_io.save(cfg, path)
    assert asdict(config_io.load(path)) == asdict(cfg)


def test_old_default_min_speech_is_migrated(tmp_path):
    """Files from before version 2 kept the old 0.25 s, which dropped short words."""
    old = asdict(AppConfig())
    del old["version"]
    old["pipeline"]["min_speech"] = 0.25
    old["pipeline"]["min_silence"] = 0.7  # a user's own choice survives
    cfg = config_io.load(write(tmp_path, old))
    assert cfg.pipeline.min_speech == PipelineSettings().min_speech == 0.1
    assert cfg.pipeline.min_silence == 0.7
    assert cfg.version == CONFIG_VERSION


def test_current_files_keep_their_min_speech(tmp_path):
    current = asdict(AppConfig())
    current["pipeline"]["min_speech"] = 0.25  # chosen in Settings › Timing since version 2
    assert config_io.load(write(tmp_path, current)).pipeline.min_speech == 0.25


def test_missing_and_unknown_keys(tmp_path):
    cfg = config_io.load(write(tmp_path, {"pipeline": {"bogus": 1}, "asr": {"threads": 3}, "future": True}))
    assert cfg.asr.threads == 3
    assert cfg.pipeline.auto_gain is True
    assert asdict(cfg.views[0]) | {"id": ""} == asdict(AppConfig().views[0]) | {"id": ""}


def test_old_transcript_setting_is_dropped(tmp_path):
    """Saving transcripts was removed; a file that had it on loads and saves without it."""
    old = asdict(AppConfig())
    old["save_transcript"] = True
    cfg = config_io.load(write(tmp_path, old))
    assert not hasattr(cfg, "save_transcript")
    config_io.save(cfg, tmp_path / "config.json")
    assert "save_transcript" not in (tmp_path / "config.json").read_text(encoding="utf-8")


def test_old_network_translation_settings_are_dropped(tmp_path):
    """Translation went through an LLM server or DeepL before version 3: its model name, URL and
    API key mean nothing now (and the key mustn't linger on disk), so translation starts off."""
    old = asdict(AppConfig())
    old["version"] = 2
    old["translation"] = {"enabled": True, "backend": "deepl", "target": "English", "only_foreign": False,
                          "base_url": "https://api-free.deepl.com", "model": "qwen2.5:7b", "api_key": "abc:fx"}
    cfg = config_io.load(write(tmp_path, old))
    assert asdict(cfg.translation) == {"model": "", "only_foreign": False}
    config_io.save(cfg, tmp_path / "config.json")
    assert "abc:fx" not in (tmp_path / "config.json").read_text(encoding="utf-8")


def test_unreadable_file_gives_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    random_ids = {"views": None, "sources": None}  # fresh ids every time
    assert asdict(config_io.load(path)) | random_ids == asdict(AppConfig()) | random_ids
    assert asdict(config_io.load(write(tmp_path, [1, 2]))) | random_ids == asdict(AppConfig()) | random_ids


def test_source_ids_and_window_choices_survive_a_round_trip(tmp_path):
    cfg = AppConfig()
    cfg.views[0].hidden_sources = [cfg.sources[0].id]
    path = tmp_path / "config.json"
    config_io.save(cfg, path)
    loaded = config_io.load(path)
    assert loaded.sources[0].id == cfg.sources[0].id
    assert loaded.views[0].hidden_sources == [cfg.sources[0].id]


def test_old_files_get_source_ids(tmp_path):
    old = asdict(AppConfig())
    for src in old["sources"]:
        del src["id"]
    loaded = config_io.load(write(tmp_path, old))
    assert len(loaded.sources[0].id) == 8 and loaded.views[0].hidden_sources == []
