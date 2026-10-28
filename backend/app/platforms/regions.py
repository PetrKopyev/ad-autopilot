from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from time import monotonic

from app.platforms.base import PlatformError

TTL_SECONDS = 24 * 3600

# Города, которые можно выбрать в сведениях о бизнесе, и их id в геобазе Яндекса. Сверены 28.10.2026 по
# https://yandex.ru/dev/tasks-api/doc/ru/regions.md, остальные — по https://yandex.ru/pogoda/<id>. VK ищет город по
# названию в своём справочнике регионов.
CITY_REGIONS = {
    "Абакан": 1095,
    "Анадырь": 11458,
    "Архангельск": 20,
    "Астрахань": 37,
    "Барнаул": 197,
    "Белгород": 4,
    "Биробиджан": 11393,
    "Благовещенск": 77,
    "Брянск": 191,
    "Великий Новгород": 24,
    "Владивосток": 75,
    "Владикавказ": 33,
    "Владимир": 192,
    "Волгоград": 38,
    "Вологда": 21,
    "Воронеж": 193,
    "Горно-Алтайск": 11319,
    "Грозный": 1106,
    "Екатеринбург": 54,
    "Иваново": 5,
    "Ижевск": 44,
    "Иркутск": 63,
    "Йошкар-Ола": 41,
    "Казань": 43,
    "Калининград": 22,
    "Калуга": 6,
    "Кемерово": 64,
    "Киров": 46,
    "Кострома": 7,
    "Краснодар": 35,
    "Красноярск": 62,
    "Курган": 53,
    "Курск": 8,
    "Кызыл": 11333,
    "Липецк": 9,
    "Магадан": 79,
    "Магнитогорск": 235,
    "Майкоп": 1093,
    "Махачкала": 28,
    "Москва": 213,
    "Москва и Московская область": 1,
    "Мурманск": 23,
    "Набережные Челны": 236,
    "Назрань": 1092,
    "Нальчик": 30,
    "Нижневартовск": 1091,
    "Нижний Новгород": 47,
    "Новокузнецк": 237,
    "Новороссийск": 970,
    "Новосибирск": 65,
    "Омск": 66,
    "Орёл": 10,
    "Оренбург": 48,
    "Пенза": 49,
    "Пермь": 50,
    "Петрозаводск": 18,
    "Петропавловск-Камчатский": 78,
    "Псков": 25,
    "Ростов-на-Дону": 39,
    "Рязань": 11,
    "Салехард": 58,
    "Самара": 51,
    "Санкт-Петербург": 2,
    "Саранск": 42,
    "Саратов": 194,
    "Смоленск": 12,
    "Сочи": 239,
    "Ставрополь": 36,
    "Сургут": 973,
    "Сыктывкар": 19,
    "Тамбов": 13,
    "Тверь": 14,
    "Тольятти": 240,
    "Томск": 67,
    "Тула": 15,
    "Тюмень": 55,
    "Улан-Удэ": 198,
    "Ульяновск": 195,
    "Уфа": 172,
    "Хабаровск": 76,
    "Ханты-Мансийск": 57,
    "Чебоксары": 45,
    "Челябинск": 56,
    "Черкесск": 1104,
    "Череповец": 968,
    "Чита": 68,
    "Элиста": 1094,
    "Южно-Сахалинск": 80,
    "Якутск": 74,
    "Ярославль": 16,
}


@dataclass(frozen=True)
class Region:
    id: int
    name: str
    parent_id: int | None
    city: bool = True


def normalize(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").split())


CITY_NAMES = {normalize(name): name for name in CITY_REGIONS}
CITY_IDS = {normalize(name): region for name, region in CITY_REGIONS.items()}


class RegionCache:
    """Справочник регионов площадки в памяти процесса: меняется редко, а запрос тратит баллы и время."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[float, dict[int, Region]]] = {}

    async def get(self, key: str, load: Callable[[], Awaitable[Iterable[Region]]]) -> dict[int, Region]:
        cached = self.items.get(key)
        if cached is None or cached[0] <= monotonic():
            cached = (monotonic() + TTL_SECONDS, {region.id: region for region in await load()})
            self.items[key] = cached
        return cached[1]

    def clear(self) -> None:
        self.items.clear()


def in_country(region: Region, regions: dict[int, Region], country: int) -> bool:
    seen = set()
    while region.parent_id is not None and region.parent_id not in seen:
        if region.parent_id == country:
            return True
        seen.add(region.parent_id)
        parent = regions.get(region.parent_id)
        if parent is None:
            return False
        region = parent
    return False


def find_region(
    city: str, candidates: Iterable[Region], regions: dict[int, Region], *, country: int, platform: str
) -> int | None:
    """Город внутри страны; из нескольких одноимённых — город, а не область, и город внутри региона страны."""
    name = normalize(city)
    found = {
        region.id: region
        for region in candidates
        if normalize(region.name) == name and in_country(region, regions, country)
    }
    if not found:
        return None
    best = max((region.city, region.parent_id != country) for region in found.values())
    chosen = [region for region in found.values() if (region.city, region.parent_id != country) == best]
    if len(chosen) > 1:
        raise PlatformError(
            f"В справочнике регионов {platform} несколько мест с названием «{city.strip()}», выбрать нельзя. "
            "Укажите в сведениях о бизнесе ближайший крупный город."
        )
    return chosen[0].id


def not_found(city: str, platform: str) -> PlatformError:
    return PlatformError(
        f"Город «{city.strip()}» не найден в справочнике регионов {platform}. "
        "Проверьте название города в сведениях о бизнесе."
    )
