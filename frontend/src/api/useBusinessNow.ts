import { useEffect, useState } from 'react';
import { getDemoDate } from './demo';
import { mockEnabled } from './screens';

/** Текущее время бизнеса для «только что» и «N минут назад». В демо сервер отдаёт только дату, поэтому null. */
export function useBusinessNow(): number | null {
  const [demo, setDemo] = useState<boolean | undefined>(mockEnabled ? false : undefined);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (mockEnabled) return;
    const controller = new AbortController();
    // Без ответа о демо считаем время настоящим: предупреждение о давней проверке важнее точной подписи.
    getDemoDate(controller.signal).then((date) => date !== null, () => false)
      .then((demo) => { if (!controller.signal.aborted) setDemo(demo); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  return demo === false ? now : null;
}
