import { useEffect, useRef, useState } from 'react';
import { Link, Navigate, useLocation } from 'react-router';
import { Button, Card, ErrorBanner } from '../components';
import { connectAccount, demoLogin, getAuthConfig } from '../api/setup';
import type { StopResult } from '../api/setup';
import { mockEnabled } from '../api/screens';
import { useSession } from '../api/session';
import { copy } from '../copy';
import { StopNotice } from './SetupShared';
import './Setup.css';

export function LoginPage() {
  const session = useSession();
  const stopResult = (useLocation().state as { stopResult?: StopResult } | null)?.stopResult;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [demo, setDemo] = useState(false);
  const [agreed, setAgreed] = useState(false);
  // Куда вести после входа. Переходим, только когда сессия перечитана: иначе раздел ещё видит гостя и вернёт на вход.
  const [next, setNext] = useState('/');
  // После удаления данных сюда приходят ещё со старой сессией и перечитывают её уже здесь: перечитанная раньше, она
  // увела бы из раздела на вход без итога остановки. С итогом остановки уводим, только когда сессия перечитана здесь:
  // после входа или если «назад» вернуло сюда уже вошедшего. Каждое чтение даёт новый объект, поэтому перечитанную
  // отличаем от той, с которой пришли, по ссылке: промежуточный рендер без сессии React может пропустить.
  const arrived = useRef(session.data);
  useEffect(() => { if (stopResult) session.reload(); }, [stopResult]);
  useEffect(() => {
    const controller = new AbortController();
    void getAuthConfig(controller.signal).then((config) => {
      if (!controller.signal.aborted) setDemo(config.demo === true);
    }).catch(() => {});
    return () => controller.abort();
  }, []);
  async function loginDemo() {
    setBusy(true); setError('');
    try { await demoLogin(); setNext('/setup'); session.reload(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  async function login() {
    setBusy(true); setError('');
    try { await connectAccount('login'); if (mockEnabled) { setNext('/setup'); session.reload(); } }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  if (session.data && (!stopResult || session.data !== arrived.current)) return <Navigate to={next} replace />;
  return <div className="page-stack launch-flow">
    <h1>{copy.login.title}</h1>
    {stopResult && <StopNotice result={stopResult} />}
    <Card className="page-stack">
      <p>{copy.login.description}</p>
      {error && <ErrorBanner message={error} />}
      <label className="check consent-check">
        <input type="checkbox" checked={agreed} onChange={(event) => setAgreed(event.target.checked)} />
        <span>{copy.login.consent} <Link to="/policy" target="_blank" rel="noopener">{copy.login.policy}</Link></span>
      </label>
      <div className="actions login-actions">
        <button type="button" className="yandex-id-button" disabled={busy || !agreed} onClick={() => void login()}>
          <svg className="yandex-id-button__logo" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <circle cx="12" cy="12" r="12" fill="#fc3f1d" />
            <path fill="#fff" d="M13.32 7.666h-.924c-1.694 0-2.585.858-2.585 2.123 0 1.43.616 2.1 1.881 2.959l1.045.704-3.003 4.487H7.49l2.695-4.014c-1.55-1.111-2.42-2.19-2.42-4.015 0-2.288 1.595-3.85 4.62-3.85h3.003v11.868H13.32V7.666z" />
          </svg>
          {copy.login.action}
        </button>
        {demo && <Button variant="secondary" disabled={busy || !agreed} onClick={() => void loginDemo()}>{copy.login.demo}</Button>}
      </div>
    </Card>
  </div>;
}
