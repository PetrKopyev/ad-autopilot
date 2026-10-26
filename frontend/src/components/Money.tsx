import { copy } from '../copy';

const integerFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

export function formatMoney(kopecks: number): string {
  if (!Number.isSafeInteger(kopecks)) throw new RangeError(copy.common.invalidMoney);
  const amount = BigInt(kopecks);
  const absolute = amount < 0n ? -amount : amount;
  const rubles = integerFormat.format(absolute / 100n);
  const fraction = (absolute % 100n).toString().padStart(2, '0');
  return `${amount < 0n ? '−' : ''}${rubles},${fraction}\u00a0₽`;
}

export function Money({ kopecks, className }: { kopecks: number; className?: string }) {
  return <span className={className}>{formatMoney(kopecks)}</span>;
}
