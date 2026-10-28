import type { ChangeEvent, InputHTMLAttributes } from 'react';

export const groupDigits = (digits: string) => digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
const digitsOf = (value: string) => value.replace(/\D/g, '');

export interface RublesInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'inputMode'> {
  value: string;
  max: number;
  onChange: (digits: string) => void;
}

/** Целые рубли: только цифры, разряды через пробел, курсор остаётся у той же цифры. value — цифры без пробелов. */
export function RublesInput({ value, max, onChange, ...props }: RublesInputProps) {
  function change(event: ChangeEvent<HTMLInputElement>) {
    const input = event.target;
    const typed = digitsOf(input.value).replace(/^0+/, '');
    const next = Number(typed) > max ? value : typed;
    const before = digitsOf(input.value.slice(0, input.selectionStart ?? input.value.length)).replace(/^0+/, '').length
      - (typed.length - next.length);
    const shown = groupDigits(next);
    let caret = 0;
    for (let seen = 0; caret < shown.length && seen < before; caret += 1) if (/\d/.test(shown[caret])) seen += 1;
    input.value = shown;
    input.setSelectionRange(caret, caret);
    if (next !== value) onChange(next);
  }
  return <input {...props} inputMode="numeric" autoComplete="off" value={groupDigits(value)} onChange={change} />;
}
