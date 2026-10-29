// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { Sparkline } from './Sparkline';

afterEach(cleanup);

const points = (container: HTMLElement) => [...container.querySelectorAll('polyline')].map((line) => line.getAttribute('points')!);

it('без двух известных точек графика нет', () => {
  for (const values of [[], [5], [null, null], [null, 3, null]]) {
    const { container } = render(<Sparkline values={values} />);
    expect(container.querySelector('svg')).toBeNull();
    cleanup();
  }
});

it('ровный ряд из нулей — линия посередине, без NaN', () => {
  const { container } = render(<Sparkline values={[0, 0, 0]} />);
  expect(points(container)).toEqual(['0.00,15.00 50.00,15.00 100.00,15.00']);
});

it('пропуск рвёт линию, а с connect соединяет соседние точки', () => {
  const values = [1, null, 3, 2];
  const { container } = render(<Sparkline values={values} />);
  expect(points(container)).toHaveLength(1);
  expect(points(container)[0]).not.toContain('NaN');
  cleanup();
  const joined = render(<Sparkline values={values} connect />).container;
  expect(points(joined)).toHaveLength(1);
  expect(points(joined)[0].split(' ')).toHaveLength(3);
});
