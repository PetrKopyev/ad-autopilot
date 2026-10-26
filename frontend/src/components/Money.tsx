import { copy } from '../copy';

const integerFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

export function formatMoney(kopecks: number): string {
  if (!Number.isSafeInteger(kopecks)) throw new RangeError(copy.common.invalidMoney);
  const amount = BigInt(kopecks);
  const absolute = amount < 0n ? -amount : amount;
  const rounded = (absolute + 50n) / 100n;
  const rubles = integerFormat.format(rounded);
  return `${amount < 0n && rounded !== 0n ? '−' : ''}${rubles}\u00a0₽`;
}

export function Money({ kopecks, className }: { kopecks: number; className?: string }) {
  return <span className={className}>{formatMoney(kopecks)}</span>;
}
