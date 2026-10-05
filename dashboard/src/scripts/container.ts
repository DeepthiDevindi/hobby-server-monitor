import { api, ApiError, banner, bytes, duration, h, initChrome, NAME_RE, pct, toast, type Me, type Meta } from './api';
import { lineChart, type Point } from './chart';
import type { Live } from './dashboard';

const name = new URLSearchParams(location.search).get('name') ?? '';
type Detail = Live & { assigned?: string[]; description?: string; autostart?: boolean; ephemeral?: boolean };
type History = { tier: string; step: number; start: number; end: number; points: Point[] };
let me: Me;
let range = sessionStorage.getItem('hsm-range') ?? '1h';

async function loadDetail(): Promise<Detail | null> {
  let c: Detail;
  try {
    c = await api<Detail>(`/api/containers/${name}`);
  } catch (e) {
    const msg = e instanceof ApiError && e.status === 403 ? 'You do not have access to this container.' : (e as Error).message;
    document.getElementById('c-sub')!.textContent = msg;
    document.querySelectorAll('main section').forEach((s) => ((s as HTMLElement).hidden = true));
    return null;
  }
  document.getElementById('c-name')!.textContent = c.name;
  document.getElementById('c-sub')!.textContent = `${c.status} · ${c.image || 'image unknown'}${c.description ? ` · ${c.description}` : ''}`;
  const f = (k: string, v: string) => [h('dt', {}, k), h('dd', {}, v)];
  document.getElementById('c-facts')!.replaceChildren(
    ...f('Status', c.status), ...f('CPU now', pct(c.cpu_pct)),
    ...f('Memory', `${bytes(c.mem_used)} / ${bytes(c.mem_limit)}`),
    ...f('Disk', c.disk_used == null ? 'n/a' : `${bytes(c.disk_used)}${c.disk_limit ? ` / ${bytes(c.disk_limit)}` : ''}`),
    ...f('Network', c.rx_bps == null ? '–' : `↓${bytes(c.rx_bps)}/s ↑${bytes(c.tx_bps)}/s`),
    ...f('IPv4', c.ipv4 ?? '–'), ...f('Uptime', c.status === 'Running' ? duration(c.uptime_s) : '–'),
    ...f('Processes', c.procs == null ? '–' : String(c.procs)),
    ...f('CPU limit', c.cpus ? `${c.cpus} core(s)${c.cpu_allowance ? `, ${c.cpu_allowance}` : ''}` : 'none'),
    ...f('Pool', c.pool ?? '–'), ...f('Owner', c.owner ?? '–'),
    ...(c.assigned ? f('Assigned to', c.assigned.join(', ') || '–') : []),
    ...f('Autostart', c.autostart ? 'yes' : 'no'), ...f('Ephemeral', c.ephemeral ? 'yes' : 'no'));
  const term = document.getElementById('term-link') as HTMLAnchorElement;
  term.href = `/terminal/?name=${encodeURIComponent(c.name)}`;
  term.hidden = c.status !== 'Running';
  return c;
}

