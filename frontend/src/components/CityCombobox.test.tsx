// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it } from 'vitest';
import { CityCombobox, cityMatches } from './CityCombobox';

afterEach(cleanup);

const cities = ['Казань', 'Москва', 'Москва и Московская область', 'Орёл', 'Санкт-Петербург'];
let chosen = '';
function Field({ initial = '' }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  chosen = value;
  return <CityCombobox label="Город" cities={cities} value={value} onChange={setValue} />;
}
const input = () => screen.getByRole('combobox', { name: 'Город' });
const options = () => screen.queryAllByRole('option').map((item) => item.textContent);

it('подсказывает города по началу названия, затем по совпадению внутри, без учёта регистра и ё', () => {
  expect(cityMatches(cities, 'моск')).toEqual(['Москва', 'Москва и Московская область']);
  expect(cityMatches(cities, 'орел')).toEqual(['Орёл']);
  expect(cityMatches(cities, 'петер')).toEqual(['Санкт-Петербург']);
  expect(cityMatches(cities, ' ')).toEqual(cities);
});

it('при вводе показывает подсказки, клик выбирает город и закрывает список', () => {
  render(<Field />);
  expect(input().getAttribute('aria-expanded')).toBe('false');
  fireEvent.change(input(), { target: { value: 'Моск' } });
  expect(input().getAttribute('aria-expanded')).toBe('true');
  expect(options()).toEqual(['Москва', 'Москва и Московская область']);
  fireEvent.click(screen.getByRole('option', { name: 'Москва' }));
  expect(chosen).toBe('Москва');
  expect(input().getAttribute('aria-expanded')).toBe('false');
});

it('стрелки ходят по подсказкам, Enter выбирает и не отправляет форму, Esc закрывает', () => {
  render(<Field />);
  fireEvent.change(input(), { target: { value: 'моск' } });
  fireEvent.keyDown(input(), { key: 'ArrowDown' });
  fireEvent.keyDown(input(), { key: 'ArrowDown' });
  const active = screen.getByRole('option', { name: 'Москва и Московская область' });
  expect(active.getAttribute('aria-selected')).toBe('true');
  expect(input().getAttribute('aria-activedescendant')).toBe(active.id);
  fireEvent.keyDown(input(), { key: 'ArrowDown' });
  expect(screen.getByRole('option', { name: 'Москва' }).getAttribute('aria-selected')).toBe('true');
  expect(fireEvent.keyDown(input(), { key: 'Enter' })).toBe(false);
  expect(chosen).toBe('Москва');
  fireEvent.keyDown(input(), { key: 'ArrowUp' });
  expect(input().getAttribute('aria-expanded')).toBe('true');
  fireEvent.keyDown(input(), { key: 'Escape' });
  expect(input().getAttribute('aria-expanded')).toBe('false');
  expect(chosen).toBe('Москва');
});

it('без совпадений говорит, что такого города нет', () => {
  render(<Field />);
  fireEvent.change(input(), { target: { value: 'Урюпинск' } });
  expect(options()).toEqual([]);
  expect(screen.getByRole('status').textContent).toMatch('нет в списке');
});

it('сохранённый город вне списка показывается как есть', () => {
  render(<Field initial="Урюпинск" />);
  expect((input() as HTMLInputElement).value).toBe('Урюпинск');
});
