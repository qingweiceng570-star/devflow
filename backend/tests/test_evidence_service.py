from uuid import uuid4

from sqlalchemy import func, select

from app.persistence.models import Evidence, SourceSnapshot
from app.services.evidence_service import capture, ensure_snapshot
from app.storage.local import put_object, read_object, text_excerpt


def test_object_storage_roundtrip_and_dedupe(settings):
    repo_id = uuid4()
    ref1, digest1 = put_object(settings.storage_root, repo_id, "bodies", b"hello")
    ref2, digest2 = put_object(settings.storage_root, repo_id, "bodies", b"hello")
    assert ref1 == ref2 and digest1 == digest2
    assert read_object(settings.storage_root, ref1) == b"hello"
    other, _ = put_object(settings.storage_root, repo_id, "bodies", b"world")
    assert other != ref1
    (settings.storage_root / "objects").exists()


def test_read_object_rejects_bad_refs(settings):
    import pytest

    from app.core.errors import DomainError

    for bad in ["objects/../etc/passwd", "objects/x/y", "/absolute", "objects/a/b/c"]:
        with pytest.raises(DomainError):
            read_object(settings.storage_root, bad)


def test_text_excerpt_truncates():
    assert text_excerpt(None) == ("", False)
    assert text_excerpt("  short  ") == ("short", False)
    excerpt, truncated = text_excerpt("x" * 100, limit=10)
    assert excerpt == "x" * 10 and truncated


def test_capture_is_immutable_and_readable(settings, database):
    repo_id = uuid4()
    with database() as db:
        row = capture(
            db,
            settings,
            repo_id=repo_id,
            source_type="issue",
            content="line one\nline two\n" + ("y" * 600),
            locator={"issue_number": 5},
            source_url="https://github.com/octo/demo/issues/5",
        )
        db.commit()
        assert row.truncated and len(row.excerpt) == 500
        assert row.locator["issue_number"] == 5
        object_ref = row.locator["object_ref"]
        stored = read_object(settings.storage_root, object_ref)
        import hashlib

        assert hashlib.sha256(stored).hexdigest() == row.content_hash

        again = capture(
            db,
            settings,
            repo_id=repo_id,
            source_type="issue",
            content="changed",
            locator={"issue_number": 5},
        )
        db.commit()
        assert again.id != row.id  # a changed source never rewrites history
        assert db.scalar(select(func.count()).select_from(Evidence)) == 2


def test_ensure_snapshot_dedupes_same_revision(settings, database):
    repo_id = uuid4()
    with database() as db:
        first = ensure_snapshot(
            db,
            settings,
            repo_id=repo_id,
            source_type="issue",
            external_id="1001",
            content='{"a": 1}',
            revision="2026-01-01T00:00:00Z",
        )
        db.commit()
        same = ensure_snapshot(
            db,
            settings,
            repo_id=repo_id,
            source_type="issue",
            external_id="1001",
            content='{"a": 1}',
            revision="2026-01-01T00:00:00Z",
        )
        db.commit()
        assert same.id == first.id
        assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 1

        changed = ensure_snapshot(
            db,
            settings,
            repo_id=repo_id,
            source_type="issue",
            external_id="1001",
            content='{"a": 2}',
            revision="2026-01-02T00:00:00Z",
        )
        db.commit()
        assert changed.id != first.id
        assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 2
