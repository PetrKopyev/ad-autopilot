from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models import Business, LeadsSurvey
from app.services.clients import ClientsRepository, ClientsService
from app.services.onboarding import NotFound
from app.services.stats import StatsRow, aggregate_clients

TODAY = date(2026, 10, 26)
WEEK = date(2026, 10, 19)


class AnsweredToday(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 26, 9, tzinfo=UTC)


@pytest.fixture(autouse=True)
def answered_today(monkeypatch):
    # Цена клиента учитывает только ответы, данные не позже дня расчёта (aggregate_clients).
    monkeypatch.setattr("app.services.clients.datetime", AnsweredToday)


@pytest.fixture
def setup():
    business = Business(id=1, lead_goal_ids=[1, 1], avg_check_kop=10000)
    question = LeadsSurvey(business_id=1, week_start=WEEK, leads_count=10)
    repository = MagicMock()
    repository.lock_business.return_value = business
    repository.questions.return_value = [question]
    repository.unanswered_weeks.return_value = []
    repository.rows.return_value = [
        StatsRow(
            business_id=1,
            platform="direct",
            campaign_id=1,
            ad_id=1,
            ad_group_id=None,
            date=WEEK,
            impressions=100,
            clicks=20,
            cost_kop=10000,
            conversions={"1": 10, "2": 99},
            fetched_at=datetime(2026, 10, 26, tzinfo=UTC),
            is_settled=True,
        )
    ]
    return ClientsService(repository), repository, business, question


def test_question_uses_previous_week_and_selected_goals(setup):
    service, repository, business, _ = setup
    result = service.questions(business, TODAY)
    assert result["items"][0]["id"] == WEEK
    assert repository.create_question.call_args.args[:3] == (1, WEEK, 10)
    assert business.lead_to_client_rate == Decimal("0.3")


def test_unanswered_question_catches_up_with_late_leads():
    """Мелочь 5 круга 13: обращения недели досчитались после вопроса — пока ответа нет, число обращений в вопросе
    обновляется, и ответ «12 клиентов из 12» не получает 422; отвеченный вопрос не меняется."""
    db = MagicMock()
    ClientsRepository(db).create_question(1, WEEK, 12, datetime(2026, 10, 26, tzinfo=UTC))
    sql = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (business_id, week_start) DO UPDATE SET leads_count" in sql
    assert "WHERE leads_survey.answered_at IS NULL" in sql


