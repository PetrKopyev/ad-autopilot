from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Business, LeadsSurvey
from app.domain.rules import client_cac, client_conversion, ref_cpl
from app.domain.types import ClientAnswer
from app.services.onboarding import NotFound
from app.services.stats import StatsRepository, aggregate, client_window


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

    def create_question(self, business_id: int, week_start: date, leads: int, now: datetime) -> None:
        self.db.execute(
            insert(LeadsSurvey)
            .values(business_id=business_id, week_start=week_start, leads_count=leads, asked_at=now)
            .on_conflict_do_nothing(index_elements=[LeadsSurvey.business_id, LeadsSurvey.week_start])
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
        week_start = today - timedelta(days=today.weekday(), weeks=1)
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
        spend: dict[date, int] = {}
        for row in rows:
            week = row.date - timedelta(days=row.date.weekday())
            spend[week] = spend.get(week, 0) + row.cost_kop
        answers = [
            ClientAnswer(
                week_end=item.week_start + timedelta(days=6),
                leads=item.leads_count,
                clients=item.clients_count if item.answered_at is not None else None,
                spend=spend.get(item.week_start, 0),
            )
            for item in questions
            if item.week_start + timedelta(days=6) <= today
        ]
        business.lead_to_client_rate = client_conversion(answers)
        return {
            "lead_to_client_rate": business.lead_to_client_rate,
            "cost_per_client_kop": client_cac(answers, date_to),
            "ref_cpl_kop": ref_cpl(business.avg_check_kop, (), today, answers),
            "from": date_from,
            "to": date_to,
        }
