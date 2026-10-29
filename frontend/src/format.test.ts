import { describe, expect, it } from 'vitest';
import { formatMoney } from './components/Money';
import { justNow, plural } from './format';

describe('plural', () => {
  it.each([[1, 'обращение'], [3, 'обращения'], [5, 'обращений'], [11, 'обращений'], [14, 'обращений'], [21, 'обращение'], [22, 'обращения'], [111, 'обращений'], [0, 'обращений']])(
    '%i → %s', (count, word) => expect(plural(count, 'обращение', 'обращения', 'обращений')).toBe(word));
});

describe('formatMoney', () => {
  it.each([
    [2_014_880, '20 148,80 ₽'], [10_000, '100 ₽'], [10_049, '100,49 ₽'], [5_000_050, '50 000,50 ₽'], [9_960, '99,60 ₽'],
    [157, '1,57 ₽'], [5, '0,05 ₽'], [200, '2 ₽'], [0, '0 ₽'], [-150, '−1,50 ₽'],
  ])('%i коп. → %s', (kopecks, text) => expect(formatMoney(kopecks)).toBe(text));
});

describe('justNow', () => {
  const now = Date.parse('2026-11-25T12:00:00Z');
  it('меньше минуты назад — только что', () => expect(justNow('2026-11-25T11:59:30Z', now)).toBe(true));
  it('старше минуты и из будущего — время', () => {
    expect(justNow('2026-11-25T11:58:59Z', now)).toBe(false);
    expect(justNow('2026-11-26T12:00:00Z', now)).toBe(false);
  });
  it('без времени бизнеса (демо) — время', () => expect(justNow('2026-11-25T11:59:30Z', null)).toBe(false));
});
