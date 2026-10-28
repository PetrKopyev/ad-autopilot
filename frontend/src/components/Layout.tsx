import { useState } from 'react';
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router';
import { copy } from '../copy';
import { logout } from '../api/setup';
import { useSession } from '../api/session';
import { Button } from './Button';
import { ErrorBanner } from './ErrorBanner';

const navigation = [
  { to: '/', label: copy.navigation.home },
  { to: '/ads', label: copy.navigation.ads },
  { to: '/statistics', label: copy.navigation.statistics },
  { to: '/history', label: copy.navigation.history },
  { to: '/clients', label: copy.navigation.clients },
  { to: '/settings', label: copy.navigation.settings },
];

export function Layout() {
  const session = useSession();
  const navigate = useNavigate();
  const onLogin = useLocation().pathname === '/login';
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function signOut() {
    setBusy(true); setError('');
    try { await logout(); session.reload(); navigate('/login'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  return (
    <div className={`app-layout${session.data ? '' : ' app-layout--guest'}`}>
      <a className="skip-link" href="#main">{copy.navigation.skip}</a>
      <header className="app-header">
        <Link to="/" className="brand">{copy.appName}</Link>
        {session.data ? <div className="header-account"><span>{session.data.login}</span>
          <Button variant="secondary" disabled={busy} onClick={() => void signOut()}>{copy.launchFlow.logout}</Button>
        </div> : session.data === null && !onLogin && <Link to="/login" className="header-link">{copy.login.title}</Link>}
      </header>
      {/* Разделы закрыты до входа: меню показываем только вошедшему. */}
      {session.data && <aside className="sidebar">
        <nav aria-label={copy.navigation.label}>
          {navigation.map(({ to, label }) => (
            <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => `nav-link${isActive ? ' nav-link--active' : ''}`}>
              {label}
            </NavLink>
          ))}
        </nav>
        <Link className="setup-link" to="/setup">{copy.common.start}</Link>
      </aside>}
      <main id="main" className="main-content" tabIndex={-1}>
        {(error || session.error) && <ErrorBanner message={error || session.error!} onRetry={error ? () => void signOut() : session.reload} />}
        <Outlet />
      </main>
      <footer className="app-footer">
        {!onLogin && <Link to="/policy">{copy.service.policyTitle}</Link>}
        {session.data && <>
          <Link to="/connect/vk">{copy.setup.accounts.vk}</Link>
          <Link to="/platform-error">{copy.service.errorsTitle}</Link>
        </>}
      </footer>
    </div>
  );
}
