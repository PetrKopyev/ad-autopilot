from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from app.domain.settings import DEFAULT_SETTINGS, DomainSettings
from app.domain.types import Decision, ExperimentResult, Metrics, Verdict, nonnegative_int

TEMPLATES = {
    # {period} — дни, по которым принято решение (period_start, period_end): расход на экране объявления считается
    # за другой период, и без дат цифры пояснения с ним не сходятся.
    "PAUSE_NO_LEADS": (
        "{period} на объявление «{ad}» нажали {clicks}, но обращений не было. Остановили его, чтобы сберечь бюджет."
    ),
    "PAUSE_TOO_EXPENSIVE": (
        "{period} на объявление «{ad}» потратили {spend} ₽, получили {leads}. Даже с учётом погрешности это "
        "слишком дорого при ориентире {ref_cpl} ₽ за обращение. Остановили его."
    ),
    "PAUSE_LOW_CTR": (
        "{period} объявление «{ad}» показали {impressions}, нажали {clicks}. На соседние нажимают чаще. Остановили его."
    ),
    "SCALE_UP": (
        "Объявления группы «{group}» (кампания «{campaign}») приносят обращения по подходящей цене. "
        "Повысили дневной лимит этой группы с {old_limit} до {new_limit} ₽."
    ),
    "EXPERIMENT_START": (
        "Проверяем {hypothesis}: сравним по {n_per_arm} каждого объявления. Это займёт примерно {days}."
    ),
    "EXPERIMENT_WIN": (
        "Объявление {winner} показывали {impressions}, на него нажали {clicks} ({rate}%) "
        "против {other_rate}% у {loser}. {confidence}. "
        "Оставили {winner}, {loser} остановили."
    ),
    "EXPERIMENT_NO_DIFF": "Убедительной разницы между объявлениями нет. Оставили прежнее, новое остановили.",
    "AUDIENCE_START": (
        "Сравниваем аудитории группы «{group}»: {control} и {variant}. Объявление то же, для новой аудитории — "
        "отдельная группа. Через {days} сравним цену обращения и оставим ту аудиторию, где обращения дешевле."
    ),
    "EXPERIMENT_EXTEND": (
        "Данных пока мало для уверенного выбора. Один раз увеличим объём проверки на {extension_percent}%."
    ),
    "EXPERIMENT_CLICKBAIT": (
        "На объявление {clicker} нажимают чаще: {rate}% показов против {other_rate}% у {other}. Но в обращения эти "
        "нажатия переходят реже: {conversion}% ({share}) против {other_conversion}% ({other_share}). "
        "Оставили прежнее объявление А, Б остановили."
    ),
    "EXPERIMENT_RUNNING": "Проверка идёт. Ждём достаточно показов и уточнения обращений за последние дни.",
    "EXPERIMENT_TOO_LONG": (
        "Для проверки нужно примерно {days}. За отведённый срок данных не хватит, её не запускаем."
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
        "в день. Это оценка по текущей цене. Решение об увеличении бюджета — за вами."
    ),
    "REJECTED_REWRITE": "Площадка отклонила объявление: {reason}. Подготовили исправленный текст.",
    "MARKING_SET": "Добавили обязательную маркировку рекламы: {marking}.",
    "LAUNCH": "Запустили рекламу. Месячный бюджет — {budget} ₽.",
    "OWNER_EDIT": "Вы изменили {object}. Дадим рекламе время поработать с новыми настройками.",
    "REVERT": "Вы отменили {change}. Вернули прежние настройки.",
    "EXTERNAL_CHANGE": "Настройки {object} изменились в кабинете площадки. Учли это изменение.",
    "EXTERNAL_PAUSE": "Вы остановили кампанию в кабинете площадки — автопилот её не включает.",
    "EXTERNAL_RESUME": (
        "Вы включили кампанию в кабинете площадки — автопилот не останавливает её сутки. "
        "Остановка по бюджету месяца при этом работает."
    ),
}

