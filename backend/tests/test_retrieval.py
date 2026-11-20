
import pytest
from sqlalchemy import select

from app.core.errors import DomainError
from app.persistence.models import Chunk, Document, IndexGeneration  # noqa: F401
from app.rag.retriever import retrieve
from app.rag.vector_store import VectorUnavailable
from tests.knowledge_helpers import make_repo, upload_and_index

AUTH_DOC = """# 认证设计

## 登录流程

用户通过 OAuth 设备码完成登录，令牌保存在服务端会话中。

## 权限模型

角色分为 viewer、analyst 与 maintainer，写入操作需要 maintainer。
"""

DEPLOY_DOC = """# 部署说明

## 发布流程

发布通过 CI 流水线构建镜像并推送到私有仓库。

## 回滚

回滚使用上一个稳定版本的镜像标签。
"""


@pytest.fixture
def repo_id(database, settings):
    return make_repo(database)


def test_bm25_retrieval_end_to_end(database, settings, repo_id):
    upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    upload_and_index(database, settings, repo_id, "deploy.md", DEPLOY_DOC)

    with database() as db:
        results, trace = retrieve(db, settings, repo_id=repo_id, query="权限模型 maintainer")
    assert results, trace.as_dict()
    assert "auth.md" in results[0].source_key
    assert results[0].heading_path == ["认证设计", "权限模型"]
    assert trace.generation_id is not None
    assert trace.channels["bm25"]["enabled"] is True
    assert trace.channels["vector"] == {"enabled": False, "reason": "not_configured"}
    assert trace.channels["rerank"] == {"enabled": False, "reason": "not_configured"}
    assert trace.degraded == []  # not-configured channels are a valid mode, not degradation
    assert trace.selected and trace.selected[0]["chunk_id"] == str(results[0].chunk_id)


class FakeReranker:
    def __init__(self, pick: int):
        self.pick = pick
        self.calls: list[int] = []

    def rerank(self, query, documents, top_n):
        self.calls.append(len(documents))
        return [(self.pick, 0.99)]


class BrokenReranker:
    def rerank(self, query, documents, top_n):
        raise RuntimeError("rerank unavailable")


def test_rerank_reorders_results_and_records_trace(database, settings, repo_id):
    upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    upload_and_index(database, settings, repo_id, "deploy.md", DEPLOY_DOC)
    settings.rerank_base_url = "http://localhost:9"
    settings.rerank_model = "fake"

    with database() as db:
        baseline, _ = retrieve(db, settings, repo_id=repo_id, query="权限发布 maintainer", final_k=2)
    fake = FakeReranker(pick=1)
    with database() as db:
        results, trace = retrieve(
            db,
            settings,
            repo_id=repo_id,
            query="权限发布 maintainer",
            final_k=1,
            rerank_factory=lambda _settings: fake,
        )
    assert trace.reranked is True
    assert trace.channels["rerank"]["enabled"] is True
    assert trace.degraded == []
    assert fake.calls and fake.calls[0] >= 2
    assert len(results) == 1 and results[0].chunk_id == baseline[1].chunk_id
    assert "rerank" in results[0].scores


def test_rerank_failure_degrades_to_rrf(database, settings, repo_id):
    upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    settings.rerank_base_url = "http://localhost:9"
    settings.rerank_model = "fake"
    with database() as db:
        results, trace = retrieve(
            db,
            settings,
            repo_id=repo_id,
            query="权限模型",
            rerank_factory=lambda _settings: BrokenReranker(),
        )
    assert results
    assert trace.reranked is False
    assert "rerank_unavailable" in trace.degraded
    assert trace.channels["rerank"] == {"enabled": False, "reason": "unavailable"}


def test_chinese_and_identifier_queries(database, settings, repo_id):
    upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    with database() as db:
        by_chinese, _ = retrieve(db, settings, repo_id=repo_id, query="权限")
        by_identifier, _ = retrieve(db, settings, repo_id=repo_id, query="maintainer 写入")
    assert by_chinese and "权限" in by_chinese[0].text
    assert by_identifier and "maintainer" in by_identifier[0].text


def test_empty_recall_and_no_index(database, settings):
    repo_id = make_repo(database, "octo/empty")
    with database() as db:
        results, trace = retrieve(db, settings, repo_id=repo_id, query="anything")
    assert results == [] and trace.degraded == ["no_index"]

    upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    with database() as db:
        results, trace = retrieve(db, settings, repo_id=repo_id, query="完全不相关的词组xyz")
    assert results == [] and trace.fused == 0 and trace.degraded == []


def test_deleted_document_filtered_with_refill(database, settings, repo_id):
    from app.services import knowledge_service

    auth_id, _ = upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    upload_and_index(database, settings, repo_id, "deploy.md", DEPLOY_DOC)

    with database() as db:
        results, _ = retrieve(db, settings, repo_id=repo_id, query="权限 maintainer")
        assert results and "auth.md" in results[0].source_key

        document = db.get(Document, auth_id)
        knowledge_service.delete_document(db, settings, document, expected_version=document.version)

    with database() as db:
        results, trace = retrieve(db, settings, repo_id=repo_id, query="权限 maintainer")
        assert all("auth.md" not in item.source_key for item in results)
        assert trace.dropped_by_filtering  # membership still exists, DB check-back drops it


