import type { ChangeEvent, InputHTMLAttributes } from 'react';

export const groupDigits = (digits: string) => digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
// Длиннее суммы не бывает: больше предела поле принимает, а ошибку показывает форма.
const maxWholeDigits = 12;

/** Рубли и копейки из набранного: точка и запятая — разделитель, до двух знаков после него, лишние нули слева убираются. */
// Разделитель — точка или запятая сразу после цифры: точка в «567 руб.» копейками не считается.
// Минус в начале остаётся: «-100» не должно молча стать 100 ₽, форма покажет ошибку.
function normalize(raw: string): string {
  const sign = /^\s*[-−–]/.test(raw) ? '-' : '';
  const match = /(?:^|\d)[.,]/.exec(raw);
  const at = match ? match.index + match[0].length - 1 : -1;
  const whole = (at < 0 ? raw : raw.slice(0, at)).replace(/\D/g, '').replace(/^0+/, '').slice(0, maxWholeDigits);
  if (at < 0) return sign + whole;
  return `${sign}${whole || '0'},${raw.slice(at + 1).replace(/\D/g, '').slice(0, 2)}`;
}
/** Значение поля из копеек: 5 000 050 → «50000,50», без округления. */
export const rublesValue = (kopecks: number) => kopecks % 100 ? `${Math.trunc(kopecks / 100)},${String(kopecks % 100).padStart(2, '0')}` : String(kopecks / 100);
const display = (value: string) => {
  const sign = value.startsWith('-') ? '-' : '';
  const [whole, fraction] = value.slice(sign.length).split(',');
  return sign + (fraction === undefined ? groupDigits(whole) : `${groupDigits(whole)},${fraction}`);
};

export interface RublesInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'inputMode'> {
  /** Рубли без пробелов, копейки через запятую: «1500», «1500,5». */
  value: string;
  onChange: (value: string) => void;
}

/** Сумма в рублях: разряды через пробел, копейки через запятую или точку, курсор остаётся у того же знака. */
export function RublesInput({ value, onChange, ...props }: RublesInputProps) {
  function change(event: ChangeEvent<HTMLInputElement>) {
    const input = event.target;
    const next = normalize(input.value);
    const before = Math.min(next.length, normalize(input.value.slice(0, input.selectionStart ?? input.value.length)).length);
    const shown = display(next);
    let caret = 0;
    for (let seen = 0; caret < shown.length && seen < before; caret += 1) if (shown[caret] !== ' ') seen += 1;
    input.value = shown;
    input.setSelectionRange(caret, caret);
    if (next !== value) onChange(next);
  }
  return <input {...props} inputMode="decimal" autoComplete="off" value={display(value)} onChange={change} />;
}
