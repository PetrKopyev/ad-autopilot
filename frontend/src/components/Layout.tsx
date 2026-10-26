import { Link, NavLink, Outlet } from 'react-router';
import { copy } from '../copy';

const navigation = [
  { to: '/', label: copy.navigation.home },
  { to: '/ads', label: copy.navigation.ads },
  { to: '/statistics', label: copy.navigation.statistics },
  { to: '/history', label: copy.navigation.history },
  { to: '/clients', label: copy.navigation.clients },
  { to: '/settings', label: copy.navigation.settings },
];

export function Layout() {
  return (
    <div className="app-layout">
      <a className="skip-link" href="#main">{copy.navigation.skip}</a>
      <header className="app-header">
        <Link to="/" className="brand">{copy.appName}</Link>
        <Link to="/login" className="header-link">{copy.login.title}</Link>
      </header>
      <aside className="sidebar">
        <nav aria-label={copy.navigation.label}>
          {navigation.map(({ to, label }) => (
            <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => `nav-link${isActive ? ' nav-link--active' : ''}`}>
              {label}
            </NavLink>
          ))}
        </nav>
        <Link className="setup-link" to="/setup">{copy.common.start}</Link>
      </aside>
      <main id="main" className="main-content" tabIndex={-1}><Outlet /></main>
      <footer className="app-footer">
        <Link to="/policy">{copy.service.policyTitle}</Link>
        <Link to="/connect/vk">{copy.setup.accounts.vk}</Link>
        <Link to="/platform-error">{copy.service.errorsTitle}</Link>
      </footer>
    </div>
  );
}