class FakeVectorStore:
    def __init__(self, ordered_ids: list[str]):
        self.ordered_ids = ordered_ids
        self.deleted: list[str] = []

    def search(self, vector, *, generation_id, k):
        return [(chunk_id, 1.0 - index * 0.1) for index, chunk_id in enumerate(self.ordered_ids[:k])]

    def upsert(self, rows):
        return len(rows)

    def delete_generation(self, generation_id):
        self.deleted.append(generation_id)


class FakeEmbeddings:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_vector_channel_fusion_with_fake_store(database, settings, repo_id):
    settings.embedding_base_url = "http://localhost:9/v1"
    settings.embedding_model = "fake"
    settings.milvus_uri = "http://localhost:9"
    document_id, _ = upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    upload_and_index(database, settings, repo_id, "deploy.md", DEPLOY_DOC)

    with database() as db:
        chunks = db.scalars(select(Chunk).order_by(Chunk.ordinal)).all()
        chunk_b = str(chunks[1].id)
        chunk_a = str(chunks[0].id)
        results, trace = retrieve(
            db,
            settings,
            repo_id=repo_id,
            query="权限模型 maintainer",
            vector_store_factory=lambda s: FakeVectorStore([chunk_b, chunk_a]),
            embedding_factory=lambda s: FakeEmbeddings(),
        )
    assert trace.channels["vector"]["enabled"] is True
    ids = [str(item.chunk_id) for item in results]
    assert chunk_b in ids and chunk_a in ids
    # Opposite ranks (1, 2) and (2, 1) have equal RRF scores. UUID tie-breaking
    # must not make this integration test depend on randomly generated IDs.
    bm25_ranks = {hit["chunk_id"]: rank for rank, hit in enumerate(trace.channels["bm25"]["top"], 1)}
    expected = {}
    for rank, chunk_id in enumerate([chunk_b, chunk_a], 1):
        expected[chunk_id] = 1 / (60 + rank)
        if chunk_id in bm25_ranks:
            expected[chunk_id] += 1 / (60 + bm25_ranks[chunk_id])
    assert [item for item in ids if item in expected] == sorted(expected, key=lambda cid: (-expected[cid], cid))


def test_single_channel_degradation(database, settings, repo_id):
    settings.embedding_base_url = "http://localhost:9/v1"
    settings.embedding_model = "fake"
    settings.milvus_uri = "http://localhost:9"
    document_id, _ = upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)

    def broken_vector(settings_):
        raise VectorUnavailable("test outage")

    with database() as db:
        results, trace = retrieve(
            db, settings, repo_id=repo_id, query="权限",
            vector_store_factory=broken_vector, embedding_factory=lambda s: FakeEmbeddings(),
        )
    assert results
    assert "vector_unavailable" in trace.degraded

    def broken_bm25(repo_id_, generation):
        raise RuntimeError("bm25 file gone")

    with database() as db:
        chunks = db.scalars(select(Chunk)).all()
        results, trace = retrieve(
            db, settings, repo_id=repo_id, query="权限",
            bm25_loader=broken_bm25,
            vector_store_factory=lambda s: FakeVectorStore([str(chunks[0].id)]),
            embedding_factory=lambda s: FakeEmbeddings(),
        )
    assert results and "bm25_unavailable" in trace.degraded

    with database() as db:
        with pytest.raises(DomainError) as caught:
            retrieve(
                db, settings, repo_id=repo_id, query="权限",
                bm25_loader=broken_bm25, vector_store_factory=broken_vector,
                embedding_factory=lambda s: FakeEmbeddings(),
            )
    assert caught.value.code == "RETRIEVAL_UNAVAILABLE"


def test_source_type_filter(database, settings, repo_id):
    upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    with database() as db:
        results, _ = retrieve(
            db, settings, repo_id=repo_id, query="权限", source_types=["generated"]
        )
    assert results == []
    with database() as db:
        results, _ = retrieve(
            db, settings, repo_id=repo_id, query="权限", source_types=["source"]
        )
    assert results


def test_missing_bm25_without_vector_is_unavailable(database, settings, repo_id):
    upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    with database() as db:
        generation = db.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
        index_path = settings.storage_root / "indexes" / str(repo_id) / str(generation.id) / "bm25.json"
        index_path.unlink()
        with pytest.raises(DomainError) as caught:
            retrieve(db, settings, repo_id=repo_id, query="权限")
    assert caught.value.code == "RETRIEVAL_UNAVAILABLE"
    assert caught.value.status == 503


def test_empty_healthy_vector_remains_a_valid_result(database, settings, repo_id):
    upload_and_index(database, settings, repo_id, "auth.md", AUTH_DOC)
    settings.embedding_base_url = "http://localhost:9/v1"
    settings.embedding_model = "fake"
    settings.milvus_uri = "http://localhost:9"

    def broken_bm25(*_):
        raise FileNotFoundError("index missing")

    with database() as db:
        results, trace = retrieve(
            db, settings, repo_id=repo_id, query="没有相关内容",
            bm25_loader=broken_bm25,
            vector_store_factory=lambda _: FakeVectorStore([]),
            embedding_factory=lambda _: FakeEmbeddings(),
        )
    assert results == []
    assert trace.channels["vector"]["enabled"] is True
    assert trace.degraded == ["bm25_unavailable"]
