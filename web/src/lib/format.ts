const COMPACT = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const DECIMAL = new Intl.NumberFormat('en', { maximumFractionDigits: 2 });

export function compactNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return COMPACT.format(n);
}

export function decimal(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return n.toFixed(digits);
}

/** How long a depth tier runs.
 *
 * The server sends a formatted `duration_label`; this is the fallback for
 * when it does not, which in practice means a server older than the field.
 * Rendering the raw `hours` there would print "0.5h", and rendering the
 * missing label printed an empty badge -- both worse than formatting it here.
 */
export function duration(label: string | undefined, hours: number): string {
  if (label) return label;
  if (!Number.isFinite(hours)) return '—';
  return hours < 1 ? `${Math.round(hours * 60)}m` : `${+hours.toFixed(2)}h`;
}

/** Costs are small enough that rounding to cents hides real spend. */
export function usd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  if (n === 0) return '$0';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${DECIMAL.format(n)}`;
}

export function ms(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  if (n < 1000) return `${Math.round(n)}ms`;
  return `${(n / 1000).toFixed(1)}s`;
}

const RELATIVE = new Intl.RelativeTimeFormat('en', { numeric: 'auto', style: 'narrow' });

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';

  const seconds = Math.round((then - Date.now()) / 1000);
  const abs = Math.abs(seconds);

  if (abs < 60) return 'just now';
  if (abs < 3600) return RELATIVE.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return RELATIVE.format(Math.round(seconds / 3600), 'hour');
  return RELATIVE.format(Math.round(seconds / 86400), 'day');
}

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString('en', { hour12: false });
}
