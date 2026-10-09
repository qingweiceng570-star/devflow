"""File-backed BM25 index per (repo, generation) (README 13.2).

Pure-Python implementation with the frozen zh-en.v1 tokenizer; the index is
an immutable artifact of one index generation. Scoring uses the classic BM25
formula (k1=1.5, b=0.75); results are deterministic (score desc, chunk id asc).
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.rag.tokenizer import TOKENIZER_VERSION, tokenize

BM25_K1 = 1.5
BM25_B = 0.75


@dataclass
class Bm25Hit:
    chunk_id: str
    score: float


class Bm25Index:
    def __init__(self, *, postings: dict, lengths: list[int], ids: list[str]):
        self.postings = postings  # term -> {doc_index: tf}
        self.lengths = lengths
        self.ids = ids
        self.doc_count = len(ids)
        self.avg_length = (sum(lengths) / self.doc_count) if self.doc_count else 0.0

    # -- build / persist ---------------------------------------------------
    @classmethod
    def build(cls, documents: list[tuple[str, str]]) -> "Bm25Index":
        ids: list[str] = []
        lengths: list[int] = []
        postings: dict[str, dict[int, int]] = {}
        for doc_index, (chunk_id, text) in enumerate(documents):
            terms = tokenize(text)
            ids.append(chunk_id)
            lengths.append(len(terms))
            frequencies: dict[str, int] = {}
            for term in terms:
                frequencies[term] = frequencies.get(term, 0) + 1
            for term, tf in frequencies.items():
                postings.setdefault(term, {})[doc_index] = tf
        return cls(postings=postings, lengths=lengths, ids=ids)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tokenizer_version": TOKENIZER_VERSION,
            "ids": self.ids,
            "lengths": self.lengths,
            "postings": self.postings,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> "Bm25Index":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("tokenizer_version") != TOKENIZER_VERSION:
            raise ValueError("Tokenizer version mismatch for this index generation")
        postings = {
            term: {int(doc): tf for doc, tf in docs.items()}
            for term, docs in payload["postings"].items()
        }
        return cls(postings=postings, lengths=payload["lengths"], ids=payload["ids"])

    # -- query -------------------------------------------------------------
    def search(self, query: str, k: int, *, allowed_ids: set[str] | None = None) -> list[Bm25Hit]:
        terms = set(tokenize(query))
        if not terms or not self.doc_count:
            return []
        scores: dict[int, float] = {}
        for term in terms:
            docs = self.postings.get(term)
            if not docs:
                continue
            df = len(docs)
            idf = ((self.doc_count - df + 0.5) / (df + 0.5)) + 1.0
            idf = max(idf, 0.0)
            for doc_index, tf in docs.items():
                length = self.lengths[doc_index] or 1
                norm = 1 - BM25_B + BM25_B * length / (self.avg_length or 1)
                scores[doc_index] = scores.get(doc_index, 0.0) + idf * (
                    tf * (BM25_K1 + 1) / (tf + BM25_K1 * norm)
                )
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.ids[item[0]]))
        hits: list[Bm25Hit] = []
        for doc_index, score in ranked:
            chunk_id = self.ids[doc_index]
            if allowed_ids is not None and chunk_id not in allowed_ids:
                continue
            hits.append(Bm25Hit(chunk_id=chunk_id, score=round(score, 6)))
            if len(hits) >= k:
                break
        return hits
