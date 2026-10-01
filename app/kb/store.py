"""Hybrid index (dense embeddings + BM25) with reciprocal-rank fusion and a relevance gate."""
from __future__ import annotations

import json
import re
import threading

import numpy as np
from rank_bm25 import BM25Okapi

from app.config import CACHE_DIR, EMBED_MODEL, KB_DIR, RAW_DIR

# Relevance gate: a stepped trade-off between semantic and lexical evidence. Calibrated on the small
# labelled set in data/eval/retrieval_queries.json (no held-out set yet; see docs/knowledge_base.md).
GATE = [  # (min dense cosine, min raw BM25) - a hit is relevant if it clears ANY step
    (0.55, 0.0),   # strong semantic match
    (0.40, 4.0),   # moderate semantic + some lexical overlap
    (0.30, 6.0),   # weaker semantic (cross-lingual, colloquial) + strong lexical overlap
]


def passes_gate(dense: float, bm25: float) -> bool:
    return any(dense >= d and bm25 >= b for d, b in GATE)

STOPWORDS = set("""
a an the and or of to in on for is are was were be been it this that with as at by from can do does i my me you your we
our what which who how when where will would should could there their they he she his her not no yes if any about into
than then so but also just only up out its has have had may might must please tell know want need
ang ng mga sa na po ba ko mo ako ikaw kayo siya ito iyan yung yun naman lang pa din rin nga kasi
yang dan di ke dari untuk ini itu dengan saya anda kamu apa bagaimana berapa kapan apakah atau juga sudah belum akan bisa
""".split())

_embedder = None
_embed_lock = threading.Lock()


def get_embedder():
    global _embedder
    with _embed_lock:
        if _embedder is None:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(EMBED_MODEL, cache_dir=str(CACHE_DIR / "fastembed"))
    return _embedder


def embed(texts: list[str]) -> np.ndarray:
    vecs = np.array(list(get_embedder().embed(texts)), dtype=np.float32)
    return vecs / np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9, None)


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if t not in STOPWORDS and len(t) > 1]


def passage(rec: dict) -> str:
    return f"{rec['title']}\n{rec['content']}"


def build_index(collection: str) -> None:
    folder = KB_DIR / collection
    records = [json.loads(l) for l in (folder / "records.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    active = [r for r in records if r["status"] == "active"]
    np.save(folder / "embeddings.npy", embed([passage(r) for r in active]))
    (folder / "index_ids.json").write_text(json.dumps([r["record_id"] for r in active]), encoding="utf-8")
    KnowledgeBase._cache.pop(collection, None)


def citation(rec: dict) -> str:
    src = rec["source"]
    page = f", p.{src['page']}" if src.get("page") else ""
    return f"{src['path']} > {src['section']}{page} (v{rec['version']})"


class KnowledgeBase:
    _cache: dict[str, "KnowledgeBase"] = {}

    @classmethod
    def get(cls, collection: str) -> "KnowledgeBase":
        if collection not in cls._cache:
            cls._cache[collection] = cls(collection)
        return cls._cache[collection]

    def __init__(self, collection: str):
        folder = KB_DIR / collection
        if not (folder / "embeddings.npy").exists():
            raise FileNotFoundError(f"Knowledge base '{collection}' not built. Run: python -m app.kb.ingest")
        self.collection = collection
        self.records = {r["record_id"]: r for r in
                        (json.loads(l) for l in (folder / "records.jsonl").read_text(encoding="utf-8").splitlines() if l.strip())}
        self.ids: list[str] = json.loads((folder / "index_ids.json").read_text(encoding="utf-8"))
        self.matrix = np.load(folder / "embeddings.npy")
        self.bm25 = BM25Okapi([tokenize(passage(self.records[i])) for i in self.ids])
        meta_path = folder / "meta.json"
        self.meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        manifest = json.loads((RAW_DIR / collection / "sources.json").read_text(encoding="utf-8"))
        self.expansions = [(re.compile(p, re.I), v) for p, v in manifest.get("query_expansions", {}).items()]

    def expand(self, query: str) -> str:
        """Append canonical KB terms for colloquial/local words found in the query (market glossary)."""
        extra = [v for pattern, v in self.expansions if pattern.search(query)]
        return f"{query} {' '.join(extra)}" if extra else query

    def search(self, query: str, k: int = 4, category: str | None = None, pool: int = 20) -> list[dict]:
        if not query.strip():
            return []
        query = self.expand(query)
        dense = self.matrix @ embed([query])[0]
        tokens = tokenize(query)
        lexical = self.bm25.get_scores(tokens) if tokens else np.zeros(len(self.ids))

        fused: dict[int, float] = {}
        for scores in (dense, lexical):
            for rank, idx in enumerate(np.argsort(-scores)[:pool]):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (60 + rank + 1)

        hits = []
        for idx in sorted(fused, key=fused.get, reverse=True):
            rec = self.records[self.ids[idx]]
            if category and rec["category"] != category:
                continue
            d, b = float(dense[idx]), float(lexical[idx])
            hits.append({
                "record_id": rec["record_id"], "title": rec["title"], "content": rec["content"],
                "category": rec["category"], "products": rec["products"], "audience": rec["audience"],
                "source": rec["source"], "version": rec["version"], "citation": citation(rec),
                "scores": {"dense": round(d, 3), "bm25": round(b, 2), "rrf": round(fused[idx], 4)},
                "relevant": passes_gate(d, b),
            })
            if len(hits) >= k:
                break
        return hits

    def stats(self) -> dict:
        return {"collection": self.collection, "indexed_records": len(self.ids), "kb_version": self.meta.get("kb_version"),
                "embedding_model": EMBED_MODEL}
