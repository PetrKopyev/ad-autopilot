import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Button, ErrorBanner, Field, Icon, PageHeader, PanelCard, describedBy } from '../components';
import { connectAccount, connectVkKey, getAuthConfig, logout } from '../api/setup';
import { useSession } from '../api/session';
import { copy } from '../copy';
import './Service.css';

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
  const [oauth, setOauth] = useState(false);
  const id = businessId();
  useEffect(() => {
    const controller = new AbortController();
    void getAuthConfig(controller.signal).then((config) => { if (!controller.signal.aborted) setOauth(config.vk_oauth); }).catch(() => {});
    return () => controller.abort();
  }, []);
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
  if (connected) return <div className="page-stack service"><PageHeader title={copy.service.vkTitle} description={copy.service.vkDescription} />
    <section className="panel service-panel service-done">
      <span className="service-done__mark" aria-hidden="true"><Icon name="check" /></span>
      <p role="status"><strong>{copy.launchFlow.vkConnected}</strong></p><p className="muted">{copy.launchFlow.vkConnectedHint}</p>
      <div className="actions"><Link className="button button--primary" to="/setup/2">{copy.launchFlow.backToSetup}</Link></div>
    </section>
  </div>;
  const vkReady = me.data?.accounts.some((account) => account.platform === 'vk' && account.status === 'ok');
  return <div className="page-stack service"><PageHeader title={copy.service.vkTitle} description={copy.service.vkDescription} />
    {(error || me.error) && <ErrorBanner message={error || me.error!} />}
    {vkReady && <p className="service-ok" role="status"><Icon name="check" />{copy.launchFlow.vkConnected}</p>}
    {!id && <div className="notice"><p>{copy.launchFlow.needBusiness}</p></div>}
    <div className={`service-grid${oauth ? ' service-grid--aside' : ''}`}>
      <PanelCard title={copy.launchFlow.keyTitle} titleId="vk-key-title">
        <form className="page-stack" onSubmit={(event) => { event.preventDefault(); void connect(true); }}>
          <ol className="service-steps">{copy.launchFlow.keySteps.map((text) => <li key={text}>{text}</li>)}</ol>
          <Field id="vk-client-id" label={copy.launchFlow.clientIdLabel}>
            <input id="vk-client-id" className="input" autoComplete="off" required disabled={busy || !id} value={clientId} onChange={(event) => setClientId(event.target.value)} />
          </Field>
          <Field id="vk-secret" label={copy.launchFlow.keyLabel} hint={copy.launchFlow.keyHint}>
            <input id="vk-secret" className="input" type="password" autoComplete="off" required disabled={busy || !id} value={secret}
              aria-describedby={describedBy('vk-secret', copy.launchFlow.keyHint)} onChange={(event) => setSecret(event.target.value)} />
          </Field>
          <div className="actions"><Button type="submit" disabled={busy || !id}>{copy.launchFlow.keyConnect}</Button></div>
          <details className="service-details"><summary>{copy.setup.accounts.create}</summary>
            <ol className="service-steps">{copy.launchFlow.createVk.map((text) => <li key={text}>{text}</li>)}</ol></details>
        </form>
      </PanelCard>
      {oauth && <PanelCard title={copy.launchFlow.vkOauth} titleId="vk-oauth-title">
        <div className="page-stack"><p className="muted">{copy.launchFlow.vkOauthHint}</p>
          <div className="actions"><Button variant="secondary" disabled={busy || !id} onClick={() => void connect(false)}>{copy.launchFlow.vkOauth}</Button></div></div>
      </PanelCard>}
    </div>
    <Link className="service-back" to="/setup/2"><Icon name="back" />{copy.common.back}</Link>
  </div>;
}
export function ServicePolicyPage() {
  useEffect(() => {
    const previousTitle = document.title;
    document.title = copy.service.policyTitle;
    return () => { document.title = previousTitle; };
  }, []);
  const me = useSession();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <div className="page-stack service"><PageHeader title={copy.service.policyTitle} description={copy.service.policyDescription} />
    <article className="panel service-panel service-doc">{copy.launchFlow.policy.map((text) => <p key={text}>{text}</p>)}
      <p className="muted">{copy.launchFlow.policyNotice}</p></article>
    {error && <ErrorBanner message={error} />}
    {me.data && <section className="panel service-panel service-account" aria-label={copy.launchFlow.signedIn(me.data.login)}>
      <p>{copy.launchFlow.signedIn(me.data.login)}</p>
      <Button disabled={busy} variant="secondary" icon="logout" onClick={() => {
        setBusy(true); setError('');
        void logout().then(() => me.reload()).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : copy.errors.generic)).finally(() => setBusy(false));
      }}>{copy.launchFlow.logout}</Button></section>}
    {me.data === null && <Link className="button button--primary service-login" to="/login">{copy.login.title}</Link>}
  </div>;
}
export function ServiceErrorPage() {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <div className="page-stack service"><PageHeader title={copy.service.errorsTitle} description={copy.service.errorsDescription} />
    {error && <ErrorBanner message={error} />}
    <div className="service-issues">
      <section className="panel service-issue service-issue--danger" aria-labelledby="issue-reauth">
        <h2 id="issue-reauth">{copy.service.reauthTitle}</h2><p>{copy.launchFlow.reauth}</p>
        <div className="actions"><Button disabled={busy} onClick={() => {
          setBusy(true); void connectAccount('direct', businessId()).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : copy.errors.generic)).finally(() => setBusy(false));
        }}>{copy.setup.accounts.connectDirect}</Button><Link className="button button--secondary" to="/connect/vk">{copy.launchFlow.vkConnect}</Link></div>
      </section>
      <section className="panel service-issue service-issue--warning" aria-labelledby="issue-balance">
        <h2 id="issue-balance">{copy.service.lowBalanceTitle}</h2><p>{copy.launchFlow.lowBalance}</p>
        <div className="actions"><a className="button button--secondary" href="https://direct.yandex.ru/" target="_blank" rel="noreferrer">{copy.launchFlow.openDirect}</a>
          <a className="button button--secondary" href="https://ads.vk.com/" target="_blank" rel="noreferrer">{copy.launchFlow.openVk}</a></div>
      </section>
      <section className="panel service-issue service-issue--neutral" aria-labelledby="issue-unavailable">
        <h2 id="issue-unavailable">{copy.service.unavailableTitle}</h2><p>{copy.launchFlow.platformUnavailable}</p>
      </section>
    </div>
    <Link className="service-back" to="/setup">{copy.common.start}<Icon name="arrow" /></Link>
  </div>;
}
