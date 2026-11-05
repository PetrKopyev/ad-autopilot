// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it } from 'vitest';
import { PhoneInput, phoneDigits } from './PhoneInput';

afterEach(cleanup);

let value = '';
function Field({ initial = '' }: { initial?: string }) {
  const [state, setState] = useState(initial);
  value = state;
  return <label>Телефон<PhoneInput value={state} onChange={setState} /></label>;
}
const input = () => screen.getByLabelText('Телефон') as HTMLInputElement;
// Как браузер: новое значение и курсор приходят вместе, затем событие ввода.
function typeAt(next: string, caret: number) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input(), next);
  input().setSelectionRange(caret, caret);
  fireEvent.input(input());
}
// Как userEvent.type: по клавише — знак встаёт у курсора в то, что сейчас показано в поле.
function typeKeys(text: string) {
  for (const key of text) {
    const at = input().selectionStart ?? input().value.length;
    typeAt(input().value.slice(0, at) + key + input().value.slice(at), at + 1);
  }
}
const backspace = () => fireEvent.keyDown(input(), { key: 'Backspace' });
const paste = (text: string) => fireEvent.paste(input(), { clipboardData: { getData: () => text } });

it('набор по цифре: +7, скобки и дефисы появляются сами', () => {
  render(<Field />);
  typeKeys('9991234567');
  expect(input().value).toBe('+7 (999) 123-45-67');
  expect(value).toBe('+7 (999) 123-45-67');
  expect(input().selectionStart).toBe(18);
});

it('первая цифра сама ставит префикс: с фокусом и без', () => {
  render(<Field />);
  fireEvent.focus(input());
  expect(input().value).toBe('+7 ');
  expect(value).toBe('+7 ');
  expect(input().selectionStart).toBe(3);
  cleanup();
  render(<Field />);
  typeKeys('9');
  expect(input().value).toBe('+7 (9');
});

it('набор с восьмёркой или семёркой: 11 цифр приводятся к +7', () => {
  render(<Field />);
  typeKeys('89991234567');
  expect(input().value).toBe('+7 (999) 123-45-67');
  cleanup();
  render(<Field />);
  typeKeys('79991234567');
  expect(input().value).toBe('+7 (999) 123-45-67');
});

it('вставка целиком: 89991234567, +7 999 123-45-67 и 79991234567', () => {
  render(<Field />);
  paste('89991234567');
  expect(input().value).toBe('+7 (999) 123-45-67');
  cleanup();
  render(<Field />);
  paste('+7 999 123-45-67');
  expect(input().value).toBe('+7 (999) 123-45-67');
  expect(input().selectionStart).toBe(18);
  cleanup();
  render(<Field />);
  paste('79991234567');
  expect(input().value).toBe('+7 (999) 123-45-67');
});

it('вставка мусора не меняет поле', () => {
  render(<Field initial="+7 (999) 123-45-67" />);
  paste('вот номер');
  expect(input().value).toBe('+7 (999) 123-45-67');
});

it('лишние цифры обрезаются', () => {
  render(<Field />);
  paste('999123456789');
  expect(input().value).toBe('+7 (999) 123-45-67');
  cleanup();
  render(<Field />);
  typeKeys('99912345678');
  expect(input().value).toBe('+7 (999) 123-45-67');
});

it('буквы не вводятся и не сдвигают курсор', () => {
  render(<Field />);
  typeKeys('9a9');
  expect(input().value).toBe('+7 (99');
  expect(input().selectionStart).toBe(6);
});

it('Backspace после дефиса стирает цифру, а не разделитель', () => {
  render(<Field initial="+7 (999) 123-45-67" />);
  input().setSelectionRange(13, 13);
  backspace();
  expect(input().value).toBe('+7 (999) 124-56-7');
  expect(input().selectionStart).toBe(11);
  expect(value).toBe('+7 (999) 124-56-7');
});

it('Backspace через скобку стирает цифру', () => {
  render(<Field initial="+7 (999) 123-45-67" />);
  input().setSelectionRange(8, 8);
  backspace();
  expect(input().value).toBe('+7 (991) 234-56-7');
  expect(input().selectionStart).toBe(6);
});

it('префикс +7 не стирается', () => {
  render(<Field initial="+7 (99" />);
  input().setSelectionRange(3, 3);
  expect(backspace()).toBe(false);
  expect(input().value).toBe('+7 (99');
  input().setSelectionRange(1, 1);
  expect(backspace()).toBe(false);
  expect(input().value).toBe('+7 (99');
});

it('последнюю цифру стереть можно: остаётся пустой префикс', () => {
  render(<Field initial="+7 (9" />);
  input().setSelectionRange(6, 6);
  backspace();
  expect(input().value).toBe('+7 ');
  expect(value).toBe('+7 ');
});

it('курсор не прыгает в конец при правке в середине', () => {
  render(<Field initial="+7 (999) 123-45-67" />);
  input().setSelectionRange(5, 5);
  typeKeys('1');
  expect(input().value).toBe('+7 (919) 912-34-56');
  expect(input().selectionStart).toBe(6);
  expect(value).toBe('+7 (919) 912-34-56');
});

it('очистка выделением возвращает к пустому полю', () => {
  render(<Field initial="+7 (999) 123-45-67" />);
  typeAt('', 0);
  expect(input().value).toBe('');
  expect(value).toBe('');
});

it('phoneDigits: 10 цифр после +7, код страны отброшен', () => {
  expect(phoneDigits('+7 (999) 123-45-67')).toBe('9991234567');
  expect(phoneDigits('89991234567')).toBe('9991234567');
  expect(phoneDigits('79991234567')).toBe('9991234567');
  expect(phoneDigits('+7 999 123-45-67')).toBe('9991234567');
  expect(phoneDigits('999123456789')).toBe('9991234567');
  expect(phoneDigits('')).toBe('');
  expect(phoneDigits('+7 ')).toBe('');
  expect(phoneDigits('+7 (999')).toBe('999');
});

it('выделить весь номер и нажать Backspace — поле очищается', () => {
  render(<Field initial="+7 (999) 123-45-67" />);
  input().setSelectionRange(0, 18);
  const event = fireEvent.keyDown(input(), { key: 'Backspace' });
  expect(event).toBe(true); // не отменено: выделенное стирает браузер
  typeAt('', 0);
  expect(value).toBe('');
});

it('выделенный кусок номера стирается целиком, а не одна цифра перед ним', () => {
  render(<Field initial="+7 (999) 123-45-67" />);
  input().setSelectionRange(9, 12);
  expect(fireEvent.keyDown(input(), { key: 'Backspace' })).toBe(true);
  typeAt('+7 (999) -45-67', 9);
  expect(value).toBe('+7 (999) 456-7');
});
