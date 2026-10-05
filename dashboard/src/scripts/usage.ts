import { api, bar, bytes, h, initChrome, pct, toast, type Quota } from './api';

type Usage = {
  period: string;
  host: { cpus: number; memory_mib: number; pools: { name: string; driver: string; total_gib: number; used_gib: number }[] };
  allocated: Quota; unlimited_containers: string[];
  users: { email: string; role: string; quota: Quota; allocated: Quota }[];
  containers: { name: string; owner: string | null; status: string; samples: number; cpu_avg: number | null; cpu_max: number | null;
    cpu_core_hours: number; mem_max: number | null; rx_bytes: number; tx_bytes: number; up_frac: number | null }[];
  tsdb: Record<string, { files: number; bytes: number }>;
};
let period = sessionStorage.getItem('hsm-period') ?? '24h';

function meter(label: string, used: number, total: number, unit: (v: number) => string): HTMLElement {
  return h('div', {}, h('div', { class: 'small' }, `${label}: ${unit(used)} of ${unit(total)}`), bar(total ? (used / total) * 100 : null));
}

const of = (a: number | null, q: number | null, f: (v: number) => string) =>
  `${f(a ?? 0)} / ${q == null ? '∞' : f(q)}`;

async function load(): Promise<void> {
  document.querySelectorAll<HTMLButtonElement>('#periods button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.p === period)));
  const u = await api<Usage>(`/api/admin/usage?period=${period}`);
  const mib = (v: number) => bytes(v * 2 ** 20);
  document.getElementById('host')!.replaceChildren(
    meter('CPU cores', u.allocated.cpus ?? 0, u.host.cpus, (v) => String(v)),
    meter('Memory', u.allocated.memory_mib ?? 0, u.host.memory_mib, mib),
    ...u.host.pools.map((p) => meter(`Disk on ${p.name} (${p.driver}, used on pool)`, p.used_gib, p.total_gib, (v) => `${v} GiB`)),
    h('div', {}, h('div', { class: 'small' }, `Disk allocated by containers: ${u.allocated.disk_gib ?? 0} GiB`)));
  document.getElementById('unlimited')!.textContent = u.unlimited_containers.length
    ? `Not counted (no CPU/memory limit set): ${u.unlimited_containers.join(', ')} — set limits on them to include them.` : '';
  document.getElementById('per-user')!.replaceChildren(...u.users.map((x) => h('tr', {},
    h('td', {}, x.email, h('span', { class: 'muted small' }, ` (${x.role})`)),
    h('td', { class: 'num' }, of(x.allocated.cpus, x.quota.cpus, String)),
    h('td', { class: 'num' }, of(x.allocated.memory_mib, x.quota.memory_mib, mib)),
    h('td', { class: 'num' }, of(x.allocated.disk_gib, x.quota.disk_gib, (v) => `${v} GiB`)))));
  document.getElementById('per-container')!.replaceChildren(...u.containers.map((c) => h('tr', {},
    h('td', {}, h('a', { href: `/container/?name=${encodeURIComponent(c.name)}` }, c.name)),
    h('td', {}, c.owner ?? '–'),
    h('td', { class: 'right num' }, pct(c.cpu_avg)), h('td', { class: 'right num' }, pct(c.cpu_max)),
    h('td', { class: 'right num' }, c.cpu_core_hours.toFixed(2)), h('td', { class: 'right num' }, bytes(c.mem_max)),
    h('td', { class: 'right num' }, bytes(c.rx_bytes)), h('td', { class: 'right num' }, bytes(c.tx_bytes)),
    h('td', { class: 'right num' }, c.up_frac == null ? 'no data' : pct(c.up_frac * 100, 0)))));
  const t = u.tsdb;
  document.getElementById('tsdb')!.textContent = `Metric store: ${Object.entries(t).map(([k, v]) => `${k} ${v.files} files ${bytes(v.bytes)}`).join(' · ')}. Data from the rollup tiers; a period with no samples shows “no data”.`;
}

(async () => {
  const me = await initChrome();
  if (me.role !== 'admin') { location.assign('/'); return; }
  document.querySelectorAll<HTMLButtonElement>('#periods button').forEach((b) => b.onclick = () => {
    period = b.dataset.p!;
    sessionStorage.setItem('hsm-period', period);
    void load().catch((e) => toast((e as Error).message));
  });
  await load();
})().catch((e) => toast((e as Error).message));
