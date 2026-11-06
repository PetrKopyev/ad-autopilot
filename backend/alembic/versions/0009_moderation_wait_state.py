"""Включения, которые ждали проверки объявлений pending-записью (error = MODERATION_PENDING), становятся применёнными:
действие сделано, площадка проверяет. Их кампании получают review_since (время записи, как sync_local), включённые
объявления — статус moderation. Кампания остановленного бизнеса (не running, kill_switch) получает паузу: включение
ждало одобрения после остановки; остановленная владельцем (owner_paused, paused_owner) и его остановленные объявления
остаются как были. Запись, которая включала только объявления (MODERATION_PENDING ставился на любое изменение, не
только запуск), статус кампании не меняет, а review_since ставит работающей кампании: её объявления включит
sync.settle_review после одобрения, у остановленной кампании выключенные объявления — норма. Запись, чья кампания не
найдена, остаётся pending: её доводит recover_pending.

Откат: записи с пометкой migrated_review снова pending с прежним текстом, а локальный статус возвращается прежний из
пометки, если с тех пор его никто не менял. Точен откат только сразу после выкладки: позже работающий воркер уже довёл
эти кампании, и вернуть прежнее состояние можно только восстановлением из копии базы.
"""

import json

import sqlalchemy as sa

from alembic import op

revision = "0009_moderation_wait_state"
down_revision = "0008_campaign_review_since"
branch_labels = None
depends_on = None

MODERATION_PENDING = "Площадка ещё проверяет объявление. Включим его, как только одобрит."


def _enabled(operations: list[dict], entity_type: str) -> list[str]:
    return [
        op["entity_id"]
        for op in operations
        if op.get("entity_type") == entity_type and op.get("field") == "status" and op.get("after") == "active"
    ]


def _ads(bind, business_id: int, platform: str, ids: list[str]):
    return bind.execute(
        sa.text(
            """
            SELECT ads.id, ads.external_id, ads.status FROM ads
            JOIN ad_groups ON ad_groups.id = ads.ad_group_id
            JOIN campaigns ON campaigns.id = ad_groups.campaign_id
            WHERE campaigns.business_id = :business AND campaigns.platform = :platform
            AND ads.external_id = ANY(:ids)
            """
        ),
        {"business": business_id, "platform": platform, "ids": ids},
    ).all()


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT change_log.id, change_log.business_id, change_log.platform, change_log.operations,
                   change_log.evidence, change_log.created_at, businesses.status AS business_status,
                   businesses.settings AS settings
            FROM change_log JOIN businesses ON businesses.id = change_log.business_id
            WHERE change_log.status = 'pending' AND change_log.evidence -> '_service' ->> 'error' = :error
            ORDER BY change_log.id
            """
        ),
        {"error": MODERATION_PENDING},
    ).all()
    for row in rows:
        service = row.evidence["_service"]
        meta = (row.settings or {}).get("_worker") or {}
        running = row.business_status == "running" and not meta.get("kill_switch") and not meta.get("pending_stop")
        campaign = bind.execute(
            sa.text(
                "SELECT id, external_id, status, owner_paused FROM campaigns WHERE business_id = :business "
                "AND platform = :platform AND external_id = :external"
            ),
            {"business": row.business_id, "platform": row.platform, "external": service["refs"]["campaign_id"]},
        ).first()
        if campaign is None:
            continue
        ads = [
            ad
            for ad in _ads(bind, row.business_id, row.platform, _enabled(row.operations, "ad"))
            if ad.status not in {"active", "moderation", "rejected", "paused_owner"}
        ]
        enabled = campaign.external_id in _enabled(row.operations, "campaign")
        status = (
            campaign.status
            if not enabled or campaign.owner_paused or campaign.status == "paused_owner"
            else "active"
            if running
            else "paused_auto"
        )
        waits = enabled or campaign.status == "active"
        previous = {
            "campaign": campaign.status,
            "ads": {str(ad.id): ad.status for ad in ads},
            "set": status,
            "review": waits,
        }
        bind.execute(
            sa.text(
                "UPDATE campaigns SET status = :status, review_since = COALESCE(review_since, :since) WHERE id = :id"
            ),
            {"status": status, "since": row.created_at if waits else None, "id": campaign.id},
        )
        for ad in ads:
            bind.execute(sa.text("UPDATE ads SET status = 'moderation' WHERE id = :id"), {"id": ad.id})
        review = sorted(_enabled(row.operations, "ad") + _enabled(row.operations, "campaign"))
        evidence = row.evidence | {
            "_service": service | {"error": None, "differences": [], "review": review, "migrated_review": previous}
        }
        bind.execute(
            sa.text("UPDATE change_log SET status = 'applied', evidence = CAST(:evidence AS jsonb) WHERE id = :id"),
            {"evidence": json.dumps(evidence, ensure_ascii=False), "id": row.id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Позднюю запись откатываем первой: прежний статус кампании ранней записи — тот, что она застала до поздней.
    rows = bind.execute(
        sa.text(
            "SELECT id, business_id, platform, evidence FROM change_log "
            "WHERE status = 'applied' AND evidence -> '_service' -> 'migrated_review' IS NOT NULL ORDER BY id DESC"
        )
    ).all()
    for row in rows:
        service = dict(row.evidence["_service"])
        previous = service.pop("migrated_review")
        service.pop("review", None)
        service.pop("differences", None)
        service["error"] = MODERATION_PENDING
        campaign = {"business": row.business_id, "platform": row.platform, "external": service["refs"]["campaign_id"]}
        where = "business_id = :business AND platform = :platform AND external_id = :external"
        # Ожидание снова держит pending-запись.
        if previous.get("review", True):
            bind.execute(sa.text(f"UPDATE campaigns SET review_since = NULL WHERE {where}"), campaign)
        bind.execute(
            sa.text(f"UPDATE campaigns SET status = :previous WHERE {where} AND status = :status"),
            campaign | {"previous": previous["campaign"], "status": previous["set"]},
        )
        for ad_id, status in previous["ads"].items():
            bind.execute(
                sa.text("UPDATE ads SET status = :status WHERE id = :id AND status = 'moderation'"),
                {"status": status, "id": int(ad_id)},
            )
        bind.execute(
            sa.text("UPDATE change_log SET status = 'pending', evidence = CAST(:evidence AS jsonb) WHERE id = :id"),
            {"evidence": json.dumps(row.evidence | {"_service": service}, ensure_ascii=False), "id": row.id},
        )
