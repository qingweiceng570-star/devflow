
import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.persistence.models import IndexGeneration, IndexMembership
from app.rag import index_manager
from app.rag.retriever import index_dir
from tests.knowledge_helpers import make_repo, run_index_job, upload, upload_and_index

DOC_A = "# A\n\nAlpha 认证设计文档，包含登录与权限说明。\n"
DOC_B = "# B\n\nBeta 部署文档，包含发布与回滚说明。\n"


def test_build_activate_and_supersede(database, settings):
    repo_id = make_repo(database)
    upload_and_index(database, settings, repo_id, "a.md", DOC_A)
    with database() as db:
        generations = db.scalars(select(IndexGeneration)).all()
        assert len(generations) == 1 and generations[0].status == "active"
        first_id = generations[0].id
        memberships = db.scalar(select(func.count()).select_from(IndexMembership))
        assert memberships == generations[0].chunk_count
        assert (index_dir(settings, repo_id, first_id) / "bm25.json").exists()

    upload_and_index(database, settings, repo_id, "b.md", DOC_B)
    with database() as db:
        rows = {row.id: row for row in db.scalars(select(IndexGeneration))}
        assert len(rows) == 2
        active = [row for row in rows.values() if row.status == "active"]
        assert len(active) == 1 and active[0].id != first_id
        assert rows[first_id].status == "superseded"
        assert active[0].chunk_count > rows[first_id].chunk_count


def test_failed_build_keeps_old_generation_active(database, settings, monkeypatch):
    repo_id = make_repo(database)
    upload_and_index(database, settings, repo_id, "a.md", DOC_A)
    with database() as db:
        active_id = db.scalar(
            select(IndexGeneration.id).where(IndexGeneration.status == "active")
        )

    def broken_build(documents):
        raise RuntimeError("disk full")

    monkeypatch.setattr("app.rag.bm25_store.Bm25Index.build", staticmethod(broken_build))
    upload(database, settings, repo_id, "b.md", DOC_B.encode())

    from app.jobs.handlers.index import handle_index
    from app.jobs.queue import claim_next

    with database() as db:
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        with pytest.raises(RuntimeError):
            handle_index(database, settings, job, "w1")

    with database() as db:
        still_active = db.scalar(
            select(IndexGeneration).where(IndexGeneration.status == "active")
        )
        assert still_active.id == active_id  # previous generation untouched
        failed = db.scalars(
            select(IndexGeneration).where(IndexGeneration.status == "failed")
        ).all()
        assert failed and failed[-1].error["code"] == "BM25_BUILD_FAILED"


def test_chunk_limit_refuses_build(database, settings, monkeypatch):
    repo_id = make_repo(database)
    settings.bm25_max_chunks_per_repo = 100
    monkeypatch.setattr(
        index_manager,
        "_current_chunks",
        lambda db, repo_id_: [object()] * 101,
    )
    with database() as db:
        with pytest.raises(DomainError) as caught:
            index_manager.build_and_activate(db, settings, repo_id)
    assert caught.value.code == "INDEX_LIMIT_EXCEEDED"


def test_generation_gc_keeps_two(database, settings):
    repo_id = make_repo(database)
    upload_and_index(database, settings, repo_id, "a.md", DOC_A)
    with database() as db:
        first_id = db.scalar(select(IndexGeneration.id))
    upload_and_index(database, settings, repo_id, "b.md", DOC_B)
    upload_and_index(database, settings, repo_id, "c.md", "# C\n\ngamma 内容\n")

    with database() as db:
        rows = db.scalars(select(IndexGeneration)).all()
        statuses = sorted(row.status for row in rows)
        assert statuses == ["active", "superseded"]
        assert all(row.id != first_id for row in rows)  # oldest collected
    assert not index_dir(settings, repo_id, first_id).exists()


def test_outbox_processed_only_on_success(database, settings):
    from app.persistence.models import IndexOutbox

    repo_id = make_repo(database)
    upload_and_index(database, settings, repo_id, "a.md", DOC_A)
    with database() as db:
        rows = db.scalars(select(IndexOutbox)).all()
        assert rows and all(row.status == "processed" for row in rows)

    # A second upload arriving while a build runs stays pending until processed.
    upload(database, settings, repo_id, "b.md", DOC_B.encode())
    with database() as db:
        pending = db.scalars(
            select(IndexOutbox).where(IndexOutbox.status == "pending")
        ).all()
        assert len(pending) == 1
    result = run_index_job(database, settings, repo_id)
    assert result["status"] == "activated"
    with database() as db:
        remaining = db.scalars(
            select(IndexOutbox).where(IndexOutbox.status == "pending")
        ).all()
        assert remaining == []


def test_older_pending_rows_are_picked_from_prior_failures(database, settings, monkeypatch):
    """A failed build leaves rows pending; a later run processes them."""
    from app.jobs.handlers.index import handle_index
    from app.jobs.queue import claim_next

    repo_id = make_repo(database)
    upload(database, settings, repo_id, "a.md", DOC_A.encode())

    def broken_build(documents):
        raise RuntimeError("temporary")

    monkeypatch.setattr("app.rag.bm25_store.Bm25Index.build", staticmethod(broken_build))
    with database() as db:
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        with pytest.raises(RuntimeError):
            handle_index(database, settings, job, "w1")
        # Failed job in production is closed by the worker; emulate:
        job.status = "failed"
        db.commit()

    monkeypatch.undo()
    # A later upload re-enqueues; all pending rows (including the old one) build.
    upload(database, settings, repo_id, "b.md", DOC_B.encode())
    result = run_index_job(database, settings, repo_id)
    assert result["status"] == "activated"
    with database() as db:
        from app.persistence.models import Document, IndexOutbox

        assert (
            db.scalar(
                select(func.count()).select_from(IndexOutbox).where(IndexOutbox.status == "pending")
            )
            == 0
        )
        assert db.scalar(select(func.count()).select_from(Document)) == 2