def test_older_unanswered_question_catches_up_too(setup):
    """Ревью круга 13: число обращений догоняет отчёты у каждого вопроса без ответа, а не только у прошлой недели."""
    service, repository, business, _ = setup
    old_week = date(2026, 10, 5)
    repository.unanswered_weeks.return_value = [old_week]
    rows = {WEEK: repository.rows.return_value, old_week: [replace(repository.rows.return_value[0], date=old_week)]}
    repository.rows.side_effect = lambda business_id, date_from, date_to: rows.get(date_from, [])
    service.questions(business, TODAY)
    assert [call.args[:3] for call in repository.create_question.call_args_list] == [(1, old_week, 10), (1, WEEK, 10)]
    # Только недели окна цены клиента: старые вопросы в цену уже не входят.
    repository.unanswered_weeks.assert_called_once_with(1, date(2026, 9, 28), date(2026, 10, 25))
    db = MagicMock()
    ClientsRepository(db).unanswered_weeks(1, date(2026, 9, 28), date(2026, 10, 25))
    sql = str(db.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "leads_survey.week_start BETWEEN" in sql


def test_no_question_without_leads(setup):
    service, repository, business, _ = setup
    repository.rows.return_value = []
    service.questions(business, TODAY)
    repository.create_question.assert_not_called()


@pytest.mark.parametrize("answer", [-1, 11, True, 1.5, "3", "unknown"])
def test_invalid_answer_is_not_saved(setup, answer):
    service, repository, business, question = setup
    with pytest.raises(ValueError):
        service.answer(business, WEEK, answer, TODAY)
    assert question.answered_at is None
    repository.save.assert_not_called()


def test_answer_overwrites_and_updates_rate_and_reference(setup):
    service, _, business, question = setup
    result = service.answer(business, WEEK, 2, TODAY)
    assert result["cost_per_client_kop"] == 5000
    assert business.lead_to_client_rate == Decimal("0.25")
    # Опорную цену считает автопилот по объявлениям (optimize.reference); экрану «Клиенты» она не нужна.
    assert "ref_cpl_kop" not in result
    result = service.answer(business, WEEK, 10, TODAY)
    assert question.clients_count == 10
    assert result["cost_per_client_kop"] == 1000
    assert business.lead_to_client_rate == Decimal("0.65")


@pytest.mark.parametrize("answer", [None, "не знаю", 0])
def test_unknown_and_zero_have_no_client_price(setup, answer):
    service, _, business, question = setup
    service.answer(business, WEEK, 2, TODAY)
    result = service.answer(business, WEEK, answer, TODAY)
    assert result["cost_per_client_kop"] is None
    assert question.answered_at is not None
    assert business.lead_to_client_rate == (Decimal("0.15") if answer == 0 else Decimal("0.3"))


def test_other_business_question_is_not_found(setup):
    service, repository, business, _ = setup
    repository.questions.return_value = []
    with pytest.raises(NotFound):
        service.answer(business, WEEK, 1, TODAY)
    repository.save.assert_not_called()


def test_price_excludes_old_weeks_and_unanswered_week(setup):
    service, repository, business, question = setup
    old = LeadsSurvey(
        business_id=1,
        week_start=date(2026, 9, 21),
        leads_count=100,
        clients_count=50,
        answered_at=datetime(2026, 9, 28, tzinfo=UTC),
    )
    repository.questions.return_value = [question, old]
    assert service.answer(business, WEEK, 2, TODAY)["cost_per_client_kop"] == 5000
    repository.questions.return_value.append(LeadsSurvey(business_id=1, week_start=date(2026, 10, 12), leads_count=5))
    repository.rows.return_value.append(replace(repository.rows.return_value[0], date=date(2026, 10, 12)))
    # Неделя с обращениями без ответа не делит свой расход на чужих клиентов: она исключена, как на «Статистике».
    assert service.answer(business, WEEK, 2, TODAY)["cost_per_client_kop"] == 5000


def test_weeks_without_leads_count_their_spend_on_both_screens(setup):
    """Важно 2 круга 13: 4 недели по 5 000 ₽, в трёх обращений нет, в четвёртой 2 обращения и 1 клиент —
    клиент стоил 20 000 ₽, и «Клиенты», и «Статистика» показывают одно и то же."""
    service, repository, business, question = setup
    base = repository.rows.return_value[0]
    weeks = [date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12), WEEK]
    rows = [
        replace(base, date=week, cost_kop=500_000, conversions={"1": 2} if week == WEEK else {}) for week in weeks
    ]
    repository.rows.return_value = rows
    question.leads_count = 2
    assert service.answer(business, WEEK, 1, TODAY)["cost_per_client_kop"] == 2_000_000
    window = aggregate_clients(rows, [question], TODAY, business.lead_goal_ids)
    assert (window["clients"], window["cost_kop"], window["cost_per_client_kop"]) == (1, 2_000_000, 2_000_000)
    assert (window["answered_weeks"], window["unknown_weeks"]) == (1, 0)


def test_api_requires_owner_and_csrf(setup, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-only")
    monkeypatch.setenv("PLATFORM_MODE", "fake")
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api.clients import get_clients_service, router
    from app.api.stats import get_today
    from app.auth.sessions import current_session, current_user, get_repository
    from app.db.models import Session, User
    from app.db.session import engine

    def forbid_database(*args, **kwargs):
        pytest.fail("Clients tests must not connect to a database")

    monkeypatch.setattr(engine, "connect", forbid_database)
    service, _, business, _ = setup
    auth = MagicMock()

    def get_business(user_id, business_id):
        if business_id not in (None, 1):
            raise HTTPException(404)
        assert user_id == 7
        return business

    auth.get_business.side_effect = get_business
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: User(id=7)
    app.dependency_overrides[get_repository] = lambda: auth
    app.dependency_overrides[get_clients_service] = lambda: service
    app.dependency_overrides[get_today] = lambda: TODAY
    app.dependency_overrides[current_session] = lambda: Session(csrf_secret="test-csrf")
    with TestClient(app) as client:
        assert client.get("/api/clients/questions?business_id=2").status_code == 404
        assert client.get("/api/clients/questions").status_code == 200
        path = f"/api/clients/questions/{WEEK}/answer"
        assert client.post(path, json={"clients_count": 2}).status_code == 403
        client.cookies.set("__Host-csrf", "test-csrf")
        headers = {"X-CSRF-Token": "test-csrf"}
        assert client.post(path, json={"clients_count": 2}, headers=headers).status_code == 200
        assert client.post(path, json={"clients_count": -1}, headers=headers).status_code == 422
        assert client.post(path, json={"clients_count": True}, headers=headers).status_code == 422
        assert client.post(path + "?business_id=2", json={"clients_count": 1}, headers=headers).status_code == 404
        missing = f"/api/clients/questions/{date(2026, 9, 7)}/answer"
        response = client.post(missing, json={"clients_count": 1}, headers=headers)
        assert (response.status_code, response.json()["detail"]) == (404, "Вопрос не найден.")