async function loadHistory(): Promise<void> {
  document.querySelectorAll<HTMLButtonElement>('#ranges button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.r === range)));
  const box = document.getElementById('charts')!;
  let hist: History;
  try {
    hist = await api<History>(`/api/containers/${name}/history?range=${range}`);
  } catch (e) {
    box.replaceChildren(h('p', { class: 'error' }, `Could not load history: ${(e as Error).message}`));
    return;
  }
  const raw = hist.tier === 'raw';
  if (!raw) rollupRates(hist);
  const k = (rawKey: string, rollKey: string) => (raw ? rawKey : rollKey);
  document.getElementById('h-meta')!.textContent =
    `${hist.points.length} points · ${hist.step >= 3600 ? `${hist.step / 3600} h` : hist.step >= 60 ? `${hist.step / 60} min` : `${hist.step} s`} per point · from the ${raw ? '10 s raw' : hist.tier === '5m' ? '5-minute' : '1-hour'} tier`;
  const common = { points: hist.points, start: hist.start, end: hist.end, step: hist.step };
  // Colours are CSS variables (light/dark validated palette), applied via style.
  const s1 = 'var(--series-1)', s2 = 'var(--series-2)';
  box.replaceChildren(
    lineChart({ ...common, title: 'CPU (% of one core)', series: [{ key: k('cpu_pct', 'cpu_avg'), label: 'CPU', color: s1 }], format: (v) => `${v.toFixed(v < 10 ? 1 : 0)}%` }),
    lineChart({ ...common, title: 'Memory used', series: [{ key: k('mem_used', 'mem_max'), label: raw ? 'Memory' : 'Memory (peak)', color: s1 }], format: (v) => bytes(v) }),
    lineChart({ ...common, title: 'Network', series: [{ key: 'rx_bps', label: 'Received', color: s1 }, { key: 'tx_bps', label: 'Sent', color: s2 }].map((s) => raw ? s : { ...s, key: s.key === 'rx_bps' ? 'rx_rate' : 'tx_rate' }), format: (v) => `${bytes(v)}/s` }),
    lineChart({ ...common, title: 'Disk used', series: [{ key: 'disk_used', label: 'Disk', color: s1 }], format: (v) => bytes(v) }),
  );
}

function rollupRates(hist: History): void {
  // Rollups store bytes per window; convert to a rate for the network chart.
  for (const p of hist.points) {
    if (p.rx_bytes != null) p.rx_rate = p.rx_bytes / hist.step;
    if (p.tx_bytes != null) p.tx_rate = p.tx_bytes / hist.step;
  }
}

function setupRanges(): void {
  document.querySelectorAll<HTMLButtonElement>('#ranges button').forEach((b) => b.onclick = () => {
    range = b.dataset.r!;
    sessionStorage.setItem('hsm-range', range);
    void loadHistory();
  });
}

function setupExec(): void {
  const f = document.getElementById('exec-form') as HTMLFormElement;
  const out = document.getElementById('exec-out')!;
  const meta = document.getElementById('exec-meta')!;
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = f.querySelector('button')!;
    btn.disabled = true;
    meta.textContent = 'running…';
    try {
      const r = await api<{ exit_code: number; stdout: string; stderr: string; truncated: number; duration_s: number; timed_out: boolean }>(
        `/api/containers/${name}/exec`, 'POST', { command: new FormData(f).get('command') });
      out.replaceChildren(r.stdout, ...(r.stderr ? [h('span', { class: 'err' }, r.stderr)] : []));
      out.hidden = false;
      meta.textContent = `exit code ${r.exit_code} · ${r.duration_s}s${r.timed_out ? ' · killed after the time limit' : ''}${r.truncated ? ` · ${bytes(r.truncated)} of output not shown (64 KiB cap)` : ''}`;
    } catch (e) {
      meta.textContent = (e as Error).message;
    } finally {
      btn.disabled = false;
    }
  };
}

async function setupAdmin(c: Detail): Promise<void> {
  document.getElementById('admin-panel')!.hidden = false;
  const lf = document.getElementById('limits-form') as HTMLFormElement;
  const val = (n: string, v: unknown) => ((lf.elements.namedItem(n) as HTMLInputElement).value = v == null ? '' : String(v));
  val('cpus', c.allocated?.cpus ?? c.cpus ?? 1);
  val('memory_mib', c.allocated?.memory_mib ?? Math.round(c.mem_limit / 2 ** 20));
  val('disk_gib', c.allocated?.disk_gib ?? '');
  const allowance = c.cpu_allowance?.match(/^(\d+)ms\/100ms$/);
  val('cpu_allowance', allowance && c.cpus ? Math.round(Number(allowance[1]) / c.cpus) : 100);
  lf.onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(lf);
    const err = document.getElementById('limits-err')!;
    err.textContent = '';
    const num = (k: string) => (fd.get(k) === '' ? null : Number(fd.get(k)));
    try {
      await api(`/api/containers/${name}`, 'PATCH', { cpus: num('cpus'), memory_mib: num('memory_mib'),
        cpu_allowance: num('cpu_allowance'), ...(num('disk_gib') != null && num('disk_gib') !== c.allocated?.disk_gib ? { disk_gib: num('disk_gib') } : {}) });
      toast('Limits saved');
      await loadDetail();
    } catch (e) { err.textContent = (e as Error).message; }
  };
  const users = await api<{ id: number; email: string }[]>('/api/admin/users');
  const sel = document.getElementById('owner-sel') as HTMLSelectElement;
  sel.replaceChildren(h('option', { value: '' }, '(no owner)'),
    ...users.map((u) => h('option', { value: String(u.id), selected: u.email === c.owner }, u.email)));
  (document.getElementById('owner-form') as HTMLFormElement).onsubmit = async (ev) => {
    ev.preventDefault();
    const err = document.getElementById('owner-err')!;
    err.textContent = '';
    try {
      await api(`/api/containers/${name}/owner`, 'PUT', { owner_id: sel.value ? Number(sel.value) : null });
      toast('Owner changed');
      await loadDetail();
    } catch (e) { err.textContent = (e as Error).message; }
  };
}

(async () => {
  me = await initChrome();
  if (!NAME_RE.test(name)) {
    document.getElementById('c-sub')!.textContent = 'Invalid container name.';
    return;
  }
  const c = await loadDetail();
  if (!c) return;
  setupRanges();
  setupExec();
  await loadHistory();
  if (me.role === 'admin') await setupAdmin(c);
  const live = await api<{ meta: Meta }>('/api/containers');
  banner(live.meta);
  setInterval(() => void loadDetail(), 10_000);  // facts follow the 10 s collector
})().catch((e) => toast((e as Error).message));
