"""Reference resolution: hybrid search, a reranker, and a floor below which
the answer is NOT_FOUND.

This is a hybrid retrieval stack, pointed at the one job in this
project where retrieval belongs. CARL-01 clause 8.1 cites "clause no. 6 of
ECAS General Requirements". If that document is already in the corpus,
downstream systems should get a link to it. If it is not, they should get an
honest "not found", never the nearest-looking title.

  wide    dense vectors and BM25 keyword scores over the corpus catalogue,
          fused with reciprocal rank fusion. Dense finds paraphrases; BM25
          finds exact tokens like "No. 28" that dense vectors blur. When
          Qdrant is on it does the fusion natively (named dense and sparse
          vectors, prefetch + RRF); otherwise the same fusion runs in process.
  narrow  a reranker reads the reference and each candidate together: a model
          reranker (Jina, through LiteLLM) when configured, otherwise a
          deterministic lexical one.
  floor   below a minimum score the answer is NOT_FOUND. Plus one guard no
          score can override: every number in the reference must appear in
          the candidate. "Federal Law No. 24" is lexically almost identical to
          "Federal Law No. 28", and it is a different law.

What this deliberately does not resolve: standard codes such as
"IEC 60335-2-13". Those are identifiers, and the right tool for them is
deterministic normalisation, not similarity. An embedding puts IEC 60335-1
next to IEC 60335-2-13, and they are different standards.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from ..config import Settings, get_settings
from ..contract import Item, Resolution
from ..normalize import normalize_ws
from .vectors import VECTOR_SIZE, cosine, embed

_TOKEN = re.compile(r"[a-z0-9]+")
_NUMBER = re.compile(r"\d+")
RRF_K = 60
_POINT_NAMESPACE = uuid.UUID("0c9b3f4e-5a41-4f0e-9a8d-6f1e2b7c3d10")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(normalize_ws(text))


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    title: str
    aliases: tuple[str, ...] = ()
    jurisdiction: str = ""
    kind: str = ""
    synthetic: bool = False
    distractor: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return (self.title, *self.aliases)

    @property
    def search_text(self) -> str:
        return " ".join(self.names)


def load_catalog(path: Path, distractors_path: Path | None = None) -> list[CatalogEntry]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = [
        CatalogEntry(
            id=doc["id"], title=doc["title"], aliases=tuple(doc.get("aliases", [])),
            jurisdiction=doc.get("jurisdiction", ""), kind=doc.get("kind", ""),
            synthetic=bool(doc.get("synthetic", False)),
        )
        for doc in data["documents"]
    ]
    if distractors_path and Path(distractors_path).exists():
        lines = Path(distractors_path).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            title = line.strip()
            if title and not title.startswith("#"):
                entries.append(CatalogEntry(id=f"NOISE-{number:04d}", title=title, distractor=True))
    return entries


# ---------------------------------------------------------------------------
# Wide: BM25 + dense, fused
# ---------------------------------------------------------------------------


class BM25:
    """Okapi BM25, the keyword half of hybrid search. Small enough to read."""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        self.average = (sum(self.lengths) / len(documents)) if documents else 0.0
        frequency: Counter = Counter()
        for doc in documents:
            frequency.update(set(doc))
        n = len(documents)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in frequency.items()}

    def scores(self, query: list[str]) -> list[float]:
        results = []
        for tf, length in zip(self.tf, self.lengths):
            score = 0.0
            for term in query:
                count = tf.get(term, 0)
                if count:
                    norm = 1 - self.b + self.b * length / (self.average or 1)
                    score += self.idf[term] * count * (self.k1 + 1) / (count + self.k1 * norm)
            results.append(score)
        return results


def reciprocal_rank_fusion(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Each ranking votes 1/(k + rank). Agreement between rankings wins, and
    raw scores on different scales never have to be compared."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, index in enumerate(ranking):
            fused[index] = fused.get(index, 0.0) + 1.0 / (k + rank + 1)
    return fused


def sparse_vector(tokens: list[str]) -> tuple[list[int], list[float]]:
    """Term counts keyed by a stable hash. Qdrant applies the IDF itself."""
    weights: dict[int, float] = {}
    for term, count in Counter(tokens).items():
        index = int.from_bytes(hashlib.blake2b(term.encode(), digest_size=4).digest(), "big") & 0x7FFFFFFF
        weights[index] = weights.get(index, 0.0) + float(count)
    return list(weights), list(weights.values())


class HybridIndex:
    def __init__(self, entries: list[CatalogEntry], settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.entries = entries
        self._tokens = [tokenize(e.search_text) for e in entries]
        self._bm25 = BM25(self._tokens)
        self._dense = embed([e.search_text for e in entries], self.settings)
        # If the provider call fell back to hashed vectors, queries must too.
        self._local = bool(self._dense) and len(self._dense[0]) == VECTOR_SIZE
        self._by_id = {e.id: e for e in entries}
        self._client = None
        self.backend = "in-process"
        if self.settings.use_qdrant and entries:
            self._index_in_qdrant()

    def _index_in_qdrant(self) -> None:
        try:
            from qdrant_client import QdrantClient, models

            url = self.settings.qdrant_url
            client = QdrantClient(":memory:") if url == ":memory:" else QdrantClient(url=url)
            name = f"{self.settings.qdrant_collection}_catalog"
            if not client.collection_exists(name):
                client.create_collection(
                    name,
                    vectors_config={"dense": models.VectorParams(
                        size=len(self._dense[0]), distance=models.Distance.COSINE)},
                    sparse_vectors_config={"bm25": models.SparseVectorParams(
                        modifier=models.Modifier.IDF)},
                )
            points = []
            for entry, vector, tokens in zip(self.entries, self._dense, self._tokens):
                indices, values = sparse_vector(tokens)
                points.append(models.PointStruct(
                    id=str(uuid.uuid5(_POINT_NAMESPACE, entry.id)),
                    vector={"dense": vector, "bm25": models.SparseVector(indices=indices, values=values)},
                    payload={"id": entry.id, "title": entry.title},
                ))
            client.upsert(name, points=points)
            self._client, self._collection, self.backend = client, name, "qdrant"
        except Exception:  # noqa: BLE001 - an optional integration never takes the pipeline down
            self._client, self.backend = None, "in-process"

    def candidates(self, query: str, limit: int) -> list[tuple[CatalogEntry, float]]:
        tokens = tokenize(query)
        if not tokens or not self.entries:
            return []
        query_vector = embed([query], self.settings, force_local=self._local)[0]

        if self._client is not None:
            from qdrant_client import models

            indices, values = sparse_vector(tokens)
            hits = self._client.query_points(
                self._collection,
                prefetch=[
                    models.Prefetch(query=query_vector, using="dense", limit=limit * 3),
                    models.Prefetch(query=models.SparseVector(indices=indices, values=values),
                                    using="bm25", limit=limit * 3),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
            ).points
            return [(self._by_id[h.payload["id"]], float(h.score))
                    for h in hits if h.payload and h.payload.get("id") in self._by_id]

        dense = [cosine(query_vector, v) for v in self._dense]
        keyword = self._bm25.scores(tokens)
        order = range(len(self.entries))
        dense_rank = sorted(order, key=lambda i: -dense[i])[: limit * 3]
        keyword_rank = [i for i in sorted(order, key=lambda i: -keyword[i]) if keyword[i] > 0][: limit * 3]
        fused = reciprocal_rank_fusion([dense_rank, keyword_rank])
        top = sorted(fused.items(), key=lambda pair: -pair[1])[:limit]
        return [(self.entries[i], score) for i, score in top]


# ---------------------------------------------------------------------------
# Narrow: rerank, floor, number check
# ---------------------------------------------------------------------------


def rerank(query: str, candidates: list[tuple[CatalogEntry, float]],
           settings: Settings) -> tuple[list[tuple[CatalogEntry, float]], str]:
    if not candidates:
        return [], "none"
    if settings.rerank_model and not settings.offline:
        try:
            import litellm

            response = litellm.rerank(model=settings.rerank_model, query=query,
                                      documents=[c.search_text for c, _ in candidates],
                                      top_n=len(candidates))
            ranked = []
            for result in response.results:
                index = result["index"] if isinstance(result, dict) else result.index
                score = result["relevance_score"] if isinstance(result, dict) else result.relevance_score
                ranked.append((candidates[index][0], float(score)))
            return ranked, settings.rerank_model
        except Exception:  # noqa: BLE001 - fall back to lexical, and the method says so
            pass
    query_norm = normalize_ws(query)
    scored = [
        (entry, max(fuzz.token_sort_ratio(query_norm, normalize_ws(name)) for name in entry.names) / 100.0)
        for entry, _ in candidates
    ]
    scored.sort(key=lambda pair: -pair[1])
    return scored, "lexical"


def numbers_agree(query: str, entry: CatalogEntry) -> bool:
    wanted = set(_NUMBER.findall(query))
    if not wanted:
        return True
    return any(wanted <= set(_NUMBER.findall(name)) for name in entry.names)


def resolve(query: str, index: HybridIndex, settings: Settings | None = None) -> Resolution:
    settings = settings or get_settings()
    query = " ".join(query.split())
    if not query:
        return Resolution(status="not_found", reason="empty reference")

    wide = index.candidates(query, settings.resolution_candidates)
    ranked, reranker = rerank(query, wide, settings)
    method = f"hybrid[{index.backend}] -> rerank[{reranker}]"
    floor = settings.resolution_min_score if reranker in ("lexical", "none") else settings.resolution_min_score_model

    for entry, score in ranked:
        if score < floor:
            break
        if not numbers_agree(query, entry):
            continue
        return Resolution(status="resolved", target_document_id=entry.id, target_title=entry.title,
                          score=round(score, 4), method=method, candidates_considered=len(wide),
                          reason=f"score {score:.2f} clears the floor of {floor:.2f}")

    if not ranked:
        reason = "no candidates"
    elif ranked[0][1] < floor:
        reason = f"best candidate {ranked[0][0].title!r} scored {ranked[0][1]:.2f}, below the floor of {floor:.2f}"
    else:
        reason = f"best candidate {ranked[0][0].title!r} fails the number check"
    return Resolution(status="not_found", score=round(ranked[0][1], 4) if ranked else 0.0,
                      method=method, reason=reason, candidates_considered=len(wide))


# ---------------------------------------------------------------------------
# In the pipeline, and in the evals
# ---------------------------------------------------------------------------


def reference_query(item: Item) -> str | None:
    """External cross-references and legal references name another document."""
    if item.kind == "cross_reference" and item.payload.get("scope") == "external":
        return item.payload.get("target_document") or None
    if item.kind == "entity" and item.type_name == "legal_reference":
        return item.payload.get("text") or None
    return None


def resolve_references(items: list[Item], settings: Settings | None = None,
                       index: HybridIndex | None = None) -> tuple[list[Item], dict]:
    settings = settings or get_settings()
    targets = [(item, query) for item in items if (query := reference_query(item))]
    counters: dict = {"references": len(targets), "resolved": 0, "not_found": 0}
    if not targets:
        return items, counters
    index = index or HybridIndex(load_catalog(settings.catalog_path, settings.distractors_path), settings)
    for item, query in targets:
        item.resolution = resolve(query, index, settings)
        counters[item.resolution.status] += 1
    counters.update(backend=index.backend, catalog_size=len(index.entries))
    return items, counters


def evaluate_resolution(settings: Settings, gold_path: Path) -> dict:
    """The same queries against a clean catalogue and a
    noisy one. If accuracy drops with noise, the floor is too low."""
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    clean = HybridIndex(load_catalog(settings.catalog_path, None), settings)
    noisy = HybridIndex(load_catalog(settings.catalog_path, settings.distractors_path), settings)

    rows = []
    for case in gold["queries"]:
        on_clean = resolve(case["query"], clean, settings)
        on_noisy = resolve(case["query"], noisy, settings)
        rows.append({
            "query": case["query"],
            "expected": case["expected"],
            "clean": on_clean.target_document_id or None,
            "noisy": on_noisy.target_document_id or None,
            "reason": on_noisy.reason,
        })

    def accuracy(key: str) -> float:
        return round(sum(1 for r in rows if r[key] == r["expected"]) / len(rows), 4) if rows else 0.0

    return {
        "rows": rows,
        "clean_accuracy": accuracy("clean"),
        "noisy_accuracy": accuracy("noisy"),
        "wrong_links": sum(1 for r in rows for key in ("clean", "noisy")
                           if r[key] and r[key] != r["expected"]),
        "catalog": {"clean": len(clean.entries), "noisy": len(noisy.entries)},
        "caveat": ("Eight hand-written cases. They show the floor and the number check "
                   "doing their job under noise; they are not a recall measurement."),
    }
