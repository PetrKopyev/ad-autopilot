import { copy } from '../copy';

const integerFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

/** Целые рубли; меньше 100 ₽ — с копейками, чтобы цена обращения в 1–2 ₽ не теряла смысл. */
export function formatMoney(kopecks: number): string {
  if (!Number.isSafeInteger(kopecks)) throw new RangeError(copy.common.invalidMoney);
  const amount = BigInt(kopecks);
  const absolute = amount < 0n ? -amount : amount;
  const exact = absolute < 10_000n && absolute % 100n !== 0n;
  const rubles = exact ? `${absolute / 100n},${String(absolute % 100n).padStart(2, '0')}` : integerFormat.format((absolute + 50n) / 100n);
  return `${amount < 0n && rubles !== '0' ? '−' : ''}${rubles} ₽`;
}

export function Money({ kopecks, className }: { kopecks: number; className?: string }) {
  return <span className={className}>{formatMoney(kopecks)}</span>;
}
