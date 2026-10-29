import { Link, NavLink } from 'react-router';
import { copy } from '../copy';
import { Button } from './Button';
import { Icon, type IconName } from './Icon';

const navigation: { to: string; label: string; icon: IconName }[] = [
  { to: '/', label: copy.navigation.home, icon: 'home' },
  { to: '/ads', label: copy.navigation.ads, icon: 'ads' },
  { to: '/statistics', label: copy.navigation.statistics, icon: 'chart' },
  { to: '/history', label: copy.navigation.history, icon: 'history' },
  { to: '/clients', label: copy.navigation.clients, icon: 'clients' },
  { to: '/settings', label: copy.navigation.settings, icon: 'settings' },
];

export interface AppNavProps {
  login: string;
  /** До запуска разделов нет: вместо меню — подпись, что они откроются. */
  locked: boolean;
  busy: boolean;
  onSignOut: () => void;
}

/**
 * Тёмная колонка слева: логотип, шесть разделов, вошедший и «Выйти». На телефоне колонка становится строкой сверху,
 * а то же меню — панелью внизу экрана: одно меню в разметке, чтобы у разделов не было двойников.
 */
export function AppNav({ login, locked, busy, onSignOut }: AppNavProps) {
  return <header className="app-rail">
    <Link to="/" className="app-rail__logo" aria-label={copy.appName}><span className="app-rail__mark"><Icon name="pilot" /></span>
      <span className="app-rail__name" aria-hidden="true">{copy.appName}</span></Link>
    {locked ? <p className="app-rail__locked">{copy.navigation.locked}</p>
      : <nav className="app-nav" aria-label={copy.navigation.label}>
        {navigation.map(({ to, label, icon }) => (
          <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => `nav-link${isActive ? ' nav-link--active' : ''}`}>
            <Icon name={icon} /><span>{label}</span>
          </NavLink>
        ))}
      </nav>}
    <div className="app-rail__account">
      <span className="app-rail__avatar" aria-hidden="true">{login.slice(0, 1).toUpperCase()}</span>
      <span className="app-rail__login" title={copy.navigation.account(login)}>{login}</span>
      <Button variant="ghost" size="small" icon="logout" className="app-rail__logout" disabled={busy} onClick={onSignOut}>{copy.launchFlow.logout}</Button>
    </div>
  </header>;
}
