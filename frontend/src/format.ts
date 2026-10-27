/** Форма слова для числа: plural(3, 'обращение', 'обращения', 'обращений') → «обращения». */
export function plural(count: number, one: string, few: string, many: string): string {
  const tens = Math.abs(count) % 100;
  const units = tens % 10;
  if (tens > 10 && tens < 20) return many;
  if (units === 1) return one;
  return units > 1 && units < 5 ? few : many;
}

function parse(value: string): Date { return new Date(value.length === 10 ? `${value}T12:00:00` : value); }

export function displayDate(value: string): string { return parse(value).toLocaleDateString('ru-RU'); }
export function displayClock(value: string): string {
  return parse(value).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
export function displayDateTime(value: string): string { return `${displayDate(value)}, ${displayClock(value)}`; }

/**
 * «Только что» — если событие меньше минуты назад по текущему времени бизнеса.
 * now = null, пока время бизнеса неизвестно (в демо оно приходит с сервера): тогда всегда показываем время.
 */
export function justNow(value: string, now: number | null): boolean {
  if (now === null) return false;
  const passed = now - new Date(value).getTime();
  return passed >= 0 && passed < 60_000;
}
