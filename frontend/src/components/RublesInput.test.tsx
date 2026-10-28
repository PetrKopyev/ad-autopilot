// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it } from 'vitest';
import { RublesInput } from './RublesInput';

afterEach(cleanup);

let digits = '';
function Field({ initial = '' }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  digits = value;
  return <label>Бюджет<RublesInput max={10_000_000} value={value} onChange={setValue} /></label>;
}
const input = () => screen.getByLabelText('Бюджет') as HTMLInputElement;
// Как браузер: новое значение и курсор приходят вместе, затем событие ввода.
function typeAt(value: string, caret: number) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input(), value);
  input().setSelectionRange(caret, caret);
  fireEvent.input(input());
}

it('оставляет только цифры и делит разряды пробелом', () => {
  render(<Field />);
  fireEvent.change(input(), { target: { value: '50000 фыв' } });
  expect(input().value).toBe('50 000');
  expect(digits).toBe('50000');
  expect(input().inputMode).toBe('numeric');
});

it('вставка с пробелами, буквами и ведущими нулями даёт число', () => {
  render(<Field />);
  fireEvent.change(input(), { target: { value: '00 1 234 567 руб.' } });
  expect(input().value).toBe('1 234 567');
  expect(digits).toBe('1234567');
});

it('буква посередине не сдвигает курсор, новая цифра — ставит его за собой', () => {
  render(<Field initial="50000" />);
  typeAt('50ы 000', 3);
  expect(input().value).toBe('50 000');
  expect(input().selectionStart).toBe(2);
  typeAt('501 000', 3);
  expect(input().value).toBe('501 000');
  expect(input().selectionStart).toBe(3);
  typeAt('5011 000', 4);
  expect(input().value).toBe('5 011 000');
  expect(input().selectionStart).toBe(5);
});

it('сумму больше предела не принимает', () => {
  render(<Field initial="9999999" />);
  fireEvent.change(input(), { target: { value: '99999999' } });
  expect(digits).toBe('9999999');
  expect(input().value).toBe('9 999 999');
  fireEvent.change(input(), { target: { value: '10000000' } });
  expect(digits).toBe('10000000');
});
