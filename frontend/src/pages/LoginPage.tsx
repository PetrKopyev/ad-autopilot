import { useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { Button, Card, ErrorBanner } from '../components';
import { connectAccount } from '../api/setup';
import { mockEnabled } from '../api/screens';
import { useSession } from '../api/session';
import { copy } from '../copy';
import './Setup.css';

export function LoginPage() {
  const navigate = useNavigate();
  const session = useSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function login() {
    setBusy(true); setError('');
    try { await connectAccount('login'); if (mockEnabled) { session.reload(); navigate('/setup'); } }
    catch { setError(copy.errors.network); }
    finally { setBusy(false); }
  }
  return <div className="page-stack launch-flow">
    <h1>{copy.login.title}</h1>
    <Card className="page-stack">
      <p>{copy.login.description}</p>
      {error && <ErrorBanner message={error} />}
      <div className="actions"><Button disabled={busy} onClick={() => void login()}>{copy.login.action}</Button></div>
      <p className="muted">{copy.login.consent} <Link to="/policy">{copy.login.policy}</Link></p>
    </Card>
  </div>;
}
