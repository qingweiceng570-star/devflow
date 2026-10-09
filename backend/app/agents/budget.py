"""Run-scoped token budget ledger (README 6.2 budget_id, 12.3).

One account per run. The current M2 loop is single-writer, so reserve/settle
are simple row updates; atomic cross-task reservations (budget_reservations)
arrive with parallel workflows in M5.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.errors import DomainError
from app.persistence.models import BudgetAccount


class BudgetLedger:
    def __init__(self, db: DBSession, run_id: UUID, limit_tokens: int):
        row = db.scalar(select(BudgetAccount).where(BudgetAccount.run_id == run_id))
        if row is None:
            row = BudgetAccount(run_id=run_id, limit_tokens=limit_tokens)
            db.add(row)
            db.flush()
        self._row = row
        self._limit = limit_tokens

    @property
    def limit_tokens(self) -> int:
        return self._limit

    @property
    def consumed_tokens(self) -> int:
        return self._row.consumed_tokens

    @property
    def remaining(self) -> int:
        return self._limit - self._row.consumed_tokens - self._row.reserved_tokens

    def reserve(self, tokens: int) -> None:
        """Fail fast with a stable error when the reservation cannot fit."""
        if tokens > self.remaining:
            raise DomainError("BUDGET_EXCEEDED", "模型调用累计预算不足", 409)
        self._row.reserved_tokens += tokens
        self._row.updated_at = datetime.now(UTC)

    def settle(self, actual_tokens: int) -> None:
        self._row.reserved_tokens = 0
        self._row.consumed_tokens += actual_tokens
        self._row.updated_at = datetime.now(UTC)

    def release(self, reserved: int) -> None:
        self._row.reserved_tokens = max(0, self._row.reserved_tokens - reserved)
        self._row.updated_at = datetime.now(UTC)
