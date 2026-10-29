import { useEffect, useState } from 'react';
import { getAuthConfig } from './setup';

/** Демо ли это: в демо время идёт только по перемотке, а кабинетов площадок, с которыми можно сверяться, нет. */
export function useDemo(): boolean {
  const [demo, setDemo] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void getAuthConfig(controller.signal).then((config) => { if (!controller.signal.aborted) setDemo(config.demo); }).catch(() => {});
    return () => controller.abort();
  }, []);
  return demo;
}
