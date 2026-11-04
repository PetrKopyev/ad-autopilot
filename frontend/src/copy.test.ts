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
