import { Link, NavLink, Outlet } from 'react-router-dom';
import { HealthDot } from './HealthDot';
import { IconBook, IconMoon, IconSun } from './icons';
import { ProfileMenu } from './ProfileMenu';
import { useApp } from '../state/AppState';
import { mockMode } from '../api/client';

export function AppShell() {
  const { theme, toggleTheme } = useApp();

  return (
    <div className="app-shell">
      <a className="visually-hidden" href="#main-content">
        跳到主要内容
      </a>
      <header className="topbar">
        <div className="topbar__inner">
          <Link to="/" className="brand" aria-label="课堂首页">
            <span className="brand__mark" aria-hidden="true">
              <IconBook size={19} />
            </span>
            <span className="brand__name">课堂</span>
          </Link>
          <nav className="topnav" aria-label="主导航">
            <NavLink to="/" end className="topnav__link">
              课程库
            </NavLink>
          </nav>
          {mockMode && (
            <span className="mock-flag" title="VITE_MOCK=1：数据来自内置假数据">
              MOCK
            </span>
          )}
          <div className="topbar__actions">
            <HealthDot />
            <button
              type="button"
              className="icon-toggle"
              aria-label={theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'}
              title={theme === 'dark' ? '浅色模式' : '深色模式'}
              onClick={toggleTheme}
            >
              {theme === 'dark' ? <IconSun size={20} /> : <IconMoon size={20} />}
            </button>
            <ProfileMenu />
          </div>
        </div>
      </header>
      <main id="main-content" className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
