"""Отметка «площадка ещё проверяет» у кампании: campaigns.review_since. Ожидание модерации — состояние кампании,
а не pending-запись журнала: статус кампании остаётся нашей целью, sync.settle_review доводит её после проверки."""

import sqlalchemy as sa

from alembic import op

revision = "0008_campaign_review_since"
down_revision = "0007_ai_spend_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("review_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "review_since")
