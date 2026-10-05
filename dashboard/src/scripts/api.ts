// Shared helpers: authenticated fetch with CSRF, tiny DOM builder, formatting.
// All server data is inserted with textContent (never innerHTML) to avoid XSS.

export type Quota = { cpus: number | null; memory_mib: number | null; disk_gib: number | null };
export type Me = {
  id: number; email: string; name: string; role: 'admin' | 'user'; csrf: string; session_expires: number;
  quota: Quota; allocated: Quota; remaining: Quota;
};
export type Meta = { ts: number | null; age_s: number | null; lxd_ok: boolean | null; error: string | null;
  last_ok: number | null; collector_ok: boolean; interval: number };

let mePromise: Promise<Me> | null = null;

export function toLogin(): never {
  location.assign('/login/');
  throw new Error('redirecting to login');
}

export function getMe(): Promise<Me> {
  mePromise ??= fetch('/api/me', { credentials: 'same-origin' }).then((r) => (r.status === 401 ? toLogin() : r.json()));
  return mePromise;
}

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function api<T = unknown>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== 'GET') headers['X-CSRF-Token'] = (await getMe()).csrf;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res: Response;
  try {
    res = await fetch(path, { method, headers, credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError('The server is unreachable. Check that the dashboard service is running.', 0);
  }
  if (res.status === 401) toLogin();
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data as { detail?: string; title?: string } | null;
    const msg = d?.title && d.detail && d.detail !== d.title ? `${d.title}: ${d.detail}` : d?.detail ?? d?.title;
    throw new ApiError(msg ?? `${res.status} ${res.statusText}`, res.status);
  }
  return data as T;
}

type Child = Node | string | null | undefined | false;
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K, props: Record<string, unknown> = {}, ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v as EventListener);
    else if (k === 'class') el.className = String(v);
    else if (k in el && typeof v !== 'string') (el as any)[k] = v;
    else el.setAttribute(k, String(v));
  }
  for (const c of children) if (c) el.append(c);
  return el;
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return '–';
  if (n === 0) return '0 B';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(Math.abs(n)) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function duration(s: number | null | undefined): string {
  if (s == null) return '–';
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${hh}h` : hh ? `${hh}h ${m}m` : `${m}m`;
}

export const pct = (v: number | null | undefined, digits = 1) => (v == null ? '–' : `${v.toFixed(digits)}%`);

let toastTimer = 0;
export function toast(msg: string): void {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (t.hidden = true), 5000);
}

/** Top banner for degraded states: LXD down, collector stopped, stale data. */
export function banner(meta: Meta | null): void {
  const b = document.getElementById('banner');
  if (!b) return;
  let msg = '';
  let bad = false;
  if (meta && !meta.collector_ok) {
    msg = meta.ts ? `Metrics collector has not reported for ${Math.round(meta.age_s ?? 0)} s — values below are stale.`
      : 'Metrics collector has not run yet — start hsm-collector to see live data.';
    bad = true;
  } else if (meta && meta.lxd_ok === false) {
    const since = meta.last_ok ? ` since ${new Date(meta.last_ok * 1000).toLocaleTimeString()}` : '';
    msg = `LXD is not answering${since} — showing last known values. ${meta.error ?? ''}`;
    bad = true;
  }
  b.textContent = msg;
  b.className = bad ? 'banner bad' : 'banner';
  b.hidden = !msg;
}

export const NAME_RE = /^[a-z0-9-]{1,63}$/;

/** Fill in the shared top bar (user, nav, sign-out). */
export async function initChrome(): Promise<Me> {
  const me = await getMe();
  document.getElementById('who')!.textContent = `${me.email} (${me.role})`;
  for (const id of ['nav-admin', 'nav-usage']) document.getElementById(id)!.hidden = me.role !== 'admin';
  const logout = document.getElementById('logout') as HTMLButtonElement;
  logout.hidden = false;
  logout.onclick = async () => {
    await api('/api/auth/logout', 'POST').catch(() => undefined);
    location.assign('/login/');
  };
  return me;
}

export function bar(pctValue: number | null): HTMLElement {
  const p = pctValue == null ? 0 : Math.max(0, Math.min(100, pctValue));
  const fill = h('i');
  fill.style.width = `${p}%`;
  fill.className = p > 90 ? 'crit' : p > 70 ? 'hot' : '';
  return h('div', { class: 'bar' }, fill);
}
