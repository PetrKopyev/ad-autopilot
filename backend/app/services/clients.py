from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Business, LeadsSurvey
from app.domain.rules import client_conversion
from app.domain.types import ClientAnswer
from app.services.onboarding import NotFound
from app.services.stats import StatsRepository, aggregate, aggregate_clients, client_window


class ClientsRepository(StatsRepository):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def lock_business(self, business_id: int) -> Business:
        return self.db.scalars(select(Business).where(Business.id == business_id).with_for_update()).one()

    def questions(self, business_id: int) -> list[LeadsSurvey]:
        return list(
            self.db.scalars(
                select(LeadsSurvey)
                .where(LeadsSurvey.business_id == business_id)
                .order_by(LeadsSurvey.week_start.desc())
            )
        )

    def unanswered_weeks(self, business_id: int, date_from: date, date_to: date) -> list[date]:
        return list(
            self.db.scalars(
                select(LeadsSurvey.week_start).where(
                    LeadsSurvey.business_id == business_id,
                    LeadsSurvey.answered_at.is_(None),
                    LeadsSurvey.week_start.between(date_from, date_to),
                )
            )
        )

    def create_question(self, business_id: int, week_start: date, leads: int, now: datetime) -> None:
        # Обращения недели досчитываются после вопроса: пока ответа нет, число в вопросе догоняет отчёты,
        # иначе ответ «клиентов больше, чем было обращений в вопросе» отклонялся бы.
        self.db.execute(
            insert(LeadsSurvey)
            .values(business_id=business_id, week_start=week_start, leads_count=leads, asked_at=now)
            .on_conflict_do_update(
                index_elements=[LeadsSurvey.business_id, LeadsSurvey.week_start],
                set_={"leads_count": leads},
                where=LeadsSurvey.answered_at.is_(None),
            )
        )

    def save(self) -> None:
        self.db.commit()


def question_item(question: LeadsSurvey) -> dict:
    return {
        "id": question.week_start,
        "business_id": question.business_id,
        "week_start": question.week_start,
        "week_end": question.week_start + timedelta(days=6),
        "leads_count": question.leads_count,
        "clients_count": question.clients_count,
        "asked_at": question.asked_at,
        "answered_at": question.answered_at,
    }


class ClientsService:
    def __init__(self, repository: ClientsRepository) -> None:
        self.repository = repository

    def questions(self, business: Business, today: date) -> dict:
        business = self.repository.lock_business(business.id)
        last_week = today - timedelta(days=today.weekday(), weeks=1)
        # Вопрос о прошлой неделе появляется, а у вопросов без ответа в окне цены клиента число обращений догоняет
        # отчёты: старые недели в цену уже не входят.
        for week_start in sorted({last_week, *self.repository.unanswered_weeks(business.id, *client_window(today))}):
            rows = self.repository.rows(business.id, week_start, week_start + timedelta(days=6))
            leads = aggregate(rows, business.lead_goal_ids, today)["leads"]
            if leads:
                self.repository.create_question(business.id, week_start, leads, datetime.now(UTC))
        questions = self.repository.questions(business.id)
        metrics = self.metrics(business, questions, today)
        self.repository.save()
        return {"items": [question_item(item) for item in questions], **metrics}

    def answer(self, business: Business, question_id: date, clients: int | str | None, today: date) -> dict:
        business = self.repository.lock_business(business.id)
        questions = self.repository.questions(business.id)
        question = next((item for item in questions if item.week_start == question_id), None)
        if question is None:
            raise NotFound("Вопрос не найден.")
        if clients == "не знаю":
            clients = None
        if clients is not None and (type(clients) is not int or not 0 <= clients <= question.leads_count):
            raise ValueError("Число клиентов должно быть от 0 до числа обращений.")
        question.clients_count = clients
        question.answered_at = datetime.now(UTC)
        metrics = self.metrics(business, questions, today)
        self.repository.save()
        return {"question": question_item(question), **metrics}

    def metrics(self, business: Business, questions: list[LeadsSurvey], today: date) -> dict:
        date_from, date_to = client_window(today)
        rows = self.repository.rows(business.id, date_from, date_to)
        answers = [
            ClientAnswer(
                week_end=item.week_start + timedelta(days=6),
                leads=item.leads_count,
                clients=item.clients_count if item.answered_at is not None else None,
            )
            for item in questions
            if item.week_start + timedelta(days=6) <= today
        ]
        business.lead_to_client_rate = client_conversion(answers)
        return {
            "lead_to_client_rate": business.lead_to_client_rate,
            "cost_per_client_kop": aggregate_clients(rows, questions, today, business.lead_goal_ids)[
                "cost_per_client_kop"
            ],
            "from": date_from,
            "to": date_to,
        }