MONEY_FIELDS = {"spend", "ref_cpl", "old_limit", "new_limit", "spent", "budget", "margin", "extra_budget"}
# Число и слово согласуются: «1 раз», «3 раза», «5 раз»; дробное — «1,5 обращения».
PLURALS = {
    "clicks": ("раз", "раза", "раз"),
    "impressions": ("раз", "раза", "раз"),
    "leads": ("обращение", "обращения", "обращений"),
    "extra_leads": ("обращение", "обращения", "обращений"),
    "n_per_arm": ("показ", "показа", "показов"),
    "days": ("день", "дня", "дней"),
}


MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def period(start: date, end: date) -> str:
    """«5 ноября», «С 1 по 14 ноября», «С 25 октября по 7 ноября»; год — только если он разный."""
    if start == end:
        return f"{start.day} {MONTHS[start.month - 1]}"
    if start.year != end.year:
        return f"С {start.day} {MONTHS[start.month - 1]} {start.year} по {end.day} {MONTHS[end.month - 1]} {end.year}"
    if start.month != end.month:
        return f"С {start.day} {MONTHS[start.month - 1]} по {end.day} {MONTHS[end.month - 1]}"
    return f"С {start.day} по {end.day} {MONTHS[end.month - 1]}"


def rubles(kopecks: int) -> str:
    """Как в интерфейсе: рубли, копейки — только если они есть («20 148,80», «100»). Не округляем: бюджет 60 000,75
    не должен стать 60 001."""
    nonnegative_int(kopecks, "kopecks")
    whole, fraction = divmod(kopecks, 100)
    return f"{whole:,}".replace(",", " ") + (f",{fraction:02}" if fraction else "")


def plural(number: int | Decimal, forms: tuple[str, str, str]) -> str:
    if isinstance(number, Decimal):
        # Округляем до десятых раньше, чем смотреть на дробь: 62,96 — «63 обращения», а не «63,0».
        number = number.quantize(Decimal("0.1"))
        if number != number.to_integral_value():
            return f"{format(number, '.1f').replace('.', ',')} {forms[1]}"
    value = int(number)
    # Разряды отделяются пробелом, как в суммах и в интерфейсе: «11 040 раз».
    shown = f"{value:,}".replace(",", " ")
    if value % 10 == 1 and value % 100 != 11:
        word = forms[0]
    elif 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        word = forms[1]
    else:
        word = forms[2]
    return f"{shown} {word}"


UNDONE = {
    "budget": "изменение дневного лимита",
    "experiment_verdict": "вывод сравнения вариантов",
    "resume": "включение рекламы",
    "text": "замену текста объявления",
    "image": "замену картинки объявления",
    "launch": "запуск объявления",
    "marking": "добавление маркировки",
}


def undone(kind: str, entity_type: str, params: Mapping[str, object]) -> str:
    """Что отменил владелец — для записи об отмене: текст исходной записи («…Остановили объявление») читался бы
    как новая остановка."""
    if kind == "pause":
        if entity_type != "ad":
            return "остановку рекламы"
        return f"остановку объявления «{params['ad']}»" if "ad" in params else "остановку объявления"
    if kind == "launch" and "hypothesis" in params:
        # Старт проверки пишется запуском объявления-варианта; владелец отменял сравнение, а не «запуск объявления».
        return "сравнение вариантов"
    if kind == "launch" and "campaign" in params:
        return f"запуск рекламы «{params['campaign']}»"
    if kind == "budget" and "group" in params:
        return f"повышение дневного лимита группы «{params['group']}»"
    return UNDONE.get(kind, "изменение")


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
    if "period_start" in data:
        data["period"] = period(date.fromisoformat(data["period_start"]), date.fromisoformat(data["period_end"]))
    for key in MONEY_FIELDS & data.keys():
        data[key] = rubles(data[key])
    for key in PLURALS.keys() & data.keys():
        if isinstance(data[key], int | Decimal) and not isinstance(data[key], bool):
            data[key] = plural(data[key], PLURALS[key])
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
    if result.verdict == Verdict.CLICKBAIT:
        return explain_clickbait(control, variant)
    if result.verdict not in (Verdict.CONTROL, Verdict.VARIANT):
        return explain(result.reason_code)
    if result.p_value is None:
        raise ValueError("A winning result must include its p-value")
    winner, loser = ("Б", "А") if result.verdict == Verdict.VARIANT else ("А", "Б")
    metrics, other = (variant, control) if result.verdict == Verdict.VARIANT else (control, variant)
    # Владельцу — не p-значение, а вывод словами: побеждает только разница с p ниже порога проверки.
    confidence = (
        "Разница почти наверняка не случайна" if result.p_value < 0.01 else "Разница, скорее всего, не случайна"
    )
    return explain(
        "EXPERIMENT_WIN",
        winner=winner,
        loser=loser,
        impressions=metrics.impressions,
        clicks=metrics.clicks,
        rate=metrics.ctr * 100,
        other_rate=other.ctr * 100,
        confidence=confidence,
    )


