import { useEffect, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';

const NAV = [
  { to: '/new', label: 'New run', hint: 'Start a new piece of research' },
  { to: '/workbooks', label: 'Workbooks', hint: 'Your finished Excel reports' },
  { to: '/graph', label: 'Discovery graph', hint: 'What a workbook is made up of' },
  { to: '/spend', label: 'Spend', hint: 'What each workbook cost to produce' },
  { to: '/balances', label: 'Balances', hint: 'Your credit, and what it covers' },
];

function useTheme() {
  const [theme, setTheme] = useState<'dark' | 'light'>(
    () => (localStorage.getItem('niche-harness-theme') as 'dark' | 'light') ?? 'dark',
  );

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('niche-harness-theme', theme);
  }, [theme]);

  return { theme, toggle: () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')) };
}

/** A sounding mark: a frontier pushed outward from a seed. */
function Mark() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden className="shrink-0">
      <circle cx="12" cy="12" r="2.5" fill="var(--track-graph)" />
      <circle cx="12" cy="12" r="6.5" fill="none" stroke="var(--track-graph)" strokeWidth="1.5" opacity="0.6" />
      <circle cx="12" cy="12" r="10.5" fill="none" stroke="var(--track-graph)" strokeWidth="1.5" opacity="0.25" />
    </svg>
  );
}

export function Shell() {
  const { theme, toggle } = useTheme();

  const health = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 30_000,
  });

  const storeDown = health.data && !health.data.store_reachable;

  return (
    <div className="flex min-h-screen">
      <a
        href="#main"
        className="sr-only rounded bg-raised px-3 py-2 text-sm focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50"
      >
        Skip to content
      </a>

      <aside className="sticky top-0 flex h-screen w-[212px] shrink-0 flex-col border-r border-line bg-surface">
        <div className="flex items-center gap-2.5 px-4 py-4">
          <Mark />
          <div className="leading-tight">
            <p className="font-display text-sm font-semibold tracking-tight text-ink">Niche Harness</p>
            <p className="font-mono text-[10px] tracking-wide text-ink-3">research console</p>
          </div>
        </div>

        <nav className="flex-1 px-2 py-2" aria-label="Sections">
          <ul className="space-y-0.5">
            {NAV.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  title={item.hint}
                  className={({ isActive }) =>
                    `block rounded-md px-2.5 py-2 text-sm transition-colors ${
                      isActive
                        ? 'bg-sunken font-medium text-ink'
                        : 'text-ink-2 hover:bg-sunken hover:text-ink'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="border-t border-line px-3 py-3">
          <div className="mb-2 flex items-center gap-1.5">
            <span
              aria-hidden
              className="h-1.5 w-1.5 shrink-0 rounded-full"
              style={{
                background: storeDown ? 'var(--status-critical)' : 'var(--status-good)',
              }}
            />
            <span className="font-mono text-[10px] text-ink-3">
              {storeDown ? 'Store unreachable' : 'Store connected'}
            </span>
          </div>
          <button
            type="button"
            onClick={toggle}
            className="w-full rounded-md border border-line px-2 py-1.5 text-xs text-ink-2 transition-colors hover:bg-sunken hover:text-ink"
          >
            {theme === 'dark' ? 'Light theme' : 'Dark theme'}
          </button>
        </div>
      </aside>

      <main id="main" className="min-w-0 flex-1">
        {storeDown ? (
          <div
            role="status"
            className="border-b px-6 py-2.5 text-xs"
            style={{
              borderColor: 'color-mix(in oklab, var(--status-critical) 35%, var(--line))',
              background: 'color-mix(in oklab, var(--status-critical) 8%, transparent)',
            }}
          >
            <span className="font-medium text-ink">Postgres is not reachable.</span>{' '}
            <span className="text-ink-2">
              Run history still loads from disk; channel, video, and graph views will be empty until
              the connection is restored.
            </span>
            {health.data?.store_error ? (
              <span className="ml-1 font-mono text-ink-3">{health.data.store_error}</span>
            ) : null}
          </div>
        ) : null}
        <Outlet />
      </main>
    </div>
  );
}
