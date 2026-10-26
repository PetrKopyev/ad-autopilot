from collections.abc import Mapping
from decimal import Decimal

from app.domain.settings import DEFAULT_SETTINGS, DomainSettings
from app.domain.types import Decision, ExperimentResult, Metrics, Verdict, nonnegative_int

TEMPLATES = {
    "PAUSE_NO_LEADS": "На объявление нажали {clicks} раз, но обращений не было. Остановили его, чтобы сберечь бюджет.",
    "PAUSE_TOO_EXPENSIVE": (
        "Потратили {spend} ₽, получили {leads} обращений. Даже с учётом погрешности это слишком дорого "
        "при ориентире {ref_cpl} ₽ за обращение. Остановили объявление."
    ),
    "PAUSE_LOW_CTR": (
        "Объявление показали {impressions} раз, нажали {clicks} раз. На соседние нажимают чаще. Остановили его."
    ),
    "SCALE_UP": (
        "Объявления приносят обращения по подходящей цене. Повысили дневной лимит с {old_limit} до {new_limit} ₽."
    ),
    "EXPERIMENT_START": (
        "Проверяем {hypothesis}: сравним по {n_per_arm} показов каждого объявления. Это займёт примерно {days} дней."
    ),
    "EXPERIMENT_WIN": (
        "Объявление {winner} показывали {impressions} раз, на него нажали {clicks} раз ({rate}%) "
        "против {other_rate}% у {loser}. Шанс случайного различия — {p_percent}%. "
        "Оставили {winner}, {loser} остановили."
    ),
    "EXPERIMENT_NO_DIFF": "Убедительной разницы между объявлениями нет. Оставили прежнее, новое остановили.",
    "EXPERIMENT_EXTEND": (
        "Данных пока мало для уверенного выбора. Один раз увеличим объём проверки на {extension_percent}%."
    ),
    "EXPERIMENT_CLICKBAIT": "Нажимают чаще, но в обращения эти нажатия переходят реже. Оставили прежнее объявление.",
    "EXPERIMENT_RUNNING": "Проверка идёт. Ждём достаточно показов и уточнения обращений за последние дни.",
    "EXPERIMENT_TOO_LONG": (
        "Для проверки нужно примерно {days} дней. За отведённый срок данных не хватит, её не запускаем."
    ),
    "NO_HYPOTHESIS": "Доступные идеи уже проверили или для следующей проверки пока недостаточно бюджета.",
    "BUDGET_STOP": (
        "Потрачено {spent} из {budget} ₽. Остановили рекламу заранее, с запасом {margin} ₽ на задержку отчётов."
    ),
    "BUDGET_RESUME": "Начался новый месяц. Возобновили рекламу в пределах бюджета {budget} ₽.",
    "SPEND_MISMATCH": (
        "Площадка сообщает больший расход, чем учтено у нас. Остановили рекламу и просим проверить расходы."
    ),
    "ANOMALY": (
        "Несколько дней нет обращений, хотя раньше они были. Проверьте сайт, счётчик и цель. Цену пока не оцениваем."
    ),
    "NOT_SHOWING": (
        "Объявление одобрено, но показов нет. Возможно, аудитория слишком узкая или в кабинете не хватает денег."
    ),
    "MIN_ACTIVE_GROUPS": (
        "Объявление требует внимания. Сохранили его, чтобы на площадке осталось достаточно активных групп."
    ),
    "BUDGET_OPPORTUNITY": (
        "При увеличении дневного бюджета на {extra_budget} ₽ можно получить ещё примерно {extra_leads} "
        "обращений в день. Это оценка по текущей цене. Решение об увеличении бюджета — за вами."
    ),
    "REJECTED_REWRITE": "Площадка отклонила объявление: {reason}. Подготовили исправленный текст.",
    "MARKING_SET": "Добавили обязательную маркировку рекламы: {marking}.",
    "LAUNCH": "Запустили рекламу. Месячный бюджет — {budget} ₽.",
    "OWNER_EDIT": "Вы изменили {object}. Дадим рекламе время поработать с новыми настройками.",
    "REVERT": "Вы отменили изменение: {change}. Вернули предыдущие настройки.",
    "EXTERNAL_CHANGE": "Настройки {object} изменились в кабинете площадки. Учли это изменение.",
}

MONEY_FIELDS = {"spend", "ref_cpl", "old_limit", "new_limit", "spent", "budget", "margin", "extra_budget"}


def rubles(kopecks: int) -> str:
    nonnegative_int(kopecks, "kopecks")
    whole, fraction = divmod(kopecks, 100)
    return f"{whole:,}".replace(",", " ") + (f",{fraction:02}" if fraction else "")


def explain(
    reason_code: str, numbers: Mapping[str, int | str | Decimal] | None = None, **values: int | str | Decimal
) -> str:
    data = dict(numbers or {}) | values
    if "hypothesis" in data:
        data["hypothesis"] = {
            "text": "другой текст",
            "image": "другую картинку",
            "audience": "другую аудиторию",
        }.get(data["hypothesis"], data["hypothesis"])
    for key in MONEY_FIELDS & data.keys():
        data[key] = rubles(data[key])
    for key, value in data.items():
        if isinstance(value, Decimal):
            data[key] = format(value, ".1f").replace(".", ",")
    return TEMPLATES[reason_code].format_map(data)


def explain_decision(decision: Decision) -> str:
    return explain(decision.reason_code, decision.numbers)


def explain_experiment(
    result: ExperimentResult, control: Metrics, variant: Metrics, settings: DomainSettings = DEFAULT_SETTINGS
) -> str:
    if result.verdict == Verdict.EXTEND:
        percent = format(((settings.test_extension_factor - 1) * 100).normalize(), "f").replace(".", ",")
        return explain(result.reason_code, extension_percent=percent)
    if result.verdict not in (Verdict.CONTROL, Verdict.VARIANT):
        return explain(result.reason_code)
    if result.p_value is None:
        raise ValueError("A winning result must include its p-value")
    winner, loser = ("Б", "А") if result.verdict == Verdict.VARIANT else ("А", "Б")
    metrics, other = (variant, control) if result.verdict == Verdict.VARIANT else (control, variant)
    percent = Decimal(str(result.p_value)) * 100
    displayed = format(percent, ".1f").replace(".", ",")
    return explain(
        "EXPERIMENT_WIN",
        winner=winner,
        loser=loser,
        impressions=metrics.impressions,
        clicks=metrics.clicks,
        rate=metrics.ctr * 100,
        other_rate=other.ctr * 100,
        p_percent="<0,1" if displayed == "0,0" else displayed,
    )
