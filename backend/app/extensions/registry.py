import json
from typing import Literal

from jsonschema import Draft202012Validator, ValidationError
from pydantic import Field
from sqlalchemy import select

from app.core.errors import DomainError
from app.domain.contracts import Contract
from app.extensions.mcp import MCPClient, check_schema, schema_hash
from app.extensions.skills import Skill, execute_skill
from app.persistence.models import ExtensionConfig
from app.services.memory_service import audit
from app.tools.executor import ToolExecutor, ToolSpec, ok_result


class InstallExtension(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    version: int = Field(ge=1)
    kind: Literal["mcp", "skill"]
    content: dict


class MCPConfig(Contract):
    endpoint: str = Field(max_length=1000)
    # Administrator attests these exact tools are read-only and repository-scoped.
    allowed_tools: list[str] = Field(min_length=1, max_length=20)
    repo_argument: str = Field(default="repository", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,39}$")
    reviewed_read_only: Literal[True]


class ExtensionArguments(Contract):
    arguments: dict = Field(default_factory=dict)


def install(db, settings, repo, actor_id, body, *, mcp_factory=MCPClient):
    from app.jobs.handlers.analyze import KNOWLEDGE_QA_TOOLS, PR_REVIEW_TOOLS

    if len(json.dumps(body.content).encode()) > 65536:
        raise DomainError("EXTENSION_TOO_LARGE", "扩展配置超过 64 KiB", 413)

    if db.scalar(
        select(ExtensionConfig).where(
            ExtensionConfig.repo_id == repo.id,
            ExtensionConfig.name == body.name,
            ExtensionConfig.version == body.version,
        )
    ):
        raise DomainError("VERSION_CONFLICT", "扩展版本已存在，请使用新版本号", 409)
    if body.kind == "mcp":
        config = MCPConfig.model_validate(body.content)
        client = mcp_factory(settings, config.endpoint)
        try:
            client.initialize()
            offered = {t["name"]: t for t in client.tools()}
        finally:
            client.close()
        if not set(config.allowed_tools) <= set(offered):
            raise DomainError("MCP_TOOL_MISSING", "白名单工具不在服务清单中", 400)
        manifest = {}
        for name in config.allowed_tools:
            tool = offered[name]
            check_schema(tool["inputSchema"])
            if config.repo_argument not in tool["inputSchema"].get("properties", {}):
                raise DomainError("MCP_SCOPE_MISSING", "工具必须接受服务器注入的仓库参数", 400)
            manifest[name] = tool
        content = {**config.model_dump(), "manifest": manifest}
    else:
        skill = Skill.model_validate(body.content)
        allowed = {s.name for s in KNOWLEDGE_QA_TOOLS + PR_REVIEW_TOOLS}
        for extension in db.scalars(
            select(ExtensionConfig).where(
                ExtensionConfig.repo_id == repo.id,
                ExtensionConfig.kind == "mcp",
                ExtensionConfig.state == "active",
            )
        ):
            allowed.update(
                f"mcp_{extension.name}_{i}"
                for i, _ in enumerate(extension.content["allowed_tools"])
            )
        if not set(skill.allowed_tools) <= allowed:
            raise DomainError("SKILL_TOOL_DENIED", "Skill 含未安装的工具", 400)
        content = skill.model_dump(mode="json")
    row = ExtensionConfig(
        repo_id=repo.id,
        name=body.name,
        version=body.version,
        kind=body.kind,
        schema_hash=schema_hash(content),
        content=content,
    )
    for old in db.scalars(
        select(ExtensionConfig).where(
            ExtensionConfig.repo_id == repo.id,
            ExtensionConfig.name == body.name,
            ExtensionConfig.state == "active",
        )
    ):
        old.state = "superseded"
    db.add(row)
    db.flush()
    audit(
        db,
        repo.id,
        actor_id,
        row.id,
        "extension.installed",
        schema_hash=row.schema_hash,
        version=row.version,
    )
    db.commit()
    return row


def build_specs(execution, base_specs):
    with execution.db_factory() as db:
        from uuid import UUID

        pinned = execution.run.config_snapshot.get("extensions")
        selection = (
            ExtensionConfig.id.in_([UUID(value) for value in pinned])
            if pinned is not None
            else ExtensionConfig.state == "active"
        )
        rows = db.scalars(
            select(ExtensionConfig)
            .where(ExtensionConfig.repo_id == execution.repo.id, selection)
            .order_by(ExtensionConfig.name)
            .limit(20)
        ).all()
    specs = []
    for row in rows:
        if row.kind == "mcp":
            for index, tool in enumerate(row.content["allowed_tools"]):

                def bind(extension, tool_name):
                    async def handler(deps, args):
                        execution.check_stop()
                        with deps.db_factory() as db:
                            current = db.get(ExtensionConfig, extension.id)
                            if current.state not in {"active", "superseded"}:
                                raise DomainError("EXTENSION_DISABLED", "扩展已停用", 409)
                        content = extension.content
                        fixed = content["repo_argument"]
                        if fixed in args.arguments or any(
                            k in args.arguments for k in ("repo_id", "actor_id", "credential_ref")
                        ):
                            raise DomainError("SCOPE_VIOLATION", "仓库参数只能由服务端注入", 403)
                        arguments = {**args.arguments, fixed: deps.repo.full_name}
                        try:
                            Draft202012Validator(
                                content["manifest"][tool_name]["inputSchema"]
                            ).validate(arguments)
                        except ValidationError:
                            raise DomainError(
                                "INVALID_TOOL_ARGUMENTS", "MCP 参数不符合固定 Schema", 400
                            ) from None
                        client = MCPClient(deps.settings, content["endpoint"])
                        try:
                            client.initialize()
                            tools = {t["name"]: t for t in client.tools()}
                            if schema_hash(tools.get(tool_name)) != schema_hash(
                                content["manifest"][tool_name]
                            ):
                                with deps.db_factory() as db:
                                    db.get(ExtensionConfig, extension.id).state = "schema_changed"
                                    db.commit()
                                raise DomainError(
                                    "MCP_SCHEMA_CHANGED", "远端工具定义改变，请重新审查安装", 409
                                )
                            execution.check_stop()
                            result = client.rpc(
                                "tools/call", {"name": tool_name, "arguments": arguments}
                            )
                        finally:
                            client.close()
                        if result.get("isError"):
                            raise DomainError("MCP_TOOL_FAILED", "MCP 工具执行失败", 502)
                        eid = deps.evidence(
                            source_type="mcp",
                            content=json.dumps(result, ensure_ascii=False),
                            locator={
                                "extension_id": str(extension.id),
                                "schema_hash": extension.schema_hash,
                                "tool": tool_name,
                            },
                        )
                        return ok_result(
                            result, source="mcp", evidence_ids=[eid], freshness="unknown"
                        )

                    return handler

                specs.append(
                    ToolSpec(
                        f"mcp_{row.name}_{index}",
                        f"已审查只读 MCP {row.name} / {tool}；arguments 遵循："
                        + json.dumps(
                            row.content["manifest"][tool]["inputSchema"], ensure_ascii=False
                        )[:4000],
                        ExtensionArguments,
                        bind(row, tool),
                    )
                )
    readonly = list(base_specs) + specs
    for row in rows:
        if row.kind != "skill":
            continue

        def bind_skill(extension):
            async def handler(deps, args):
                with deps.db_factory() as db:
                    if db.get(ExtensionConfig, extension.id).state not in {"active", "superseded"}:
                        raise DomainError("EXTENSION_DISABLED", "Skill 已停用", 409)
                skill = Skill.model_validate(extension.content)

                def trace(step, status):
                    with deps.db_factory() as db:
                        audit(
                            db,
                            deps.repo.id,
                            deps.ctx.actor_id,
                            deps.ctx.run_id,
                            "skill.step",
                            extension_id=str(extension.id),
                            version=extension.version,
                            step=step,
                            status=status,
                        )
                        db.commit()

                executor = ToolExecutor(readonly, deps)

                async def agent_runner(agent, arguments):
                    import asyncio
                    from types import SimpleNamespace

                    from app.jobs.handlers.analyze import handle_analyze
                    from app.persistence.models import Run, RunEvidence
                    from app.services.run_service import validate_target

                    if deps.scratch.get("skill_agent_depth", 0) or execution.run.parent_run_id:
                        raise DomainError("SKILL_RECURSION", "子 Agent 不允许继续委派", 409)
                    target = validate_target(agent, arguments)
                    with deps.db_factory() as db:
                        child = Run(
                            repo_id=deps.repo.id,
                            parent_run_id=execution.run.id,
                            kind=agent,
                            target=target,
                            status="queued",
                            context_through_seq=execution.run.context_through_seq,
                            config_snapshot={
                                **execution.run.config_snapshot,
                                "disable_extensions": True,
                            },
                        )
                        db.add(child)
                        db.commit()
                        child_id = child.id
                    fake = SimpleNamespace(
                        id=execution.job_id, entity_id=child_id, lease_epoch=execution.job_epoch
                    )
                    await asyncio.to_thread(
                        handle_analyze, deps.db_factory, deps.settings, fake, execution.worker_id
                    )
                    execution.check_stop()
                    with deps.db_factory() as db:
                        child = db.get(Run, child_id)
                        if child.status != "completed" or not child.result:
                            raise DomainError("SKILL_AGENT_FAILED", "子 Agent 未形成结果", 409)
                        for binding in db.scalars(
                            select(RunEvidence).where(RunEvidence.run_id == child_id)
                        ):
                            if binding.evidence_id not in execution.collected_ids:
                                execution.db.add(
                                    RunEvidence(
                                        run_id=execution.run.id,
                                        evidence_id=binding.evidence_id,
                                        usage="collected",
                                    )
                                )
                                execution.collected_ids.add(binding.evidence_id)
                        execution.db.commit()
                        return child.result

                output, evidence = await execute_skill(
                    skill, args.arguments, executor, execution.check_stop, trace, agent_runner
                )
                return ok_result(
                    {
                        "extension_id": str(extension.id),
                        "version": extension.version,
                        "output": output,
                    },
                    source="skill",
                    evidence_ids=evidence,
                    freshness="unknown",
                )

            return handler

        specs.append(
            ToolSpec(
                f"skill_{row.name}", row.content["description"], ExtensionArguments, bind_skill(row)
            )
        )
    return specs
