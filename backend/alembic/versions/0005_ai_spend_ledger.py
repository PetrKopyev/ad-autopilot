"""AiSpendLedger: учёт трат на ИИ для лимитов владельца и суточного потолка сервиса.

Таблица без внешних ключей: удаление бизнеса или аккаунта не обнуляет лимит (иначе трату оператору
можно было бы сжигать бесконечным пересозданием аккаунта). owner_key — sha256(yandex_uid):
персональный идентификатор в базе не храним. Строка с final=false — резерв оценки до вызова
(защита от параллельных запросов); после ответа строка уточняется до фактической стоимости
либо удаляется при отказе.
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_ai_spend_ledger"
down_revision = "0004_marking_transmission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_spend_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("cost_kop", sa.BigInteger(), nullable=False),
        sa.Column("final", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cost_kop >= 0", name="cost_kop_non_negative"),
    )
    op.create_index("ix_ai_spend_ledger_owner", "ai_spend_ledger", ["owner_key", "created_at"])
    op.create_index("ix_ai_spend_ledger_business_id", "ai_spend_ledger", ["business_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_spend_ledger_business_id", table_name="ai_spend_ledger")
    op.drop_index("ix_ai_spend_ledger_owner", table_name="ai_spend_ledger")
    op.drop_table("ai_spend_ledger")
