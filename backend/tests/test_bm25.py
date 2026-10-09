import json

import pytest

from app.rag.bm25_store import Bm25Index


def build_index() -> Bm25Index:
    return Bm25Index.build(
        [
            ("c1", "the quick brown fox jumps over the lazy dog"),
            ("c2", "machine learning models need training data"),
            ("c3", "quick training tips for marathon runners"),
        ]
    )


def test_search_ranks_relevant_first():
    index = build_index()
    hits = index.search("quick brown fox", 3)
    assert hits[0].chunk_id == "c1"
    assert hits[0].score > 0
    assert all(hit.score > 0 for hit in hits)


def test_search_deterministic_tie_break():
    index = Bm25Index.build([("b", "same words here"), ("a", "same words here")])
    hits = index.search("same words", 2)
    assert [hit.chunk_id for hit in hits] == ["a", "b"]  # id tie-break


def test_save_load_roundtrip(tmp_path):
    index = build_index()
    path = tmp_path / "bm25.json"
    index.save(path)
    loaded = Bm25Index.load(path)
    assert loaded.ids == index.ids
    assert loaded.search("training", 2) == index.search("training", 2)


def test_tokenizer_version_mismatch_rejected(tmp_path):
    index = build_index()
    path = tmp_path / "bm25.json"
    index.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tokenizer_version"] = "other.v9"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        Bm25Index.load(path)


def test_allowed_ids_filter_and_empty_query():
    index = build_index()
    hits = index.search("brown fox", 5, allowed_ids={"c3"})
    assert hits == []
    assert index.search("", 5) == []
    assert index.search("zzz-not-present", 5) == []
