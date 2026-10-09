"""M1 repositories, sync, jobs, workspace evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0002_m1"
down_revision = "0001_m0"
branch_labels = None
depends_on = None

ACTIVE_JOB_STATUSES = "status IN ('queued', 'retry_wait', 'leased')"


def upgrade():
    op.create_table(
        "repositories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("full_name", sa.String(300), nullable=False),
        sa.Column("credential_ref", sa.String(80), nullable=False),
        sa.Column("default_branch", sa.String(200), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "external_id", name="uq_repositories_provider_external"),
    )
    op.create_index("ix_repositories_full_name", "repositories", ["full_name"])

    op.create_table(
        "repo_memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "repo_id", name="uq_repo_memberships_user_repo"),
    )
    op.create_index("ix_repo_memberships_user_id", "repo_memberships", ["user_id"])
    op.create_index("ix_repo_memberships_repo_id", "repo_memberships", ["repo_id"])

    op.create_table(
        "issues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("body_ref", sa.String(300), nullable=True),
        sa.Column("author_login", sa.String(120), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("comments_count", sa.Integer(), nullable=False),
        sa.Column("html_url", sa.String(500), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_id", "number", name="uq_issues_repo_number"),
        sa.UniqueConstraint("repo_id", "external_id", name="uq_issues_repo_external"),
    )

    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("merged", sa.Boolean(), nullable=False),
        sa.Column("draft", sa.Boolean(), nullable=False),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("body_ref", sa.String(300), nullable=True),
        sa.Column("author_login", sa.String(120), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("base_ref", sa.String(200), nullable=True),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("head_ref", sa.String(200), nullable=True),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("html_url", sa.String(500), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_id", "number", name="uq_pull_requests_repo_number"),
        sa.UniqueConstraint("repo_id", "external_id", name="uq_pull_requests_repo_external"),
        sa.UniqueConstraint("repo_id", "id", name="uq_pull_requests_repo_id"),
    )

    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pr_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=True),
        sa.Column("author_login", sa.String(120), nullable=True),
        sa.Column("body_ref", sa.String(300), nullable=True),
        sa.Column("html_url", sa.String(500), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_id", "external_id", name="uq_reviews_repo_external"),
        sa.ForeignKeyConstraint(
            ["repo_id", "pr_id"],
            ["pull_requests.repo_id", "pull_requests.id"],
            name="fk_reviews_pull_request",
        ),
    )

    op.create_table(
        "ci_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("workflow_name", sa.String(200), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("head_branch", sa.String(200), nullable=True),
        sa.Column("event", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("conclusion", sa.String(20), nullable=True),
        sa.Column("html_url", sa.String(500), nullable=True),
        sa.Column("run_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "repo_id", "external_id", "attempt", name="uq_ci_runs_repo_external_attempt"
        ),
        sa.UniqueConstraint("repo_id", "id", name="uq_ci_runs_repo_id"),
    )
    op.create_index("ix_ci_runs_head_sha", "ci_runs", ["head_sha"])

    op.create_table(
        "ci_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ci_run_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("conclusion", sa.String(20), nullable=True),
        sa.Column("html_url", sa.String(500), nullable=True),
        sa.Column("logs_ref", sa.String(300), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_id", "external_id", name="uq_ci_jobs_repo_external"),
        sa.ForeignKeyConstraint(
            ["repo_id", "ci_run_id"],
            ["ci_runs.repo_id", "ci_runs.id"],
            name="fk_ci_jobs_ci_run",
        ),
    )

    op.create_table(
        "activity_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_event_id", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("actor_login", sa.String(120), nullable=True),
        sa.Column("subject_type", sa.String(20), nullable=True),
        sa.Column("subject_number", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_ref", sa.String(300), nullable=True),
        sa.Column("evidence_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_id", "source_event_id", name="uq_activity_events_repo_source"),
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resources", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sync_runs_repo_id", "sync_runs", ["repo_id"])

    op.create_table(
        "sync_cursors",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource", sa.String(30), nullable=False),
        sa.Column("committed_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etag", sa.String(200), nullable=True),
        sa.Column("coverage", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_id", "resource", name="uq_sync_cursors_repo_resource"),
    )
    op.create_index("ix_sync_cursors_repo_id", "sync_cursors", ["repo_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_jobs_status_available", "jobs", ["status", "available_at"])
    op.create_index(
        "uq_jobs_active_entity",
        "jobs",
        ["kind", "entity_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_JOB_STATUSES),
        sqlite_where=sa.text(ACTIVE_JOB_STATUSES),
    )

    op.create_table(
        "resource_leases",
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("resource_kind", sa.String(30), primary_key=True),
        sa.Column("resource_key", sa.String(120), primary_key=True),
        sa.Column("owner", sa.String(120), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_resource_leases_expires_at", "resource_leases", ["expires_at"])

    op.create_table(
        "source_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("revision", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_ref", sa.String(300), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_source_snapshots_repo_type_external",
        "source_snapshots",
        ["repo_id", "source_type", "external_id"],
    )
    op.create_index(
        "ix_source_snapshots_repo_hash", "source_snapshots", ["repo_id", "content_hash"]
    )
    op.create_index(
        "ix_source_snapshots_repo_id_id", "source_snapshots", ["repo_id", "id"], unique=True
    )

    op.create_table(
        "evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_url", sa.String(500), nullable=True),
        sa.Column("locator", sa.JSON(), nullable=False),
        sa.Column("excerpt", sa.String(2000), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id", "snapshot_id"],
            ["source_snapshots.repo_id", "source_snapshots.id"],
            name="fk_evidence_snapshot",
        ),
    )
    op.create_index("ix_evidence_repo_created", "evidence", ["repo_id", "created_at"])

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("route", sa.String(120), nullable=False),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_idempotency_actor_route_key",
        "idempotency_records",
        ["actor_id", "route", "key"],
        unique=True,
    )
    op.create_index("ix_idempotency_expires", "idempotency_records", ["expires_at"])


def downgrade():
    op.drop_table("idempotency_records")
    op.drop_table("evidence")
    op.drop_table("source_snapshots")
    op.drop_table("resource_leases")
    op.drop_table("jobs")
    op.drop_table("sync_cursors")
    op.drop_table("sync_runs")
    op.drop_table("activity_events")
    op.drop_table("ci_jobs")
    op.drop_table("ci_runs")
    op.drop_table("reviews")
    op.drop_table("pull_requests")
    op.drop_table("issues")
    op.drop_table("repo_memberships")
    op.drop_table("repositories")
