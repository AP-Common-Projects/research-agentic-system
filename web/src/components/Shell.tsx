import { useEffect, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { Tooltip } from './primitives';

import type { ReactElement } from 'react';

type NavIcon = (props: { className?: string }) => ReactElement;

const ICONS: Record<string, NavIcon> = {
  new: ({ className }) => (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden>
      <path d="M10 4v12M4 10h12" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  ),
  live: ({ className }) => (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden>
      <path d="M2.5 11.5h3l2-6 3.5 11 2.5-8 1.5 3h2.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  workbooks: ({ className }) => (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden>
      <rect x="4" y="3" width="12" height="14" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
      <path d="M7 7h6M7 10.5h6M7 14h3.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  ),
  graph: ({ className }) => (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden>
      <circle cx="5" cy="15" r="1.8" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="10" cy="10" r="1.8" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="10" cy="4.5" r="1.8" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="15.5" cy="10" r="1.8" stroke="currentColor" strokeWidth="1.6" />
      <path d="M6.3 13.7 8.7 11.3M10 8.2V6.3M11.3 11.3l2.9-1.4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  ),
  spend: ({ className }) => (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden>
      <path d="M10 3v14M13.5 6.5c0-1.4-1.4-2.5-3.5-2.5s-3.5 1-3.5 2.5S8 8.5 10 9c2 .5 3.5 1.3 3.5 3s-1.6 2.7-3.5 2.7-3.5-1-3.5-2.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  balances: ({ className }) => (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden>
      <rect x="2.5" y="6" width="15" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
      <path d="M2.5 9h15" stroke="currentColor" strokeWidth="1.6" />
      <path d="M5.5 12.5h3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  ),
};

const NAV = [
  { to: '/new', icon: 'new', label: 'New run', hint: 'Start a new piece of research' },
  { to: '/live', icon: 'live', label: 'Live runs', hint: 'Follow a run while it works' },
  { to: '/workbooks', icon: 'workbooks', label: 'Workbooks', hint: 'Your finished Excel reports' },
  { to: '/graph', icon: 'graph', label: 'Discovery graph', hint: 'What a workbook is made up of' },
  { to: '/spend', icon: 'spend', label: 'Spend', hint: 'What each workbook cost to produce' },
  { to: '/balances', icon: 'balances', label: 'Balances', hint: 'Your credit, and what it covers' },
];

function useTheme() {
  const [theme, setTheme] = useState<'dark' | 'light'>(
    () => (localStorage.getItem('niche-harness-theme') as 'dark' | 'light') ?? 'dark',
  );
  // False on the mount that sets the theme the page already loaded in --
  // only a later, user-driven change should animate.
  const isFirstRender = useState(() => ({ current: true }))[0];

  useEffect(() => {
    document.documentElement.classList.toggle('theme-changing', !isFirstRender.current);
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('niche-harness-theme', theme);
    isFirstRender.current = false;
    if (!document.documentElement.classList.contains('theme-changing')) return;
    const id = window.setTimeout(
      () => document.documentElement.classList.remove('theme-changing'),
      260,
    );
    return () => window.clearTimeout(id);
  }, [theme]);

  return { theme, toggle: () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')) };
}

function useCollapsed() {
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem('niche-harness-sidebar') === 'collapsed',
  );
  useEffect(() => {
    localStorage.setItem('niche-harness-sidebar', collapsed ? 'collapsed' : 'expanded');
  }, [collapsed]);
  return { collapsed, toggle: () => setCollapsed((c) => !c) };
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

function ThemeGlyph({ theme }: { theme: 'dark' | 'light' }) {
  return theme === 'dark' ? (
    <svg viewBox="0 0 20 20" fill="none" className="size-[18px]" aria-hidden>
      <path
        d="M16.5 12.3A6.8 6.8 0 0 1 7.7 3.5a6.8 6.8 0 1 0 8.8 8.8Z"
        stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"
      />
    </svg>
  ) : (
    <svg viewBox="0 0 20 20" fill="none" className="size-[18px]" aria-hidden>
      <circle cx="10" cy="10" r="3.4" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M10 2.5v2M10 15.5v2M17.5 10h-2M4.5 10h-2M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4M15.3 15.3l-1.4-1.4M6.1 6.1 4.7 4.7"
        stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
      />
    </svg>
  );
}

function CollapseGlyph({ collapsed, className }: { collapsed: boolean; className?: string }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden>
      <rect x="2.5" y="4" width="15" height="12" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 4v12" stroke="currentColor" strokeWidth="1.5" />
      <path
        d={collapsed ? 'M6.2 7.5 9 10l-2.8 2.5' : 'M9.8 7.5 7 10l2.8 2.5'}
        stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
      />
    </svg>
  );
}

export function Shell() {
  const { theme, toggle } = useTheme();
  const { collapsed, toggle: toggleCollapsed } = useCollapsed();

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

      <aside
        className={`sticky top-0 flex h-screen shrink-0 flex-col border-r border-line bg-surface transition-[width] duration-200 ${
          collapsed ? 'w-[60px]' : 'w-[212px]'
        }`}
      >
        <div
          className={`flex items-center gap-2.5 px-4 py-4 ${
            collapsed ? 'flex-col justify-center gap-2 px-0' : 'justify-between'
          }`}
        >
          <div className={`flex min-w-0 items-center gap-2.5 ${collapsed ? 'justify-center' : ''}`}>
            <Mark />
            {!collapsed && (
              <div className="min-w-0 leading-tight">
                <p className="truncate font-display text-sm font-semibold tracking-tight text-ink">
                  Niche Harness
                </p>
                <p className="truncate font-mono text-[10px] tracking-wide text-ink-3">
                  research console
                </p>
              </div>
            )}
          </div>

          <Tooltip label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} side="right">
            <button
              type="button"
              onClick={toggleCollapsed}
              aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
              className="flex shrink-0 items-center justify-center rounded-md border border-line p-1 text-ink-3 transition-colors hover:bg-sunken hover:text-ink"
            >
              <CollapseGlyph collapsed={collapsed} className="size-3.5 shrink-0" />
            </button>
          </Tooltip>
        </div>

        <nav className="flex-1 px-2 py-2" aria-label="Sections">
          <ul className="space-y-0.5">
            {NAV.map((item) => {
              const Icon = ICONS[item.icon];
              return (
                <li key={item.to}>
                  <Tooltip label={collapsed ? `${item.label} — ${item.hint}` : item.hint} side="right" className="w-full">
                    <NavLink
                      to={item.to}
                      aria-label={collapsed ? item.label : undefined}
                      className={({ isActive }) =>
                        `flex w-full items-center gap-2.5 rounded-md text-sm transition-colors ${
                          collapsed ? 'justify-center px-0 py-2' : 'px-2.5 py-2'
                        } ${
                          isActive
                            ? 'bg-sunken font-medium text-ink'
                            : 'text-ink-2 hover:bg-sunken hover:text-ink'
                        }`}
                    >
                      <Icon className="size-[18px] shrink-0" />
                      {!collapsed && <span className="truncate">{item.label}</span>}
                    </NavLink>
                  </Tooltip>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className={`border-t border-line py-3 ${collapsed ? 'px-2' : 'px-3'}`}>
          <Tooltip label={collapsed ? (storeDown ? 'Store unreachable' : 'Store connected') : undefined} side="right" className="w-full">
            <div className={`mb-2 flex items-center gap-1.5 ${collapsed ? 'justify-center' : ''}`}>
              <span
                aria-hidden
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{
                  background: storeDown ? 'var(--status-critical)' : 'var(--status-good)',
                }}
              />
              {!collapsed && (
                <span className="font-mono text-[10px] text-ink-3">
                  {storeDown ? 'Store unreachable' : 'Store connected'}
                </span>
              )}
            </div>
          </Tooltip>

          <Tooltip label={collapsed ? (theme === 'dark' ? 'Light theme' : 'Dark theme') : undefined} side="right" className="w-full">
            <button
              type="button"
              onClick={toggle}
              aria-label={collapsed ? (theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme') : undefined}
              className={`w-full rounded-md border border-line text-xs text-ink-2 transition-colors hover:bg-sunken hover:text-ink ${
                collapsed ? 'flex justify-center px-0 py-1.5' : 'px-2 py-1.5'
              }`}
            >
              {collapsed ? <ThemeGlyph theme={theme} /> : theme === 'dark' ? 'Light theme' : 'Dark theme'}
            </button>
          </Tooltip>

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
