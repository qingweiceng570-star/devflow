"""The only agent-visible mutation creates a local, unapproved draft."""

import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.core.errors import DomainError
from app.persistence.models import ActionDraft, RepoMembership
from app.services.action_service import ActionInput, create_draft
from app.tools.executor import ToolSpec, ok_result


def draft_spec(execution):
    async def prepare(deps, args):
        execution.check_stop()
        with deps.db_factory() as db:
            membership = db.scalar(
                select(RepoMembership).where(
                    RepoMembership.repo_id == deps.repo.id,
                    RepoMembership.user_id == deps.ctx.actor_id,
                )
            )
            if not membership or membership.role not in {"analyst", "maintainer", "admin"}:
                raise DomainError("FORBIDDEN", "无权创建本仓库草稿", 403)
            if args.number == deps.ctx.pr_number and deps.ctx.head_sha:
                args = args.model_copy(update={"expected_head_sha": deps.ctx.head_sha})
            logical_id = uuid5(
                NAMESPACE_URL, str(deps.ctx.run_id) + json.dumps(args.model_dump(), sort_keys=True)
            )
            row = db.get(ActionDraft, logical_id)
            if row is None:
                row = create_draft(
                    db,
                    deps.settings,
                    deps.repo,
                    deps.ctx.actor_id,
                    args,
                    client=deps.github_read.client,
                    draft_id=logical_id,
                )
            execution.draft_ids.add(row.id)
            return ok_result(
                {
                    "draft_id": str(row.id),
                    "state": row.state,
                    "version": row.version,
                    "payload_hash": row.payload_hash,
                    "payload": row.payload,
                    "note": "仅本地草稿；必须由用户审阅批准并执行后才会写入 GitHub。",
                },
                source="local_draft",
            )

    return ToolSpec(
        "create_action_draft",
        "根据用户要求准备本地操作草稿，不发送任何 GitHub 写请求。必须等待用户在审批页批准并执行。",
        ActionInput,
        prepare,
    )
