import pytest

from app.rag.bm25_store import Bm25Index
from app.rag.index_cache import Bm25Cache


def test_cache_reuses_generation_but_detects_replacement_and_deletion(tmp_path):
    path = tmp_path / "index.json"
    Bm25Index.build([("a", "alpha")]).save(path)
    cache = Bm25Cache()
    first = cache.load(path)
    assert cache.load(path) is first
    Bm25Index.build([("b", "beta replacement with a different size")]).save(path)
    assert cache.load(path).search("beta", 1)[0].chunk_id == "b"
    path.unlink()
    with pytest.raises(FileNotFoundError):
        cache.load(path)


def test_cache_is_bounded_by_entries_and_size(tmp_path):
    paths = [tmp_path / f"{i}.json" for i in range(3)]
    for path in paths:
        Bm25Index.build([(path.name, "alpha")]).save(path)
    cache = Bm25Cache(max_entries=2)
    first = cache.load(paths[0])
    cache.load(paths[1])
    cache.load(paths[2])
    assert cache.load(paths[0]) is not first
    tiny = Bm25Cache(max_bytes=1)
    assert tiny.load(paths[0]) is not tiny.load(paths[0])


def test_corrupt_file_does_not_fall_back_to_cached_content(tmp_path):
    path = tmp_path / "index.json"
    Bm25Index.build([("a", "alpha")]).save(path)
    cache = Bm25Cache()
    cache.load(path)
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        cache.load(path)
