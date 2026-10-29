"""Translation models VoiceRhino can download and run on this PC (into English).

Two kinds, both offline once downloaded:
  - "opus_mt": OPUS-MT (University of Helsinki) models converted to CTranslate2, one per
    language; small and fast, they run inside VoiceRhino.
  - "hy_mt": Tencent's Hy-MT2 1.8B, one model for 33 languages; better, but heavier. It runs in
    llama.cpp's llama-server, started as a hidden process that only listens on 127.0.0.1.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..asr.download import local_name

HF = "https://huggingface.co/"
OPUS_FILES = ("config.json", "shared_vocabulary.json", "source.spm", "target.spm", "model.bin")
LLAMA_CPP = "https://github.com/ggml-org/llama.cpp/releases/download/b11980/llama-b11980-bin-win-cpu-x64.zip"

OPUS_NOTE = ("Small and fast: about 0.05-0.1 s a line on one CPU core, ~0.15 GB of memory. Fine for "
             "short, everyday sentences; slang and long sentences come out rougher.")
MULTI_NOTE = ("One model for many languages, so noticeably rougher than the one-language models. "
              "Just as light: about 0.05-0.1 s a line on one CPU core.")


@dataclass(frozen=True)
class TranslationModel:
    id: str  # stable key stored in config.json
    name: str  # dropdown label
    kind: str  # "opus_mt" or "hy_mt"
    language: str  # code of the language it translates from; "" = any (it tells them apart itself)
    size_mb: int  # approximate download size
    downloads: tuple[str, ...]  # opus_mt: one repo's files; hy_mt: the .gguf and llama.cpp's zip
    license: str
    notes: str = ""

    @property
    def folder(self) -> str:
        """opus_mt: the models-folder subfolder its files go in (the Hugging Face repo's name)."""
        return self.downloads[0].split("/")[4]


def _opus(lang: str, name: str, size_mb: int, repo_lang: str = "", license: str = "Apache-2.0",
          notes: str = OPUS_NOTE) -> TranslationModel:
    repo = f"{HF}gaudi/opus-mt-{repo_lang or lang}-en-ctranslate2/resolve/main/"
    multi = " multilingual" if repo_lang == "mul" or lang == "mul" else ""
    return TranslationModel(
        id=f"opus-mt-{lang}-en", name=f"{name} → English (Opus-MT{multi}, light)", kind="opus_mt",
        language="" if lang == "mul" else lang, size_mb=size_mb,
        downloads=tuple(repo + f for f in OPUS_FILES), license=license, notes=notes)


TRANSLATION_MODELS: list[TranslationModel] = [
    _opus("zh", "Chinese", 160, license="CC-BY-4.0"),
    _opus("ja", "Japanese", 155),
    _opus("ko", "Korean", 160),
    _opus("es", "Spanish", 160),
    _opus("fr", "French", 154),
    _opus("de", "German", 152),
    _opus("it", "Italian", 177),
    _opus("ru", "Russian", 160, license="CC-BY-4.0"),
    # No one-language Portuguese model is published in this format: the multilingual one does it.
    _opus("pt", "Portuguese", 158, repo_lang="mul", notes=MULTI_NOTE),
    _opus("mul", "Other languages", 158, notes=MULTI_NOTE),
    TranslationModel(
        id="hy-mt2-1.8b-q4", name="Any language → English (Hy-MT2 1.8B, better, heavier)", kind="hy_mt",
        language="", size_mb=1157,
        downloads=(f"{HF}tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q4_K_M.gguf", LLAMA_CPP),
        license="Apache-2.0 (model), MIT (llama.cpp)",
        notes="Clearly better translations, and one model covers 33 languages (Chinese, Japanese, "
              "Korean, Spanish, Portuguese, Russian, Arabic, Hindi, Vietnamese, Thai, ...). The cost: "
              "each line keeps 4 CPU cores busy for about 0.3-0.8 s on a fast CPU (longer on older "
              "ones), which can cost frames in CPU-heavy games, and it holds 1-2 GB of memory. "
              "Downloads the model (1.13 GB) and llama.cpp (24 MB), which runs it in a hidden "
              "process that only listens on this PC."),
]

_BY_ID = {m.id: m for m in TRANSLATION_MODELS}


def get_translation_model(model_id: str) -> TranslationModel:
    return _BY_ID[model_id]


def download_names(spec: TranslationModel) -> list[str]:
    """What the model's downloads are called in the models folder."""
    if spec.kind == "opus_mt":
        return [spec.folder]
    return [local_name(url) for url in spec.downloads]
