import { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import { Button, Card, ErrorBanner } from '../components';
import { connectAccount, demoLogin, getAuthConfig } from '../api/setup';
import type { StopResult } from '../api/setup';
import { mockEnabled } from '../api/screens';
import { useSession } from '../api/session';
import { copy } from '../copy';
import { StopNotice } from './SetupShared';
import './Setup.css';

export function LoginPage() {
  const navigate = useNavigate();
  const session = useSession();
  const stopResult = (useLocation().state as { stopResult?: StopResult } | null)?.stopResult;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [demo, setDemo] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void getAuthConfig(controller.signal).then((config) => {
      if (!controller.signal.aborted) setDemo(config.demo === true);
    }).catch(() => {});
    return () => controller.abort();
  }, []);
  async function loginDemo() {
    setBusy(true); setError('');
    try { await demoLogin(); session.reload(); navigate('/setup'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  async function login() {
    setBusy(true); setError('');
    try { await connectAccount('login'); if (mockEnabled) { session.reload(); navigate('/setup'); } }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  return <div className="page-stack launch-flow">
    <h1>{copy.login.title}</h1>
    {stopResult && <StopNotice result={stopResult} />}
    <Card className="page-stack">
      <p>{copy.login.description}</p>
      {error && <ErrorBanner message={error} />}
      <div className="actions">
        <Button disabled={busy} onClick={() => void login()}>{copy.login.action}</Button>
        {demo && <Button disabled={busy} onClick={() => void loginDemo()}>Войти в демо</Button>}
      </div>
      <p className="muted">{copy.login.consent} <Link to="/policy">{copy.login.policy}</Link></p>
    </Card>
  </div>;
}
