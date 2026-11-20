from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from app.domain.contracts import Contract


class PlanTask(Contract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    agent: Literal["pr_review", "ci_debug", "issue_triage", "knowledge_qa"]
    target: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    required: bool = True


class ReleaseTarget(Contract):
    observer_rounds: int = Field(default=1, ge=0, le=1)
    pr_number: int = Field(ge=1)
    ci_run_id: int | None = Field(default=None, ge=1)
    attempt: int | None = Field(default=None, ge=1)
    tasks: list[PlanTask] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid_plan(self):
        if bool(self.ci_run_id) != bool(self.attempt):
            raise ValueError("CI run 与 attempt 必须同时指定")
        keys = {task.key for task in self.tasks}
        if "observer_ci" in keys:
            raise ValueError("observer_ci 是系统保留的任务 key")
        if len(keys) != len(self.tasks):
            raise ValueError("任务 key 重复")
        remaining = {task.key: set(task.depends_on) for task in self.tasks}
        if any(not deps <= keys for deps in remaining.values()):
            raise ValueError("依赖不存在")
        while remaining:
            ready = {key for key, deps in remaining.items() if not deps}
            if not ready:
                raise ValueError("计划存在环")
            remaining = {key: deps - ready for key, deps in remaining.items() if key not in ready}
        return self


class WeeklyTarget(Contract):
    period_start: datetime
    period_end: datetime
    timezone: str = Field(default="Asia/Shanghai", max_length=80)

    @model_validator(mode="after")
    def valid_period(self):
        if not self.period_start.tzinfo or not self.period_end.tzinfo:
            raise ValueError("日期必须含时区")
        seconds = (self.period_end - self.period_start).total_seconds()
        if not 0 < seconds <= 31 * 86400:
            raise ValueError("报告范围必须在 31 天内")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("未知时区") from None
        return self