def explain_clickbait(control: Metrics, variant: Metrics) -> str:
    """Чаще нажимают, но реже обращаются — с цифрами обеих сторон, как в остальных выводах сравнения."""
    (clicker, metrics), (other, rest) = sorted((("А", control), ("Б", variant)), key=lambda arm: -arm[1].ctr)

    def conversion(arm: Metrics) -> Decimal:
        return Decimal(arm.leads) / arm.clicks * 100 if arm.clicks else Decimal(0)

    def share(arm: Metrics) -> str:
        return f"{plural(arm.leads, PLURALS['leads'])} из {plural(arm.clicks, ('нажатия', 'нажатий', 'нажатий'))}"

    return explain(
        "EXPERIMENT_CLICKBAIT",
        clicker=clicker,
        other=other,
        rate=metrics.ctr * 100,
        other_rate=rest.ctr * 100,
        conversion=conversion(metrics),
        other_conversion=conversion(rest),
        share=share(metrics),
        other_share=share(rest),
    )


def audience_side(label: str, metrics: Metrics) -> str:
    if not metrics.leads:
        return f"«{label}» — ни одного обращения за {rubles(metrics.spend)} ₽"
    return f"«{label}» — {plural(metrics.leads, PLURALS['leads'])} по {rubles(metrics.spend // metrics.leads)} ₽"


def explain_audience(
    result: ExperimentResult,
    control: Metrics,
    variant: Metrics,
    labels: Sequence[str],
    settings: DomainSettings = DEFAULT_SETTINGS,
    *,
    overlap: bool = False,
) -> str:
    """Вывод проверки аудиторий словами: цена обращения каждой аудитории и что оставили. overlap — аудитории
    пересекаются (VK: «все возрасты» включают «25–54 года»), вывод говорит, что сравнивали настройку показа."""
    old, new = labels
    numbers = f"{audience_side(old, control)}, {audience_side(new, variant)}"
    if overlap:
        numbers += (
            ". Аудитории пересекаются: одни и те же люди могли видеть оба объявления, поэтому это сравнение настройки "
            "показа, а не двух разных групп людей"
        )
    if control.leads + variant.leads < settings.audience_min_leads:
        return (
            f"За две недели проверки аудиторий обращений слишком мало для честного сравнения: {numbers}. "
            f"Оставили «{old}», объявление для «{new}» остановили."
        )
    if result.verdict not in (Verdict.CONTROL, Verdict.VARIANT):
        return (
            f"Сравнили аудитории: {numbers}. Убедительной разницы в цене обращения нет. "
            f"Оставили «{old}», объявление для «{new}» остановили."
        )
    winner, loser = (new, old) if result.verdict == Verdict.VARIANT else (old, new)
    confidence = (
        "Разница почти наверняка не случайна"
        if result.p_value is not None and result.p_value < 0.01
        else "Разница, скорее всего, не случайна"
    )
    return (
        f"Сравнили аудитории: {numbers}. {confidence}. Обращения дешевле у аудитории «{winner}» — "
        f"её оставили, объявление для «{loser}» остановили."
    )
