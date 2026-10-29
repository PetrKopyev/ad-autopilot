import { useState } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router';
import { copy } from '../copy';
import { logout } from '../api/setup';
import { useSession } from '../api/session';
import { AppNav } from './AppNav';
import { ErrorBanner } from './ErrorBanner';
import { Icon } from './Icon';

export function Layout() {
  const session = useSession();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const onLogin = pathname === '/login';
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function signOut() {
    setBusy(true); setError('');
    try { await logout(); session.reload(); navigate('/login'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  // Мастер и план идут до запуска: разделов там ещё нет, меню не отвлекает.
  const locked = !session.data?.has_business || pathname === '/plan' || pathname.startsWith('/setup');
  return (
    <div className={`app-layout${!session.data ? ' app-layout--guest' : locked ? '' : ' app-layout--tabbar'}`}>
      <a className="skip-link" href="#main">{copy.navigation.skip}</a>
      {/* Разделы закрыты до входа: меню показываем только вошедшему. */}
      {session.data ? <AppNav login={session.data.login} locked={locked} busy={busy} onSignOut={() => void signOut()} />
        : <header className="guest-header">
          <Link to="/" className="guest-header__brand"><span className="app-rail__mark"><Icon name="pilot" /></span>{copy.appName}</Link>
          {session.data === null && !onLogin && <Link to="/login" className="guest-header__login">{copy.login.title}</Link>}
        </header>}
      <div className="app-body">
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
    </div>
  );
}
