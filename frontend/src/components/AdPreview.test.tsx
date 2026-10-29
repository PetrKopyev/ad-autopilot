// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { AdPreview } from './AdPreview';

afterEach(cleanup);

it('Директ — строка выдачи с доменом сайта, без картинки', () => {
  render(<AdPreview platform="direct" title="Маникюр у дома" text="Запись онлайн" site="https://www.studio.ru/price" image={{ url: '/a.jpg', alt: 'Руки' }} />);
  expect(screen.getByText('Реклама · studio.ru')).toBeTruthy();
  expect(screen.getByText('Маникюр у дома')).toBeTruthy();
  expect(screen.queryByRole('img')).toBeNull();
  expect(screen.getByText('Примерный вид: площадка может показать немного иначе.')).toBeTruthy();
});

it('ВКонтакте — карточка ленты с картинкой; без картинки и сайта — подсказки вместо них', () => {
  const { rerender } = render(<AdPreview platform="vk" title="Педикюр" text="К выходным" site="https://studio.ru" image={{ url: '/a.jpg', alt: 'Руки' }} />);
  expect(screen.getByRole('img', { name: 'Руки' })).toBeTruthy();
  rerender(<AdPreview platform="vk" title="Педикюр" text="К выходным" />);
  expect(screen.getByText('Отметьте картинку — она появится здесь')).toBeTruthy();
  expect(screen.getAllByText('Ваш сайт').length).toBeGreaterThan(0);
});
