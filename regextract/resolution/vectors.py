"""Embeddings -- used narrowly, on purpose.

The plan is explicit that a vector store is NOT in the core extraction path.
Extraction reads a document we already have in hand; adding retrieval there
would invent a retrieval-quality problem where none exists.

Embeddings earn their place in exactly two jobs:

  1. Resolving an external reference to a document already in the corpus.
     CARL-01 clause 8.1 points at "ECAS General Requirements". If that
     document is held, downstream should get a link, not resolved=false.
     Lives in resolution/retrieval.py: dense vectors fused with BM25, then
     a reranker, then a floor. Qdrant does the fusion natively when
     switched on.

  2. Matching clauses across revisions for stage 10. When revision 5 renumbers
     a clause, the diff has to recognise it as the same clause rather than a
     deletion plus an addition. Lives in publishing.publish.match_clauses.

Offline there is no embedding provider, so we fall back to a deterministic
hashed bag-of-words vector. It is not semantic and it does not pretend to be.
For job 2 it is the right tool even live: renumbering produces near-identical
text, which hashed vectors catch exactly, for free and reproducibly.
"""

from __future__ import annotations

import hashlib
import math
import re

from ..config import Settings, get_settings

VECTOR_SIZE = 256
_WORD = re.compile(r"[a-z0-9]+")


def local_embedding(text: str, size: int = VECTOR_SIZE) -> list[float]:
    """Deterministic hashed bag of words. No provider, no cost, no semantics."""
    vector = [0.0] * size
    for word in _WORD.findall(text.lower()):
        digest = hashlib.blake2b(word.encode("utf-8"), digest_size=4).digest()
        index = int.from_bytes(digest, "big") % size
        vector[index] += 1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def embed(texts: list[str], settings: Settings | None = None, *,
          force_local: bool = False) -> list[list[float]]:
    """Live: the configured embedding model through LiteLLM. Offline, or if
    the provider call fails: local hashed vectors. `force_local` keeps a query
    in the same space as an index that had to fall back."""
    settings = settings or get_settings()
    if settings.offline or force_local:
        return [local_embedding(t) for t in texts]
    try:
        import litellm

        response = litellm.embedding(model=settings.embedding_model, input=texts)
        return [row["embedding"] for row in response["data"]]
    except Exception:  # noqa: BLE001 - embeddings are an optimisation, never fatal
        return [local_embedding(t) for t in texts]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
