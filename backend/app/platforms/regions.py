from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from time import monotonic

from app.platforms.base import PlatformError

TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Region:
    id: int
    name: str
    parent_id: int | None
    city: bool = True


def normalize(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").split())


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
