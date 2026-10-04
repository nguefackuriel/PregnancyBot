"""Interface commune des lecteurs (moteurs de lecture de l'écriture)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RawRead:
    raw: str | None          # texte lu (None = rien lu / zone vide)
    conf: float              # confiance du lecteur 0..1
    legible: bool = True     # False si le lecteur déclare la zone illisible


class Reader:
    """Un lecteur reçoit la page recalée (1654×2339) et le gabarit du type de page.

    Il doit retourner, pour chaque clé demandée, une lecture brute. Les clés
    sans encre détectée ne lui sont pas transmises (elles sont NON_FOURNI).
    """
    name = "base"
    supports_classification = False

    def read_fields(self, page: np.ndarray, page_type: str, keys: list[str], template: dict,
                    context: dict | None = None) -> dict[str, RawRead]:
        raise NotImplementedError

    def classify_page(self, image: np.ndarray) -> str | None:   # optionnel
        return None


def get_reader(name: str, **kw) -> Reader:
    if name == "crnn":
        from .crnn_reader import CRNNReader
        return CRNNReader(**kw)
    if name == "ollama":
        from .ollama_reader import OllamaReader
        return OllamaReader(**kw)
    if name == "tesseract":
        from .tesseract_reader import TesseractReader
        return TesseractReader(**kw)
    if name == "anthropic":
        from .anthropic_reader import AnthropicReader
        return AnthropicReader(**kw)
    if name == "mock":
        from .mock_reader import MockReader
        return MockReader(**kw)
    raise ValueError(f"lecteur inconnu : {name}")
