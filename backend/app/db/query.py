from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, true


@dataclass(frozen=True)
class Window:
    """Отбор по диапазону значений столбца, границы включительно; None — без границы."""

    start: Any = None
    end: Any = None

    def condition(self, column):
        return and_(
            true() if self.start is None else column >= self.start,
            true() if self.end is None else column <= self.end,
        )

    def matches(self, value) -> bool:
        # Как в SQL: сравнение с NULL не проходит ни одну границу.
        if value is None:
            return False
        return (self.start is None or value >= self.start) and (self.end is None or value <= self.end)


def condition(column, value):
    """Фильтр репозитория для SQL: значение (None — IS NULL), набор значений или Window."""
    if isinstance(value, Window):
        return value.condition(column)
    if isinstance(value, set | frozenset | list | tuple):
        return column.in_(value)
    return column == value


def matches(item, filters: dict[str, Any]) -> bool:
    """Тот же фильтр для строк в памяти; NULL не входит в набор значений, как в SQL IN."""
    return all(
        value.matches(getattr(item, name))
        if isinstance(value, Window)
        else getattr(item, name) is not None and getattr(item, name) in value
        if isinstance(value, set | frozenset | list | tuple)
        else getattr(item, name) == value
        for name, value in filters.items()
    )
