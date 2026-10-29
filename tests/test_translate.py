"""Offline translation: which lines get translated, the worker, the downloads, and the two
model runners. Tests that need a real model skip unless it's already in the models folder."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psutil
import pytest

from voicerhino.asr import download
from voicerhino.paths import models_dir
from voicerhino.transcript import TranscriptStore, Utterance
from voicerhino.translate import (TranslationConfig, TranslationWorker, Translator, _differs,
                                  guess_languages, is_model_downloaded, wanted)
from voicerhino.translate.catalog import OPUS_FILES, TRANSLATION_MODELS, download_names, get_translation_model
from voicerhino.translate.llama import HyMTTranslator

HY = get_translation_model("hy-mt2-1.8b-q4")


def test_guess_languages():
    assert guess_languages("你好，我们去中路") == {"zh"}
    assert guess_languages("ちょっと待って、回復するから。") == {"ja"}  # kana among the kanji
    assert guess_languages("야 빨리 와, 우리 지금 싸우고 있어!") == {"ko"}
    assert guess_languages("Привет, как дела") == {"ru"}
    assert guess_languages("مرحبا كيف حالك") == {"other"}
    assert guess_languages("GG 你好吗") == {"zh"}
    assert guess_languages("Okay, I'm going to push top lane.") == {"en"}
    assert guess_languages("Cuidado, hay dos detrás de ti, vamos a la torre.") == {"es"}
    assert guess_languages("Attends, je suis derrière toi.") == {"fr"}
    assert guess_languages("Ich will das so machen, warte mal.") == {"de"}  # "will", "so" aren't English here
    assert guess_languages("Andiamo, sono qui con te.") == {"it"}
    assert guess_languages("Você está aqui, então vamos.") == {"pt"}
    assert guess_languages("Mañana") == {"es"}  # accents count when there are no common words
    assert guess_languages("GG") == set() and guess_languages("10, 20!") == set()


def test_wanted_lines():
    chinese, english, spanish = "你好，我们去中路", "Nice, I'm going mid.", "Vamos a la torre"
    assert wanted(chinese, "zh") and not wanted(english, "zh") and not wanted(spanish, "zh")
    assert wanted(spanish, "es") and not wanted(chinese, "es") and not wanted(english, "es")
    assert wanted(chinese, "") and wanted(spanish, "") and not wanted(english, "")  # any-language models
    assert not wanted("GG", "")  # nothing to go on: skipped
    assert wanted(english, "zh", only_foreign=False)


def test_differs():
    assert _differs("Hello, let's go mid", "你好，我们去中路")
    assert not _differs("Nice shot.", "nice shot")  # an echo, not a translation
    assert not _differs("  ", "hola")


def test_catalog():
    ids = [m.id for m in TRANSLATION_MODELS]
    assert len(ids) == len(set(ids))
    for m in TRANSLATION_MODELS:
        assert m.size_mb > 0 and m.license and m.notes and "→ English" in m.name
        if m.kind == "opus_mt":
            assert [u.rsplit("/", 1)[-1] for u in m.downloads] == list(OPUS_FILES)
            assert all(u.startswith("https://huggingface.co/gaudi/") for u in m.downloads)
    assert get_translation_model("opus-mt-zh-en").folder == "opus-mt-zh-en-ctranslate2"
    portuguese, other = get_translation_model("opus-mt-pt-en"), get_translation_model("opus-mt-mul-en")
    assert portuguese.folder == other.folder and (portuguese.language, other.language) == ("pt", "")
    assert download_names(HY) == ["Hy-MT2-1.8B-Q4_K_M.gguf", "llama-b11980-bin-win-cpu-x64"]
    assert HY.downloads[1].startswith("https://github.com/ggml-org/llama.cpp/releases/download/")


# ------------------------------------------------------------------ worker ---

class FakeTranslator(Translator):
    def __init__(self):
        self.seen: list[str] = []
        self.closed = False

    def translate(self, text: str) -> str:
        self.seen.append(text)
        return {"你好，我们去中路": "Hello, let's go mid"}.get(text, text)

    def close(self) -> None:
        self.closed = True


def final(i: int, text: str, age: float = 1.0) -> Utterance:
    now = time.monotonic()
    return Utterance(i, "main", text, now - age - 1, now - age, True)


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_worker_translates_wanted_lines_only():
    store, fake, statuses = TranscriptStore(), FakeTranslator(), []
    worker = TranslationWorker(store, TranslationConfig(model="opus-mt-zh-en"), statuses.append,
                               loader=lambda spec, progress, cancel: fake)
    for i, text in enumerate(["你好，我们去中路", "Already English.", "小心后面", "老了"], 1):
        utt = final(i, text, age=120 if text == "老了" else 1)  # the last one waited too long
        store.push(utt)
        worker.submit(utt)
    assert wait_for(lambda: "小心后面" in fake.seen)
    worker.stop()
    first, second, third, stale = store.finals()
    assert first.translation == "Hello, let's go mid"
    assert second.translation is None  # English: never sent
    assert third.translation is None  # came back unchanged: not shown
    assert "老了" not in fake.seen and fake.closed
    assert "Translation ready: Chinese → English" in statuses


def test_worker_reports_a_model_that_wont_load():
    def broken(spec, progress, cancel):
        raise OSError("disk full")

    worker = TranslationWorker(TranscriptStore(), TranslationConfig(model="opus-mt-zh-en"), loader=broken)
    assert wait_for(lambda: worker.error is not None)
    assert "disk full" in worker.error and "Chinese → English" in worker.error
    worker.stop()


def test_stopping_cancels_a_download():
    started = threading.Event()

    def slow(spec, progress, cancel):
        started.set()
        while not cancel.wait(0.01):
            pass
        raise download.DownloadCancelled("x")

    worker = TranslationWorker(TranscriptStore(), TranslationConfig(model="hy-mt2-1.8b-q4"), loader=slow)
    assert started.wait(2)
    t = time.monotonic()
    worker.stop()
    assert time.monotonic() - t < 1 and worker.error is None


def test_unknown_model_is_refused():
    with pytest.raises(KeyError):
        TranslationWorker(TranscriptStore(), TranslationConfig(model="qwen2.5:7b"))


# --------------------------------------------------------------- downloads ---

class _Files(BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}

    def do_GET(self):
        data = self.files.get(self.path)
        self.send_response(200 if data is not None else 404)
        self.send_header("Content-Length", str(len(data or b"")))
        self.end_headers()
        self.wfile.write(data or b"")

    def log_message(self, *args):
        pass


@pytest.fixture
def file_server():
    httpd = HTTPServer(("127.0.0.1", 0), _Files)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", _Files.files
    httpd.shutdown()


def test_folder_and_zip_downloads(file_server, tmp_path):
    url, files = file_server
    files["/repo/a.json"], files["/repo/model.bin"] = b"{}", b"x" * 1000
    folder = download.ensure_folder("repo", [url + "/repo/a.json", url + "/repo/model.bin"], root=tmp_path)
    assert (folder / "model.bin").read_bytes() == b"x" * 1000 and download.is_folder_downloaded("repo", tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("llama-server.exe", b"MZ")
        zf.writestr("../escape.txt", b"nope")
    files["/llama-b1-bin-win-cpu-x64.zip"] = buffer.getvalue()
    out = download.ensure_download(url + "/llama-b1-bin-win-cpu-x64.zip", root=tmp_path)
    assert out == tmp_path / "llama-b1-bin-win-cpu-x64" and (out / "llama-server.exe").read_bytes() == b"MZ"
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "llama-b1-bin-win-cpu-x64.zip").exists()
    assert download.is_downloaded(url + "/llama-b1-bin-win-cpu-x64.zip", tmp_path)


# ------------------------------------------------------------------ Hy-MT2 ---

class _Chat(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict, dict]] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Chat.requests.append((self.path, dict(self.headers), body))
        data = json.dumps({"choices": [{"message": {"content": " Hello, let's go mid \n"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_hy_mt_request_never_uses_a_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")  # would break the request if used
    monkeypatch.setenv("NO_PROXY", "")
    httpd = HTTPServer(("127.0.0.1", 0), _Chat)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        t = HyMTTranslator(f"http://127.0.0.1:{httpd.server_port}", "secret")
        assert t.translate("你好，我们去中路") == "Hello, let's go mid"
    finally:
        httpd.shutdown()
    path, headers, body = _Chat.requests[-1]
    assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer secret"
    assert body["messages"][0]["content"].endswith("\n\n你好，我们去中路")
    assert "into English" in body["messages"][0]["content"]


def test_llama_server_that_exits_is_reported(tmp_path):
    with pytest.raises(RuntimeError, match="exited"):  # python rejects llama-server's arguments
        HyMTTranslator.start(Path(sys.executable), tmp_path / "model.gguf", timeout=20)


needs_hy = pytest.mark.skipif(not is_model_downloaded(HY), reason="Hy-MT2 and llama.cpp aren't downloaded")
needs_opus = pytest.mark.skipif(not is_model_downloaded(get_translation_model("opus-mt-zh-en")),
                                reason="Opus-MT zh-en isn't downloaded")


def _hy_paths() -> tuple[Path, Path]:
    return models_dir() / "llama-b11980-bin-win-cpu-x64" / "llama-server.exe", models_dir() / download_names(HY)[0]


@needs_hy
def test_real_hy_mt2_translates_on_loopback_only():
    exe, model = _hy_paths()
    t = HyMTTranslator.start(exe, model)
    proc = psutil.Process(t._proc.pid)
    try:
        assert "let's go" in t.translate("你好，我们去中路").lower().replace("’", "'")
        assert "tower" in t.translate("Cuidado, vamos a la torre.").lower()
        conns = proc.net_connections()
        assert conns and all(c.laddr.ip == "127.0.0.1" for c in conns)
        assert all(not c.raddr or c.raddr.ip == "127.0.0.1" for c in conns)  # nothing outside this PC
    finally:
        t.close()
    assert not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE


@needs_hy
def test_llama_server_dies_with_voicerhino():
    exe, model = _hy_paths()
    script = ("import sys, time\nfrom pathlib import Path\nfrom voicerhino.translate.llama import HyMTTranslator\n"
              f"t = HyMTTranslator.start(Path(r'{exe}'), Path(r'{model}'))\n"
              "print(t._proc.pid, flush=True)\ntime.sleep(60)\n")
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        server = psutil.Process(int(parent.stdout.readline()))
        assert server.is_running()
    finally:
        parent.kill()  # like a crash: no cleanup code runs
        parent.wait()
    assert wait_for(lambda: not server.is_running(), timeout=10)


@needs_opus
def test_real_opus_mt():
    from voicerhino.translate.opus import OpusMTTranslator

    t = OpusMTTranslator(models_dir() / "opus-mt-zh-en-ctranslate2")
    assert "mid" in t.translate("你好，我们去中路").lower()
    two = t.translate("小心后面有人！快跑。")  # two sentences, translated as a batch
    assert "run" in two.lower() and "watch out" in two.lower()
