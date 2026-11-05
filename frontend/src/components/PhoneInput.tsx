import { type ChangeEvent, type ClipboardEvent, type FocusEvent, type InputHTMLAttributes, type KeyboardEvent } from 'react';

/** Цифры номера после +7: не более 10, код страны 7/8 из начала отброшен. Пустое и «+7 » — без цифр. */
export function phoneDigits(raw: string): string {
  let body = raw;
  if (body.startsWith('+7')) body = body.slice(2);
  else if (/^7[ ()]/.test(body)) body = body.slice(1); // префикс без «+»: его стёрли выделением
  let digits = body.replace(/\D/g, '');
  if (digits.length > 10 && (digits[0] === '7' || digits[0] === '8')) digits = digits.slice(1);
  return digits.slice(0, 10);
}

/** Маска «+7 (999) 123-45-67» из цифр после +7; для 0 цифр — пустой префикс «+7 ». */
export function formatPhone(digits: string): string {
  if (!digits) return '+7 ';
  let out = `+7 (${digits.slice(0, 3)}`;
  if (digits.length > 3) out += `) ${digits.slice(3, 6)}`;
  if (digits.length > 6) out += `-${digits.slice(6, 8)}`;
  if (digits.length > 8) out += `-${digits.slice(8, 10)}`;
  return out;
}

// Позиция цифры с индексом index в показанном значении: до скобки, в скобках, после дефисов.
function digitPosition(index: number): number {
  return index < 3 ? 4 + index : index < 6 ? 6 + index : index < 8 ? 7 + index : 8 + index;
}
// Каретка за которой в поле count цифр; без цифр — конец префикса «+7 ».
function caretAfterCount(count: number): number {
  return count === 0 ? 3 : digitPosition(count - 1) + 1;
}

export interface PhoneInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'> {
  /** Отформатированное значение: пустая строка или «+7 …». */
  value: string;
  onChange: (value: string) => void;
}

/** Телефон рекламодателя по маске «+7 (999) 123-45-67»: префикс сам появляется при фокусе и не стирается,
 *  вставка с кодом 7/8 или 8 приводится к +7, курсор при стирании через скобку и дефис удаляет цифру. */
export function PhoneInput({ value, onChange, onFocus, onKeyDown, onPaste, ...props }: PhoneInputProps) {
  function change(event: ChangeEvent<HTMLInputElement>) {
    const input = event.target;
    const caret = input.selectionStart ?? input.value.length;
    const digits = phoneDigits(input.value);
    // Полностью стёрли — поле пустое; иначе префикс держится даже без цифр.
    const shown = input.value === '' ? '' : formatPhone(digits);
    const before = phoneDigits(input.value.slice(0, caret)).length;
    input.value = shown;
    input.setSelectionRange(caretAfterCount(before), caretAfterCount(before));
    if (shown !== value) onChange(shown);
  }
  function keydown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== 'Backspace' && event.key !== 'Delete') return;
    const input = event.currentTarget;
    // Выделенный кусок стирает сам браузер, а change переформатирует остаток.
    if (input.selectionStart !== input.selectionEnd) return;
    const caret = input.selectionStart ?? 0;
    const digits = phoneDigits(input.value);
    const before = phoneDigits(input.value.slice(0, caret)).length;
    const removeAt = event.key === 'Backspace' ? before - 1 : before;
    if (removeAt < 0 || removeAt >= digits.length) {
      if (removeAt < 0) event.preventDefault(); // префикс «+7 » не стирается
      return;
    }
    event.preventDefault();
    const next = digits.slice(0, removeAt) + digits.slice(removeAt + 1);
    const shown = formatPhone(next);
    const caretAt = removeAt === 0 ? 4 : caretAfterCount(removeAt);
    input.value = shown;
    input.setSelectionRange(caretAt, caretAt);
    if (shown !== value) onChange(shown);
  }
  function paste(event: ClipboardEvent<HTMLInputElement>) {
    event.preventDefault();
    const digits = phoneDigits(event.clipboardData.getData('text'));
    if (!digits) return; // не номер — поле не трогаем
    const input = event.currentTarget;
    const shown = formatPhone(digits);
    input.value = shown;
    input.setSelectionRange(shown.length, shown.length);
    if (shown !== value) onChange(shown);
  }
  function focus(event: FocusEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    if (input.value === '') {
      input.value = '+7 ';
      input.setSelectionRange(3, 3);
      onChange('+7 ');
    }
    onFocus?.(event);
  }
  return <input {...props} type="tel" inputMode="tel" autoComplete="tel" value={value} onChange={change} onFocus={focus}
    onKeyDown={keydown} onPaste={paste} />;
}
