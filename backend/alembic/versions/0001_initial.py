"""Initial application schema."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("yandex_uid", sa.Text(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("yandex_uid", name=op.f("uq_users_yandex_uid")),
    )
    op.create_table(
        "businesses",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("site_url", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("niche_text", sa.Text(), nullable=True),
        sa.Column("niche_class", sa.Text(), nullable=True),
        sa.Column("medical_level", sa.Text(), server_default="none", nullable=False),
        sa.Column("description", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("description_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("month_budget_kop", sa.BigInteger(), nullable=False),
        sa.Column("avg_check_kop", sa.BigInteger(), nullable=True),
        sa.Column("lead_to_client_rate", sa.Numeric(), nullable=True),
        sa.Column("auto_continue", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("metrica_counter_id", sa.BigInteger(), nullable=True),
        sa.Column("lead_goal_ids", postgresql.ARRAY(sa.Integer()), server_default="{}", nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.CheckConstraint(
            "medical_level IN ('none', 'maybe', 'required')", name=op.f("ck_businesses_medical_level_values")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'plan_ready', 'running', 'paused', 'budget_stop')",
            name=op.f("ck_businesses_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_businesses_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_businesses")),
    )
    op.create_table(
        "oauth_states",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("pkce_verifier", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("purpose IN ('login', 'direct', 'vk')", name=op.f("ck_oauth_states_purpose_values")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_oauth_states_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("state", name=op.f("pk_oauth_states")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("csrf_secret", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_table(
        "art_calls",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("cost_kop", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_art_calls_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_art_calls")),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("idea_text", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("phash", sa.Text(), nullable=True),
        sa.Column("formats", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("platform_refs", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("cost_kop", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("selected", sa.Boolean(), server_default="false", nullable=False),
        sa.CheckConstraint("kind IN ('image', 'video', 'upload')", name=op.f("ck_assets_kind_values")),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_assets_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
    )
    op.create_table(
        "budget_ledger",
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("allocated_kop", sa.BigInteger(), nullable=False),
        sa.Column("period_cap_kop", sa.BigInteger(), nullable=False),
        sa.Column("spent_kop", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("spent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hard_stop_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("platform IN ('direct', 'vk', 'fake')", name=op.f("ck_budget_ledger_platform_values")),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_budget_ledger_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("business_id", "month", "platform", name=op.f("pk_budget_ledger")),
    )
    op.create_table(
        "change_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("operations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("reason_params", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("reverts_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "actor IN ('autopilot', 'owner', 'system', 'external')", name=op.f("ck_change_log_actor_values")
        ),
        sa.CheckConstraint(
            "kind IN ('pause', 'resume', 'budget', 'text', 'image', 'launch', 'revert', "
            "'experiment_verdict', 'marking')",
            name=op.f("ck_change_log_kind_values"),
        ),
        sa.CheckConstraint("platform IN ('direct', 'vk', 'fake')", name=op.f("ck_change_log_platform_values")),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'failed', 'reverted')", name=op.f("ck_change_log_status_values")
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_change_log_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reverts_id"], ["change_log.id"], name=op.f("fk_change_log_reverts_id_change_log"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_log")),
    )
    op.create_index(
        "ix_change_log_business_id_created_at",
        "change_log",
        ["business_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "leads_survey",
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("leads_count", sa.Integer(), nullable=False),
        sa.Column("clients_count", sa.Integer(), nullable=True),
        sa.Column("asked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_leads_survey_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("business_id", "week_start", name=op.f("pk_leads_survey")),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False),
        sa.Column("cost_kop", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_llm_calls_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_table(
        "marking_profiles",
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("inn_enc", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone_enc", sa.Text(), nullable=True),
        sa.Column(
            "confirmed_in_platform", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.CheckConstraint(
            "subject_type IN ('ip', 'ooo', 'selfemployed', 'person')",
            name=op.f("ck_marking_profiles_subject_type_values"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_marking_profiles_business_id_businesses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("business_id", name=op.f("pk_marking_profiles")),
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'launched', 'failed')", name=op.f("ck_plans_status_values")
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_plans_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
    )
    op.create_table(
        "platform_accounts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("auth_mode", sa.Text(), nullable=False),
        sa.Column("external_login", sa.Text(), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("status", sa.Text(), server_default="ok", nullable=False),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("auth_mode IN ('oauth', 'manual_key')", name=op.f("ck_platform_accounts_auth_mode_values")),
        sa.CheckConstraint("platform IN ('direct', 'vk', 'fake')", name=op.f("ck_platform_accounts_platform_values")),
        sa.CheckConstraint(
            "status IN ('ok', 'needs_reauth', 'revoked')", name=op.f("ck_platform_accounts_status_values")
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_platform_accounts_business_id_businesses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_accounts")),
    )
    op.create_table(
        "worker_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("units_spent", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("log", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_worker_runs_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_runs")),
    )
    op.create_table(
        "campaigns",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("platform_account_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("daily_limit_kop", sa.BigInteger(), nullable=False),
        sa.Column("period_cap_kop", sa.BigInteger(), nullable=False),
        sa.Column("owner_paused", sa.Boolean(), server_default="false", nullable=False),
        sa.CheckConstraint("platform IN ('direct', 'vk', 'fake')", name=op.f("ck_campaigns_platform_values")),
        sa.CheckConstraint("type IN ('unified', 'text', 'vk_plan')", name=op.f("ck_campaigns_type_values")),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_campaigns_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["platform_account_id"],
            ["platform_accounts.id"],
            name=op.f("fk_campaigns_platform_account_id_platform_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaigns")),
    )
    op.create_table(
        "ad_groups",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("geo", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], name=op.f("fk_ad_groups_campaign_id_campaigns"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ad_groups")),
    )
    op.create_table(
        "ads",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ad_group_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("display_url", sa.Text(), nullable=True),
        sa.Column("asset_id", sa.BigInteger(), nullable=True),
        sa.Column("video_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("moderation_status", sa.Text(), nullable=True),
        sa.Column("moderation_reason", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), server_default="base", nullable=False),
        sa.Column("experiment_id", sa.BigInteger(), nullable=True),
        sa.Column("frozen_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('base', 'control', 'variant')", name=op.f("ck_ads_role_values")),
        sa.CheckConstraint(
            "status IN ('draft', 'moderation', 'active', 'paused_auto', 'paused_owner', 'rejected', 'archived')",
            name=op.f("ck_ads_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["ad_group_id"], ["ad_groups.id"], name=op.f("fk_ads_ad_group_id_ad_groups"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], name=op.f("fk_ads_asset_id_assets"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["video_id"], ["assets.id"], name=op.f("fk_ads_video_id_assets"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ads")),
    )
    op.create_table(
        "targets",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ad_group_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('phrase', 'interest', 'audience', 'negative')", name=op.f("ck_targets_kind_values")
        ),
        sa.ForeignKeyConstraint(
            ["ad_group_id"], ["ad_groups.id"], name=op.f("fk_targets_ad_group_id_ad_groups"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_targets")),
    )
    op.create_table(
        "experiments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("ad_group_id", sa.BigInteger(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("control_ad_id", sa.BigInteger(), nullable=False),
        sa.Column("variant_ad_id", sa.BigInteger(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("planned_n_per_arm", sa.BigInteger(), nullable=False),
        sa.Column("min_effect", sa.Numeric(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("change_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "hypothesis IN ('text', 'image', 'audience')", name=op.f("ck_experiments_hypothesis_values")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'concluded', 'cancelled')", name=op.f("ck_experiments_status_values")
        ),
        sa.CheckConstraint(
            "verdict IN ('variant_wins', 'control_wins', 'no_difference', 'inconclusive')",
            name=op.f("ck_experiments_verdict_values"),
        ),
        sa.ForeignKeyConstraint(
            ["ad_group_id"], ["ad_groups.id"], name=op.f("fk_experiments_ad_group_id_ad_groups"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_experiments_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["change_id"], ["change_log.id"], name=op.f("fk_experiments_change_id_change_log"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["control_ad_id"], ["ads.id"], name=op.f("fk_experiments_control_ad_id_ads"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["variant_ad_id"], ["ads.id"], name=op.f("fk_experiments_variant_ad_id_ads"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiments")),
    )
    op.create_table(
        "stats_daily",
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("ad_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("impressions", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("clicks", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cost_kop", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("conversions", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_settled", sa.Boolean(), server_default="false", nullable=False),
        sa.CheckConstraint("platform IN ('direct', 'vk', 'fake')", name=op.f("ck_stats_daily_platform_values")),
        sa.ForeignKeyConstraint(["ad_id"], ["ads.id"], name=op.f("fk_stats_daily_ad_id_ads"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_stats_daily_business_id_businesses"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], name=op.f("fk_stats_daily_campaign_id_campaigns"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("platform", "ad_id", "date", name=op.f("pk_stats_daily")),
    )
    op.create_index("ix_stats_daily_business_id_date", "stats_daily", ["business_id", "date"], unique=False)
    op.create_foreign_key(
        op.f("fk_ads_experiment_id_experiments"),
        "ads",
        "experiments",
        ["experiment_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_ads_experiment_id_experiments"), "ads", type_="foreignkey")
    op.drop_index("ix_stats_daily_business_id_date", table_name="stats_daily")
    op.drop_table("stats_daily")
    op.drop_table("experiments")
    op.drop_table("targets")
    op.drop_table("ads")
    op.drop_table("ad_groups")
    op.drop_table("campaigns")
    op.drop_table("worker_runs")
    op.drop_table("platform_accounts")
    op.drop_table("plans")
    op.drop_table("marking_profiles")
    op.drop_table("llm_calls")
    op.drop_table("leads_survey")
    op.drop_index("ix_change_log_business_id_created_at", table_name="change_log")
    op.drop_table("change_log")
    op.drop_table("budget_ledger")
    op.drop_table("assets")
    op.drop_table("art_calls")
    op.drop_table("sessions")
    op.drop_table("oauth_states")
    op.drop_table("businesses")
    op.drop_table("users")
