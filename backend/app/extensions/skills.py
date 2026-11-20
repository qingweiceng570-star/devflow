"""Declarative acyclic templates. No code, evaluation, recursion or installation."""

import json
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from app.core.errors import DomainError
from app.domain.contracts import Contract
from app.extensions.mcp import check_schema


class SkillStep(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    kind: Literal["tool", "condition", "transform", "agent"]
    tool: str | None = None
    arguments: dict = Field(default_factory=dict)
    source: str | None = None
    equals: str | int | bool | None = None
    when: str | None = None
    agent: Literal["pr_review", "ci_debug", "issue_triage", "knowledge_qa"] | None = None


class Skill(Contract):
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict
    allowed_tools: list[str] = Field(max_length=30)
    steps: list[SkillStep] = Field(min_length=1, max_length=12)
    output_schema: dict
    max_steps: int = Field(default=12, ge=1, le=12)

    @model_validator(mode="after")
    def validate_steps(self):
        check_schema(self.input_schema)
        check_schema(self.output_schema)
        if len(self.steps) > self.max_steps:
            raise ValueError("超过步骤预算")
        known = {"input"}
        for step in self.steps:
            if step.id in known or (step.when and step.when not in known):
                raise ValueError("重复 ID 或依赖不存在/有环")
            if step.source and step.source.split(".")[0] not in known:
                raise ValueError("结果来源必须在前序步骤")
            if step.kind == "condition" and not step.source:
                raise ValueError("条件步骤必须指定来源")
            if step.kind == "tool" and (
                step.tool not in self.allowed_tools or step.tool.startswith("skill_")
            ):
                raise ValueError("工具未授权或递归 Skill")
            if step.kind == "agent" and not step.agent:
                raise ValueError("缺少 Agent")
            known.add(step.id)
        return self


def resolve(value, state):
    if isinstance(value, dict):
        if set(value) == {"$from"}:
            parts = value["$from"].split(".")
            result = state
            for part in parts:
                if not isinstance(result, dict) or part not in result:
                    raise DomainError("SKILL_REFERENCE", "模板字段引用不存在", 400)
                result = result[part]
            return result
        return {key: resolve(item, state) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, state) for item in value]
    return value


async def execute_skill(skill, inputs, executor, check_stop, trace, agent_runner=None):
    Draft202012Validator(skill.input_schema).validate(inputs)
    state, evidence = {"input": inputs}, []
    for step in skill.steps:
        check_stop()
        if step.when and state.get(step.when) is not True:
            state[step.id] = {"skipped": True}
            trace(step.id, "skipped")
            continue
        args = resolve(step.arguments, state)
        if step.kind == "tool":
            if step.tool not in executor.tool_names or step.tool not in skill.allowed_tools:
                raise DomainError("SKILL_TOOL_DENIED", "模板工具未安装或未授权", 403)
            result = await executor.execute(step.tool, json.dumps(args))
            if result.status == "error":
                raise DomainError(result.error.code, result.error.message, 409)
            state[step.id] = result.model_dump(mode="json")
            evidence.extend(result.evidence_ids)
        elif step.kind == "condition":
            state[step.id] = resolve({"$from": step.source}, state) == step.equals
        elif step.kind == "transform":
            state[step.id] = args
        elif agent_runner:
            state[step.id] = await agent_runner(step.agent, args)
        else:
            raise DomainError("SKILL_AGENT_CONTEXT", "Agent 步骤需要协作工作流上下文", 409)
        trace(step.id, "completed")
        if len(json.dumps(state, ensure_ascii=False).encode()) > 65536:
            raise DomainError("SKILL_RESULT_LIMIT", "模板累计结果超过 64 KiB", 409)
    output = state[skill.steps[-1].id]
    Draft202012Validator(skill.output_schema).validate(output)
    return output, list(dict.fromkeys(evidence))
