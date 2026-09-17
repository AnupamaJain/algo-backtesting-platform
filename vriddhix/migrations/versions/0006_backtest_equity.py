"""Persist the backtest equity curve and its computed metrics.

The backtester already computed a daily equity curve, CAGR, drawdown, Sharpe,
Sortino and Calmar -- and then discarded all of it, keeping only the trades.
Anything downstream had to recompute from the trade ledger, which can only
produce a *realised* curve: equity steps on exits and open losing positions
are never marked. Compared against a daily-marked benchmark that understates
drawdown, which is the flattering direction, and precisely the kind of quiet
error this platform exists to refuse.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("metrics", sa.Text(), nullable=True))

    op.create_table(
        "backtest_equity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("equity", sa.Numeric(18, 2), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "date", name="uq_backtest_equity_run_date"),
    )
    op.create_index("ix_backtest_equity_run", "backtest_equity", ["run_id", "date"])


def downgrade() -> None:
    op.drop_index("ix_backtest_equity_run", table_name="backtest_equity")
    op.drop_table("backtest_equity")
    op.drop_column("backtest_runs", "metrics")
