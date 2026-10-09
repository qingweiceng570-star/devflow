"""M3 knowledge base: documents, chunks, index generations and outbox."""

import sqlalchemy as sa
from alembic import op

revision = "0004_m3"
down_revision = "0003_m2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_key", sa.String(300), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.String(120), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_id", "source_key", name="uq_documents_repo_source"),
    )
    op.create_index("ix_documents_repo_id", "documents", ["repo_id"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.String(120), nullable=True),
        sa.Column("parser_version", sa.String(30), nullable=False),
        sa.Column("object_ref", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "document_id", "content_hash", "parser_version", name="uq_document_versions_hash"
        ),
        sa.UniqueConstraint("repo_id", "id", name="uq_document_versions_repo_id"),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            sa.Uuid(),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_version_id", "ordinal", name="uq_chunks_version_ordinal"),
    )
    op.create_index("ix_chunks_repo_version", "chunks", ["repo_id", "document_version_id"])

    op.create_table(
        "index_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("embedding_config", sa.JSON(), nullable=False),
        sa.Column("bm25_manifest", sa.JSON(), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_index_generations_repo_id", "index_generations", ["repo_id"])
    op.create_index(
        "uq_index_generations_active_repo",
        "index_generations",
        ["repo_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "index_memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "generation_id",
            sa.Uuid(),
            sa.ForeignKey("index_generations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("generation_id", "chunk_id", name="uq_index_memberships_generation_chunk"),
    )
    op.create_index("ix_index_memberships_generation_id", "index_memberships", ["generation_id"])
    op.create_index("ix_index_memberships_chunk_id", "index_memberships", ["chunk_id"])

    op.create_table(
        "index_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_index_outbox_status_created", "index_outbox", ["status", "created_at"])


def downgrade():
    op.drop_table("index_outbox")
    op.drop_table("index_memberships")
    op.drop_table("index_generations")
    op.drop_table("chunks")
    op.drop_table("document_versions")
    op.drop_table("documents")
