"""Optional translation of finalized captions into English, entirely on this PC.

Runs on its own thread so slow translators never delay captions; the translation is
attached to the utterance afterwards via TranscriptStore.update. The models (see
catalog.py) download once on first use; after that nothing goes over the network.
Whisper's own task="translate" (asr.engines) is the other way to get English.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..asr.download import (DownloadCancelled, ensure_download, ensure_folder, is_downloaded,
                            is_folder_downloaded)
from ..transcript import TranscriptStore, Utterance
from .catalog import TranslationModel, get_translation_model

log = logging.getLogger(__name__)

STALE_AFTER = 60.0  # s; a line still waiting this long is skipped (the history shows about a minute)


@dataclass
class TranslationConfig:
    model: str = ""  # translate.catalog id; "" = off
    # Translate only lines in the model's language (any non-English language for multilingual
    # models). Off: every line, e.g. when the guess gets a language wrong.
    only_foreign: bool = True


class Translator(ABC):
    @abstractmethod
    def translate(self, text: str) -> str: ...

    def close(self) -> None:
        pass


# ------------------------------------------------------------ which lines ---

_SCRIPTS = {"HANGUL": "ko", "HIRAGANA": "ja", "KATAKANA": "ja", "KATAKANA-HIRAGANA": "ja",
            "CJK": "zh", "CYRILLIC": "ru"}

# Common words that tell these languages apart. Words shared by several of them ("a", "no",
# "so", "ok") are left out or listed under each.
_WORDS = {
    "en": """the and you i i'm it's that's let's don't can't i'll you're we're they're he's she's
        there's what's is are were am be been it to too of with from by about for at but if not
        yes yeah this that these those what where who how why when there here now just going gonna
        wanna got get go know think want need like right good nice thanks thank sorry please wait
        look see back one out up down did does have has had would could should my your our their
        they we she her his him them us all some any can just really enemy behind""",
    "es": """el la los las de que y es en un una por para con pero muy qué cómo vamos está estoy
        eso esto aquí ahora bien gracias tengo tienes hay yo tú mi se lo le del al más ya también
        porque cuando donde bueno oye nada sí no""",
    "fr": """le la les de des du et est je tu il elle nous vous ils un une pas ne que qui c'est ça
        oui non mais pour avec sur dans bien merci très on y va j'ai suis au aux ce cette moi toi
        allez""",
    "de": """der die das und ist ich du er sie wir ihr nicht ein eine zu mit auf für ja nein was
        wie wo auch noch schon aber oder wenn dann hier jetzt gut danke bitte mal so doch bin hast
        habe kann geht los warum""",
    "it": """il lo la gli le di che e è non un una per con sono sei ma come cosa perché questo
        quello qui ora bene grazie sì ciao andiamo anche ancora molto mi ti ci del della io tu noi
        voi""",
    "pt": """o a os as de que e é não um uma por para com mas você eu ele ela nós isso aqui agora
        bem obrigado obrigada sim vamos está estou tá muito também então porque do da no na meu
        minha""",
}
_WORDS = {lang: set(words.split()) for lang, words in _WORDS.items()}
_LETTERS = {"ñ": "es", "¿": "es", "¡": "es", "ß": "de", "ä": "de", "ö": "de", "ü": "de",
            "ã": "pt", "õ": "pt", "ç": "fr pt", "œ": "fr", "è": "fr it", "ê": "fr pt",
            "à": "fr it pt", "ò": "it", "ì": "it", "ù": "fr it", "á": "es pt", "í": "es pt",
            "ó": "es pt it", "ú": "es pt"}
_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")


def guess_languages(text: str) -> set[str]:
    """The likeliest languages of a caption line, e.g. {"zh"} or {"es", "pt"} for a tie.

    Non-Latin scripts decide it ("other" for scripts not listed); Latin-script text is scored
    on common words and accented letters. Empty when there's nothing to go on ("GG", "10")."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return set()
    scripts = [unicodedata.name(c, "LATIN").split(" ", 1)[0] for c in letters]
    foreign = [s for s in scripts if s != "LATIN"]
    if len(foreign) / len(letters) > 0.3:
        found = {_SCRIPTS.get(s, "other") for s in foreign}
        if "ja" in found:  # kana: Japanese, even though most of its characters are Chinese ones
            return {"ja"}
        counts = {lang: sum(_SCRIPTS.get(s, "other") == lang for s in foreign) for lang in found}
        return {max(counts, key=counts.get)}
    score = dict.fromkeys(_WORDS, 0)
    for word in _TOKEN.findall(text.lower().replace("’", "'")):
        for lang, words in _WORDS.items():
            score[lang] += word in words
    for c in text.lower():
        for lang in _LETTERS.get(c, "").split():
            score[lang] += 1
    best = max(score.values())
    return {lang for lang, s in score.items() if s == best} if best else set()


