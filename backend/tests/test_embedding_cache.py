import json
from uuid import uuid4

import pytest

from app.rag.embedding_cache import EmbeddingCache


def test_cache_reuses_exact_text_and_invalidates_on_endpoint_change(settings):
    settings.embedding_dimension = 64
    settings.embedding_base_url = "https://example.com/v1"
    settings.embedding_model = "test"
    repo_id = uuid4()
    calls = []

    class Embedder:
        def embed(self, texts):
            calls.append(texts)
            return [[0.1] * 64 for _ in texts]

    cache = EmbeddingCache(settings, repo_id)
    vectors, reused, created = cache.embed(["alpha", "alpha", "beta"], Embedder())
    assert calls == [["alpha", "beta"]]
    assert len(vectors) == 3 and created == 2
    cache.save()
    cached = EmbeddingCache(settings, repo_id)
    cached.embed(["beta"], Embedder())
    cached.save()
    assert len(calls) == 1
    assert len(json.loads(cached.path.read_text())["vectors"]) == 1
    settings.embedding_base_url = "https://other.example.com/v1"
    EmbeddingCache(settings, repo_id).embed(["beta"], Embedder())
    assert len(calls) == 2
    EmbeddingCache(settings, uuid4()).embed(["beta"], Embedder())
    assert len(calls) == 3  # No cache sharing across repositories.


@pytest.mark.parametrize("response", [[], [[float("nan")] * 64], [[0.1] * 63]])
def test_invalid_vectors_are_not_cached(settings, response):
    settings.embedding_dimension = 64
    cache = EmbeddingCache(settings, uuid4())

    class BrokenEmbedder:
        def embed(self, texts):
            return response

    with pytest.raises(ValueError):
        cache.embed(["alpha"], BrokenEmbedder())
    assert cache.vectors == {}
