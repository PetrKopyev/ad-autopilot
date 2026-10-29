import { copy } from '../copy';

const integerFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

/** Рубли, копейки — только если они есть: сумму не округляем, чтобы бюджет 50 000,50 ₽ не стал 50 001 ₽. */
export function formatMoney(kopecks: number): string {
  if (!Number.isSafeInteger(kopecks)) throw new RangeError(copy.common.invalidMoney);
  const amount = BigInt(kopecks);
  const absolute = amount < 0n ? -amount : amount;
  const whole = integerFormat.format(absolute / 100n);
  const rubles = absolute % 100n === 0n ? whole : `${whole},${String(absolute % 100n).padStart(2, '0')}`;
  return `${amount < 0n && rubles !== '0' ? '−' : ''}${rubles} ₽`;
}

export function Money({ kopecks, className }: { kopecks: number; className?: string }) {
  return <span className={className}>{formatMoney(kopecks)}</span>;
}