def wanted(text: str, language: str, only_foreign: bool = True) -> bool:
    """Whether a model that translates `language` ("" = any) should get this line."""
    if not only_foreign:
        return True
    found = guess_languages(text)
    if not found or "en" in found:
        return False
    return not language or language in found


# ------------------------------------------------------------------ models ---

def is_model_downloaded(spec: TranslationModel, root: Path | None = None) -> bool:
    if spec.kind == "opus_mt":
        return is_folder_downloaded(spec.folder, root)
    return all(is_downloaded(url, root) for url in spec.downloads)


def preload_runtime() -> None:
    """Import CTranslate2 now, before any speech engine exists. Its first import (which loads its
    own DLLs, Intel OpenMP among them) aborted the whole process in testing when it overlapped
    sherpa-onnx loading or decoding audio. Takes ~0.2 s and ~12 MB; done even with translation off,
    since it can be turned on while captions run."""
    try:
        import ctranslate2  # noqa: F401
    except Exception:  # e.g. a missing runtime DLL: Opus-MT then reports it when picked
        log.exception("couldn't load CTranslate2")


def load_translator(spec: TranslationModel, progress=None,
                    cancel: threading.Event | None = None) -> Translator:
    """Download (first time only) and start the model."""
    if spec.kind == "opus_mt":
        from .opus import OpusMTTranslator

        return OpusMTTranslator(ensure_folder(spec.folder, spec.downloads, progress, cancel))
    if spec.kind == "hy_mt":
        from .llama import HyMTTranslator

        model = ensure_download(spec.downloads[0], progress, cancel)
        runtime = ensure_download(spec.downloads[1], progress, cancel)
        return HyMTTranslator.start(runtime / "llama-server.exe", model, cancel=cancel)
    raise ValueError(f"unknown translation model kind {spec.kind!r}")


class TranslationWorker:
    """Loads the model, then translates finals in the background and attaches the result."""

    def __init__(self, store: TranscriptStore, config: TranslationConfig,
                 on_status: Callable[[str], None] | None = None,
                 loader: Callable[..., Translator] = load_translator):
        self.store = store
        self.config = config
        self.spec = get_translation_model(config.model)  # KeyError: not a model this version knows
        self.error: str | None = None
        self._on_status = on_status or (lambda _text: None)
        self._loader = loader
        self._translator: Translator | None = None
        self._last_progress = 0.0
        self._cancel = threading.Event()
        self._queue: queue.Queue[Utterance | None] = queue.Queue(maxsize=50)
        self._thread = threading.Thread(target=self._run, name="translator", daemon=True)
        self._thread.start()

    def submit(self, utt: Utterance) -> None:
        if not utt.final or not utt.text or utt.translation:
            return
        if not wanted(utt.text, self.spec.language, self.config.only_foreign):
            return
        try:
            self._queue.put_nowait(utt)
        except queue.Full:
            log.warning("translation backlog full; skipping a line")

    def stop(self) -> None:
        self._cancel.set()
        while True:  # drop the backlog so the stop marker fits
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put_nowait(None)
        # Still loading a model: it notices the cancel and closes it itself when done.
        self._thread.join(timeout=2)
        if self._thread.is_alive() and self._translator is not None:
            self._translator.close()  # mid-translation: end it (closing again later is harmless)

    def _progress(self, done: int, total: int) -> None:
        if total and time.monotonic() - self._last_progress > 0.5:
            self._last_progress = time.monotonic()
            self._on_status(f"Downloading {self.spec.name}: {done / total:.0%} of {total / 1e6:.0f} MB")

    def _run(self) -> None:
        name = self.spec.name.split(" (")[0]
        try:
            if not is_model_downloaded(self.spec):
                self._on_status(f"Downloading {name}…")
            self._translator = self._loader(self.spec, self._progress, self._cancel)
        except DownloadCancelled:
            return
        except Exception as exc:
            if self._cancel.is_set():
                return
            log.exception("loading translation model %s failed", self.spec.id)
            self.error = f"couldn't start {name}: {exc}"
            return
        if self._cancel.is_set():
            self._translator.close()
            return
        self._on_status(f"Translation ready: {name}")
        try:
            while (utt := self._queue.get()) is not None:
                if time.monotonic() - utt.end > STALE_AFTER:
                    continue
                try:
                    translated = self._translator.translate(utt.text)
                    self.error = None
                except Exception as exc:
                    if self._cancel.is_set():
                        return
                    log.warning("translation failed: %s", exc)
                    self.error = str(exc)
                    continue
                if _differs(translated, utt.text):
                    self.store.update(utt.id, translation=translated)
        finally:
            self._translator.close()


def _differs(translated: str, original: str) -> bool:
    """False for empty results and ones that only echo the line back."""
    def norm(s: str) -> str:
        return "".join(c for c in s.lower() if c.isalnum())
    return bool(norm(translated)) and norm(translated) != norm(original)
