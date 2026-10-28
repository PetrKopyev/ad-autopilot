import { expect, test } from '@playwright/test';
import { copy } from '../src/copy';

test.skip(!process.env.BASE_URL, 'Вручную: BASE_URL=https://localhost npm run e2e; стенд должен быть запущен.');

for (const width of [390, 1280]) {
  test(`демо: мастер, 30 дней автопилота, откат и пауза (${width}px)`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    await page.goto('/login');
    await expect(page.getByRole('heading', { name: copy.login.title, exact: true })).toBeVisible();
    await expect(page.getByRole('navigation', { name: copy.navigation.label })).toHaveCount(0);
    await page.getByRole('button', { name: 'Войти в демо', exact: true }).click();
    await expect(page.getByLabel(copy.setup.business.website)).toHaveValue('https://salon.example/');
    await expect(page.getByLabel(copy.setup.business.city, { exact: true })).toHaveValue('Москва');
    await expect(page.getByLabel(copy.setup.business.budget)).toHaveValue('50\u00a0000');
    await page.getByRole('button', { name: copy.common.next, exact: true }).click();
    await expect(page).toHaveURL(/\/setup\/2$/);
    await page.getByRole('checkbox', { name: 'Заявка', exact: true }).check();
    await page.getByRole('button', { name: copy.common.next, exact: true }).click();
    await expect(page).toHaveURL(/\/setup\/3$/);
    await expect(page.getByRole('button', { name: copy.common.next, exact: true })).toBeEnabled();
    await page.getByRole('button', { name: copy.common.next, exact: true }).click();
    await expect(page).toHaveURL(/\/setup\/4$/);
    await expect(page.getByLabel(copy.setup.draft.services)).not.toHaveValue('');
    await page.getByRole('checkbox', { name: copy.launchFlow.approved }).check();
    await page.getByRole('button', { name: copy.common.next, exact: true }).click();
    await expect(page).toHaveURL(/\/setup\/5$/);
    await page.getByLabel(copy.setup.advertiser.taxId, { exact: true }).fill('526317984689');
    await page.getByLabel(copy.launchFlow.fullName, { exact: true }).fill('Демонстрационный салон');
    await page.getByLabel(copy.launchFlow.phone, { exact: true }).fill('+7 900 123-45-67');
    await page.getByRole('checkbox', { name: copy.launchFlow.confirmDirect }).check();
    await page.getByRole('button', { name: copy.plan.title, exact: true }).click();
    await expect(page).toHaveURL(/\/plan$/);
    await page.getByRole('button', { name: copy.plan.launch, exact: true }).click();
    await page.getByRole('link', { name: copy.launchFlow.openHome, exact: true }).click();
    await expect(page.getByText(/^Работает,/)).toBeVisible();
    const advance = page.waitForResponse((response) => response.url().endsWith('/api/demo/advance') && response.request().method() === 'POST');
    await page.getByRole('button', { name: '+30 дней', exact: true }).click();
    expect((await advance).status()).toBe(202);
    await expect(page.getByText(/Автопилот обрабатывает прошедшие дни: \d+ из 30/)).toBeVisible();
    const started = Date.now();
    await expect(page.getByText(/^Готово\. Демо-дата: .*Перемотано 30 из 120 дней/)).toBeVisible({ timeout: 240_000 });
    console.log(`+30 дней (${width}px): ${((Date.now() - started) / 1000).toFixed(1)} с`);
    const budget = page.locator('.budget-ring');
    await expect(budget).toBeVisible();
    const numbers = (await budget.getAttribute('aria-label'))!.match(/[\d\s\u00a0\u202f]+(?:[,.]\d+)?/g)!
      .map((value) => Number(value.replace(/[\s\u00a0\u202f]/g, '').replace(',', '.'))).filter((value) => value > 0);
    expect(numbers.length).toBeGreaterThanOrEqual(2);
    expect(numbers[0]).toBeLessThanOrEqual(numbers[1]);
    expect(numbers[1]).toBe(50000);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await testInfo.attach(`home-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });

    await page.getByRole('navigation').getByRole('link', { name: copy.navigation.ads, exact: true }).click();
    await expect(page.getByText(/Остановлено нами/).first()).toBeVisible();
    await page.getByRole('navigation').getByRole('link', { name: copy.navigation.history, exact: true }).click();
    await page.getByLabel(copy.historyScreen.filterAuthor).selectOption('autopilot');
    await expect(page.locator('.history-entry').first()).toContainText('Автопилот');
    // Отмена остановки подписана «Продолжить», остальных изменений — «Отменить».
    const labels = copy.historyScreen;
    await page.locator('.history-entry').getByRole('button', { name: new RegExp(`^(${copy.history.undo}|${labels.resume})$`) }).first().click();
    await page.getByRole('button', { name: new RegExp(`^(${labels.confirmYes}|${labels.resumeYes})$`) }).click();
    await page.getByLabel(copy.historyScreen.filterAuthor).selectOption('');
    await expect(page.getByRole('heading', { name: new RegExp(`^(Вы отменили|${labels.resumedAfterStop})`) }).first()).toBeVisible();

    await page.getByRole('navigation').getByRole('link', { name: copy.navigation.home, exact: true }).click();
    await page.getByRole('button', { name: copy.home.stopAll, exact: true }).click();
    await page.getByRole('button', { name: copy.screens.confirmStop, exact: true }).click();
    await expect(page.getByText(copy.home.paused, { exact: true })).toBeVisible();
    await page.getByRole('button', { name: copy.settings.resume, exact: true }).click();
    await expect(page.getByText(/^Работает,/)).toBeVisible();

    const consoleErrors: string[] = [];
    page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });
    await page.getByRole('navigation').getByRole('link', { name: copy.navigation.settings, exact: true }).click();
    await expect(page.getByRole('heading', { name: copy.settings.title, exact: true })).toBeVisible();
    const budgetField = page.getByLabel(copy.settingsScreen.budgetLabel);
    await expect(budgetField).toHaveValue('50\u00a0000');
    await expect(page.getByRole('textbox', { name: new RegExp(`^${copy.settingsScreen.description.services}`) })).not.toHaveValue('');
    await expect(page.getByText(copy.settingsScreen.markingStored('•••4689', '+7•••4567'))).toBeVisible();
    await budgetField.fill('55000');
    const saved = page.waitForResponse((response) => response.url().endsWith('/api/business/budget') && response.request().method() === 'PUT');
    await page.getByRole('button', { name: copy.settingsScreen.save, exact: true }).first().click();
    expect((await saved).status()).toBe(200);
    await expect(page.getByRole('status').filter({ hasText: copy.settingsScreen.saved })).toBeVisible();
    await page.reload();
    await expect(page.getByLabel(copy.settingsScreen.budgetLabel)).toHaveValue('55\u00a0000');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await testInfo.attach(`settings-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
    expect(consoleErrors).toEqual([]);

    for (const name of [copy.navigation.home, copy.navigation.ads, copy.navigation.statistics, copy.navigation.history, copy.navigation.clients, copy.navigation.settings]) {
      await page.getByRole('navigation').getByRole('link', { name, exact: true }).click();
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      await expect(page.getByRole('status').filter({ hasText: copy.screens.loading })).toHaveCount(0);
    }
    for (const name of [copy.service.policyTitle, copy.setup.accounts.vk, copy.service.errorsTitle]) {
      await page.getByRole('contentinfo').getByRole('link', { name, exact: true }).click();
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    }
    await page.goto('/plan');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    expect(pageErrors).toEqual([]);
  });
}
