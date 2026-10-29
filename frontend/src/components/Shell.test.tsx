// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { AdStatusBadge, AppNav, BudgetRing, formatMoney, Field, MetricTile, PageHeader, PanelCard, PillSteps, PlatformBadge, SegmentedControl, Sparkline, describedBy } from './index';

afterEach(cleanup);

it('статус объявления — словами из copy.status и тоном светофора', () => {
  render(<><AdStatusBadge status="moderation" /><AdStatusBadge status="paused_auto" reason="дорого" /><AdStatusBadge status="rejected" /></>);
  expect(screen.getByText(copy.status.moderation).className).toContain('status-badge--warning');
  expect(screen.getByText(`${copy.status.stoppedAuto}: дорого`).className).toContain('status-badge--neutral');
  expect(screen.getByText(copy.status.rejected).className).toContain('status-badge--danger');
  expect(document.body.textContent).not.toMatch(/Проверяем/);
});

it('спарклайн рвёт линию на днях без данных и не рисует тренд из одной точки', () => {
  const { container, rerender } = render(<Sparkline values={[1, 2, null, 3, 4]} label="Обращения по дням" />);
  expect(container.querySelectorAll('polyline')).toHaveLength(2);
  expect(screen.getByRole('img', { name: 'Обращения по дням' })).toBeTruthy();
  rerender(<Sparkline values={[null, 5, null]} />);
  expect(container.querySelector('svg')).toBeNull();
});

it('переключатель отмечает выбранное и сообщает о выборе', () => {
  const change = vi.fn();
  render(<SegmentedControl label={copy.autopilotMode.label} value="working" onChange={change}
    options={[{ value: 'working', label: copy.autopilotMode.working }, { value: 'paused', label: copy.autopilotMode.paused }]} />);
  const group = screen.getByRole('group', { name: copy.autopilotMode.label });
  expect(group.querySelector('[aria-pressed=true]')!.textContent).toBe(copy.autopilotMode.working);
  fireEvent.click(screen.getByRole('button', { name: copy.autopilotMode.paused }));
  expect(change).toHaveBeenCalledWith('paused');
});

it('шаги-таблетки: пройденные — ссылки, текущий отмечен', () => {
  render(<MemoryRouter><PillSteps label={copy.setup.title} current={1} steps={[{ label: 'О бизнесе', to: '/setup/1' }, { label: 'Кабинеты' }, { label: 'План' }]} /></MemoryRouter>);
  expect(screen.getByRole('link', { name: `${copy.common.completedStep}: О бизнесе` }).getAttribute('href')).toBe('/setup/1');
  expect(screen.getByText('Кабинеты').closest('li')!.getAttribute('aria-current')).toBe('step');
  expect(screen.getAllByRole('link')).toHaveLength(1);
});

it('плитка, панель, шапка, площадка, кольцо и поле собираются с доступными именами', () => {
  render(<MemoryRouter>
    <PageHeader title="Главная" description="Студия · Москва" actions={<button type="button">Действие</button>} />
    <PanelCard title="Кампании" titleId="campaigns"><MetricTile label="Обращения" value="96" hint="заявки и звонки" /></PanelCard>
    <PlatformBadge platform="vk" />
    <BudgetRing spentKopecks={4_318_000} budgetKopecks={5_000_000} />
    <Field id="clients" label="Сколько клиентов" hint="Целое число" error="Введите число"><input id="clients" className="input" aria-describedby={describedBy('clients', 1, 1)} /></Field>
  </MemoryRouter>);
  expect(screen.getByRole('heading', { level: 1, name: 'Главная' })).toBeTruthy();
  expect(screen.getByRole('region', { name: 'Кампании' }).textContent).toContain('96');
  expect(screen.getByText(copy.screens.platforms.vk)).toBeTruthy();
  expect(screen.getByRole('group', { name: copy.budget.label(formatMoney(4_318_000), formatMoney(5_000_000)) }).textContent).toContain('86%');
  expect(screen.getByRole('textbox', { name: 'Сколько клиентов' }).getAttribute('aria-describedby')).toBe('clients-hint clients-error');
});

it('до запуска вместо меню — подпись; после — шесть разделов и выход', () => {
  const signOut = vi.fn();
  const { rerender } = render(<MemoryRouter><AppNav login="owner" locked busy={false} onSignOut={signOut} /></MemoryRouter>);
  expect(screen.queryByRole('navigation')).toBeNull();
  expect(screen.getByText(copy.navigation.locked)).toBeTruthy();
  rerender(<MemoryRouter><AppNav login="owner" locked={false} busy={false} onSignOut={signOut} /></MemoryRouter>);
  expect(screen.getByRole('navigation', { name: copy.navigation.label }).querySelectorAll('a')).toHaveLength(6);
  expect(screen.getByRole('link', { name: copy.navigation.home }).getAttribute('aria-current')).toBe('page');
  fireEvent.click(screen.getByRole('button', { name: copy.launchFlow.logout }));
  expect(signOut).toHaveBeenCalled();
});
