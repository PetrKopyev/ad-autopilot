import { describe, expect, it } from 'vitest';
import { copy } from './copy';

describe('склонение единицы прогноза', () => {
  it('узкий диапазон показывает нижнюю границу — склоняем по ней', () => {
    expect(copy.plan.forecastShortUnit(101, 102)).toBe('обращение');
    expect(copy.plan.forecastShortUnit(102, 103)).toBe('обращения');
  });

  it('обычный диапазон склоняем по верхней границе', () => {
    expect(copy.plan.forecastShortUnit(1, 2)).toBe('обращения');
    expect(copy.plan.forecastShortUnit(5, 8)).toBe('обращений');
    expect(copy.plan.forecastShortUnit(1, 1)).toBe('обращение');
  });
});

describe('недели цены клиента', () => {
  it('подпись называет, из каких недель сложилась цена: тихие недели тоже входят', () => {
    const weeks = { answered_weeks: 1, quiet_weeks: 3, unknown_weeks: 0 };
    expect(copy.home.clientsHint(weeks)).toBe('За 4 недели: 1 с ответом, 3 без обращений.');
    expect(copy.screens.answeredWeeks({ ...weeks, quiet_weeks: 2, unknown_weeks: 1 })).toBe('За 4 недели: 1 с ответом, 2 без обращений, 1 без ответа.');
    expect(copy.home.clientsHint({ ...weeks, answered_weeks: 0 })).toBe('Появятся после ваших ответов о клиентах.');
  });
});
