import { useEffect, useState } from 'react';
import { getDemoNow } from './demo';
import { mockEnabled } from './screens';

/**
 * Текущее время бизнеса для «только что» и «N минут назад». В демо это демо-время с сервера:
 * после перемотки оно уходит от настоящего, поэтому сверяемся с сервером при каждом обновлении.
 */
export function useBusinessNow(): number | null {
  const [offset, setOffset] = useState<number | null>(mockEnabled ? 0 : null);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const controller = new AbortController();
    async function sync() {
      if (mockEnabled) { setNow(Date.now()); return; }
      // Без ответа оставляем прежний сдвиг, а если его ещё нет — считаем время настоящим:
      // предупреждение о давней проверке важнее точной подписи.
      const shift = await getDemoNow(controller.signal).then((value) => value === null ? 0 : Date.parse(value) - Date.now(), () => null);
      if (controller.signal.aborted) return;
      setOffset((previous) => shift ?? previous ?? 0); setNow(Date.now());
    }
    void sync();
    const timer = window.setInterval(() => void sync(), 30_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, []);
  return offset === null ? null : now + offset;
}
