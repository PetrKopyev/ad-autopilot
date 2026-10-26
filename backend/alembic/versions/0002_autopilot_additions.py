"""Freeze timestamps, proposed statuses and art call operation ids."""

import sqlalchemy as sa

from alembic import op

revision = "0002_autopilot_additions"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("campaigns", "ad_groups", "targets"):
        op.add_column(table, sa.Column("frozen_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("art_calls", sa.Column("operation_id", sa.Text(), nullable=True))
    op.add_column("art_calls", sa.Column("seed", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_art_calls_operation_id"), "art_calls", ["operation_id"], unique=False)
    op.drop_constraint(op.f("ck_change_log_status_values"), "change_log", type_="check")
    op.create_check_constraint(
        op.f("ck_change_log_status_values"),
        "change_log",
        "status IN ('proposed', 'pending', 'applied', 'failed', 'reverted')",
    )
    op.drop_constraint(op.f("ck_experiments_status_values"), "experiments", type_="check")
    op.create_check_constraint(
        op.f("ck_experiments_status_values"),
        "experiments",
        "status IN ('proposed', 'running', 'concluded', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_experiments_status_values"), "experiments", type_="check")
    op.create_check_constraint(
        op.f("ck_experiments_status_values"), "experiments", "status IN ('running', 'concluded', 'cancelled')"
    )
    op.drop_constraint(op.f("ck_change_log_status_values"), "change_log", type_="check")
    op.create_check_constraint(
        op.f("ck_change_log_status_values"), "change_log", "status IN ('pending', 'applied', 'failed', 'reverted')"
    )
    op.drop_index(op.f("ix_art_calls_operation_id"), table_name="art_calls")
    op.drop_column("art_calls", "seed")
    op.drop_column("art_calls", "operation_id")
    for table in ("targets", "ad_groups", "campaigns"):
        op.drop_column(table, "frozen_until")
