"""Dense half of recall. Lexical search stays in knowledge.py.

Fusion is reciprocal rank fusion: a note both channels return beats a note
only one channel returns, and neither channel's score scale has to match the
other. The embedding model is optional. Tests inject one. Production loads an
MLX model when that package is installed and BRAIN_EMBED is not 0.
"""

from __future__ import annotations

import math
import os
import struct
from typing import Protocol

RRF_K = 60
# Floor and band measured on this corpus with bge-small (2026-09-27):
# a real paraphrase sat near 0.60-0.75, an unrelated note near 0.48.
# Keep the cluster around the best hit, and drop anything below the floor.
MIN_COSINE = 0.55
MARGIN = 0.15
MODEL_ID = "mlx-community/bge-small-en-v1.5-4bit"

_UNSET = object()
_current: object = _UNSET
embed_error = ""


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, same dimension for every text in the call."""


def set_embedder(embedder: Embedder | None) -> None:
    global _current, embed_error
    _current = embedder
    embed_error = ""


def get_embedder() -> Embedder | None:
    global _current, embed_error
    if _current is not _UNSET:
        return _current if _current is None or hasattr(_current, "embed") else None
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        _current = None
        return None
    try:
        loaded = _load_mlx()
    except Exception as exc:
        embed_error = f"{type(exc).__name__}: {exc}"
        _current = None
        return None
    _current = loaded
    return loaded


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *[float(x) for x in vec])


def unpack(blob: bytes, dim: int) -> list[float]:
    if len(blob) != dim * 4:
        return []
    return list(struct.unpack(f"<{dim}f", blob))


def cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = left_norm = right_norm = 0.0
    for x, y in zip(left, right):
        dot += x * y
        left_norm += x * x
        right_norm += y * y
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def rrf(
    rankings: list[list[tuple[str, str, str]]],
    limit: int,
    k: int = RRF_K,
    prefer_lexical: bool = False,
) -> list[tuple[str, str, str]]:
    """Fuse ranked lists. The first list keeps the snippet.

    On a tie, an identifier query keeps the lexical hit (error codes, ids).
    Any other query keeps the semantic hit, which is the paraphrase.
    """
    scores: dict[str, float] = {}
    support: dict[str, int] = {}
    payload: dict[str, tuple[str, str, str]] = {}
    channel_rank: list[dict[str, int]] = []
    for ranking in rankings:
        ranks: dict[str, int] = {}
        for rank, row in enumerate(ranking, start=1):
            key = row[2]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            support[key] = support.get(key, 0) + 1
            if key not in payload:
                payload[key] = row
            ranks[key] = rank
        channel_rank.append(ranks)
    lexical_rank = channel_rank[0] if channel_rank else {}
    semantic_rank = channel_rank[1] if len(channel_rank) > 1 else {}

    def sort_key(key: str) -> tuple[float, int, float, float]:
        lexical = -float(lexical_rank.get(key, 10**6))
        semantic = -float(semantic_rank.get(key, 10**6))
        first, second = (lexical, semantic) if prefer_lexical else (semantic, lexical)
        return (scores[key], support[key], first, second)

    ordered = sorted(scores, key=sort_key, reverse=True)
    return [payload[key] for key in ordered[:limit]]


class _MlxEmbedder:
    model_id = MODEL_ID

    def __init__(self, model: object, tokenizer: object, generate) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._generate = generate

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), 32):
            chunk = texts[start:start + 32]
            result = self._generate(self._model, self._tokenizer, texts=chunk)
            embeds = result.text_embeds if hasattr(result, "text_embeds") else result
            rows = embeds.tolist()
            if rows and isinstance(rows[0], float):
                rows = [rows]
            out.extend([float(x) for x in row] for row in rows)
        return out


def _load_mlx() -> Embedder | None:
    try:
        from mlx_embeddings.utils import generate, load
    except ImportError:
        try:
            from mlx_embeddings import generate, load
        except ImportError:
            return None
    model, tokenizer = load(MODEL_ID)
    return _MlxEmbedder(model, tokenizer, generate)
