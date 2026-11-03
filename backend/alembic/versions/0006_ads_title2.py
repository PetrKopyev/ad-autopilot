"""Второй заголовок объявления: ads.title2 хранит то, что сгенерировал ad_text (план его уже держал),
и передаёт его в AdSpec — иначе генерация второго заголовка была мёртвой: на площадки он не уходил."""

import sqlalchemy as sa

from alembic import op

revision = "0006_ads_title2"
down_revision = "0005_ai_spend_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ads",
        sa.Column("title2", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("ads", "title2")
