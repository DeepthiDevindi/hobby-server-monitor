// Shared helpers: authenticated fetch with CSRF, tiny DOM builder, formatting.
// All server data is inserted with textContent (never innerHTML) to avoid XSS.

export type Me = { id: number; email: string; name: string; role: 'admin' | 'user'; csrf: string; session_expires: number };

let mePromise: Promise<Me> | null = null;

export function toLogin(): never {
  location.assign('/login/');
  throw new Error('redirecting to login');
}

export function getMe(): Promise<Me> {
  mePromise ??= fetch('/api/me', { credentials: 'same-origin' }).then((r) => (r.status === 401 ? toLogin() : r.json()));
  return mePromise;
}

function errorText(data: unknown, fallback: string): string {
  const detail = (data as { detail?: unknown })?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d) => `${(d.loc ?? []).slice(-1)[0] ?? ''}: ${d.msg}`).join('; ');
  return fallback;
}

export async function api<T = unknown>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== 'GET') headers['X-CSRF-Token'] = (await getMe()).csrf;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {
    method, headers, credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) toLogin();
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(errorText(data, `${res.status} ${res.statusText}`));
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
  if (!n) return '0 B';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

let toastTimer = 0;
export function toast(msg: string): void {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (t.hidden = true), 4000);
}

export const NAME_RE = /^[a-z0-9-]{1,63}$/;

/** Fill in the shared top bar (user, admin link, sign-out). */
export async function initChrome(): Promise<Me> {
  const me = await getMe();
  document.getElementById('who')!.textContent = `${me.email} (${me.role})`;
  document.getElementById('nav-admin')!.hidden = me.role !== 'admin';
  const logout = document.getElementById('logout') as HTMLButtonElement;
  logout.hidden = false;
  logout.onclick = async () => {
    await api('/api/auth/logout', 'POST').catch(() => undefined);
    location.assign('/login/');
  };
  return me;
}
