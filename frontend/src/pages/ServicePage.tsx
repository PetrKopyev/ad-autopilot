import { useState } from 'react';
import { Link } from 'react-router';
import { Button, Card, ErrorBanner } from '../components';
import { connectAccount, connectVkKey, logout } from '../api/setup';
import { useSession } from '../api/session';
import { copy } from '../copy';
import './Setup.css';

function businessId(): number | undefined {
  try { return JSON.parse(sessionStorage.getItem('ad-autopilot-setup-draft') ?? '{}').business?.id as number | undefined; }
  catch { return undefined; }
}
export function ServiceVkPage() {
  const me = useSession();
  const [clientId, setClientId] = useState('');
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [connected, setConnected] = useState(false);
  const id = businessId();
  async function connect(manual: boolean) {
    if (busy) return; setBusy(true); setError('');
    try {
      if (!id || manual && (!clientId.trim() || !secret.trim())) throw new Error(copy.launchFlow.keyMissing);
      if (manual) { await connectVkKey(clientId.trim(), secret.trim(), id); setConnected(true); }
      else await connectAccount('vk', id);
      me.reload();
    } catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setSecret(''); setBusy(false); }
  }
  return <div className="page-stack launch-flow"><h1>{copy.service.vkTitle}</h1>
    {(error || me.error) && <ErrorBanner message={error || me.error!} />}
    {(connected || me.data?.accounts.some((account) => account.platform === 'vk' && account.status === 'ok')) && <p role="status">{copy.launchFlow.vkConnected}</p>}
    {!id && <p>{copy.launchFlow.needBusiness}</p>}
    <Card className="page-stack"><h2>{copy.launchFlow.vkConnect}</h2><div className="actions"><Button disabled={busy || !id} onClick={() => void connect(false)}>{copy.launchFlow.vkConnect}</Button></div>
      <details><summary>{copy.setup.accounts.create}</summary><ol>{copy.launchFlow.createVk.map((text) => <li key={text}>{text}</li>)}</ol></details>
    </Card>
    <Card><form className="page-stack" onSubmit={(event) => { event.preventDefault(); void connect(true); }}>
      <h2>{copy.setup.accounts.keyTab}</h2><ol>{copy.launchFlow.keySteps.map((text) => <li key={text}>{text}</li>)}</ol>
      <label className="field">{copy.launchFlow.clientIdLabel}<input autoComplete="off" required disabled={busy || !id} value={clientId} onChange={(event) => setClientId(event.target.value)} /></label>
      <label className="field">{copy.launchFlow.keyLabel}<input type="password" autoComplete="off" required disabled={busy || !id} value={secret} aria-describedby="key-hint" onChange={(event) => setSecret(event.target.value)} /></label>
      <p className="muted" id="key-hint">{copy.launchFlow.keyHint}</p><div className="actions"><Button type="submit" disabled={busy || !id}>{copy.launchFlow.keyConnect}</Button></div>
    </form></Card><Link to="/setup/2">{copy.common.back}</Link>
  </div>;
}
export function ServicePolicyPage() {
  const me = useSession();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <div className="page-stack launch-flow"><h1>{copy.service.policyTitle}</h1>
    <Card className="page-stack">{copy.launchFlow.policy.map((text) => <p key={text}>{text}</p>)}<p className="muted">{copy.launchFlow.policyNotice}</p></Card>
    {error && <ErrorBanner message={error} />}
    {me.data && <Card className="page-stack"><p>{copy.launchFlow.signedIn(me.data.login)}</p><div className="actions"><Button disabled={busy} variant="secondary" onClick={() => {
      setBusy(true); setError('');
      void logout().then(() => me.reload()).catch(() => setError(copy.errors.network)).finally(() => setBusy(false));
    }}>{copy.launchFlow.logout}</Button></div></Card>}
    {me.data === null && <Link to="/login">{copy.login.title}</Link>}
  </div>;
}
export function ServiceErrorPage() {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <div className="page-stack launch-flow"><h1>{copy.service.errorsTitle}</h1>
    {error && <ErrorBanner message={error} />}
    <Card className="page-stack"><p>{copy.launchFlow.reauth}</p><div className="actions"><Button disabled={busy} onClick={() => {
      setBusy(true); void connectAccount('direct', businessId()).catch(() => setError(copy.errors.network)).finally(() => setBusy(false));
    }}>{copy.setup.accounts.connectDirect}</Button><Link className="button button--secondary" to="/connect/vk">{copy.launchFlow.vkConnect}</Link></div></Card>
    <Card className="page-stack"><p>{copy.launchFlow.lowBalance}</p><div className="actions"><a className="button button--secondary" href="https://direct.yandex.ru/" target="_blank" rel="noreferrer">{copy.launchFlow.openDirect}</a><a className="button button--secondary" href="https://ads.vk.com/" target="_blank" rel="noreferrer">{copy.launchFlow.openVk}</a></div></Card>
    <Card><p>{copy.launchFlow.platformUnavailable}</p></Card><Link to="/setup">{copy.common.start}</Link>
  </div>;
}
