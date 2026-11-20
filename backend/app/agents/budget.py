"""Atomic, durable reservations shared by all nodes of a workflow."""

from uuid import UUID

from sqlalchemy import select, update

from app.core.errors import DomainError
from app.persistence.models import BudgetAccount, BudgetReservation, Run


class BudgetLedger:
    def __init__(self, db, run_id: UUID, limit_tokens: int):
        self.db = db
        run = db.get(Run, run_id)
        root_id = run.parent_run_id if run and run.parent_run_id else run_id
        row = db.scalar(select(BudgetAccount).where(BudgetAccount.run_id == root_id))
        if row is None:
            row = BudgetAccount(run_id=root_id, limit_tokens=limit_tokens)
            db.add(row)
            db.flush()
        self.account_id = row.id
        self._limit = int(row.limit_tokens * 0.85) if root_id != run_id else row.limit_tokens
        self.reservation_id = None
        db.commit()

    @property
    def limit_tokens(self):
        return self._limit

    @property
    def consumed_tokens(self):
        return self.db.scalar(
            select(BudgetAccount.consumed_tokens).where(BudgetAccount.id == self.account_id)
        )

    @property
    def remaining(self):
        consumed, reserved = self.db.execute(
            select(BudgetAccount.consumed_tokens, BudgetAccount.reserved_tokens).where(
                BudgetAccount.id == self.account_id
            )
        ).one()
        return self._limit - consumed - reserved

    def reserve(self, tokens):
        if tokens < 0 or self.reservation_id:
            raise DomainError("BUDGET_RESERVATION_INVALID", "预算预留状态无效", 409)
        result = self.db.execute(
            update(BudgetAccount)
            .where(
                BudgetAccount.id == self.account_id,
                BudgetAccount.consumed_tokens + BudgetAccount.reserved_tokens + tokens
                <= self._limit,
            )
            .values(reserved_tokens=BudgetAccount.reserved_tokens + tokens)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise DomainError("BUDGET_EXCEEDED", "模型调用累计预算不足", 409)
        reservation = BudgetReservation(account_id=self.account_id, tokens=tokens)
        self.db.add(reservation)
        self.db.commit()
        self.reservation_id = reservation.id

    def settle(self, actual_tokens):
        if self.reservation_id is None:
            raise DomainError("BUDGET_RESERVATION_INVALID", "缺少预算预留", 409)
        reservation = self.db.scalar(
            select(BudgetReservation)
            .where(BudgetReservation.id == self.reservation_id)
            .with_for_update()
        )
        if reservation.state == "reserved":
            actual = max(0, actual_tokens)
            reservation.state = "settled"
            reservation.actual_tokens = actual
            self.db.execute(
                update(BudgetAccount)
                .where(BudgetAccount.id == self.account_id)
                .values(
                    reserved_tokens=BudgetAccount.reserved_tokens - reservation.tokens,
                    consumed_tokens=BudgetAccount.consumed_tokens + actual,
                )
            )
            self.db.commit()
        self.reservation_id = None

    def release(self, reserved):
        # Unknown network outcome is conservatively charged, never refunded.
        if self.reservation_id:
            self.settle(reserved)


def settle_interrupted(db, run_id):
    account = db.scalar(
        select(BudgetAccount).where(BudgetAccount.run_id == run_id).with_for_update()
    )
    if account is None:
        return
    reservations = db.scalars(
        select(BudgetReservation)
        .where(BudgetReservation.account_id == account.id, BudgetReservation.state == "reserved")
        .with_for_update()
    ).all()
    for row in reservations:
        row.state = "estimated"
        row.actual_tokens = row.tokens
        account.consumed_tokens += row.tokens
        account.reserved_tokens -= row.tokens
