"""OPUS-MT models (CTranslate2 format) running inside VoiceRhino on the CPU."""

from __future__ import annotations

import re
from pathlib import Path

from . import Translator

# Marian models are trained on single sentences: a long line is split and translated as a batch.
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s*")


class OpusMTTranslator(Translator):
    def __init__(self, folder: Path, threads: int = 1):
        import ctranslate2
        import sentencepiece

        self._model = ctranslate2.Translator(str(folder), device="cpu", compute_type="int8",
                                             inter_threads=1, intra_threads=threads)
        self._source = sentencepiece.SentencePieceProcessor(model_file=str(folder / "source.spm"))
        self._target = sentencepiece.SentencePieceProcessor(model_file=str(folder / "target.spm"))

    def translate(self, text: str) -> str:
        sentences = [s for s in _SENTENCE_END.split(text.strip()) if s.strip()]
        if not sentences:
            return ""
        batch = [self._source.encode(s, out_type=str) + ["</s>"] for s in sentences]
        results = self._model.translate_batch(batch, beam_size=2, max_decoding_length=256)
        return " ".join(self._target.decode([t for t in r.hypotheses[0] if t != "</s>"]).strip()
                        for r in results)
