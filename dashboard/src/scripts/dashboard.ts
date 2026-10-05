import { api, banner, bar, bytes, duration, h, initChrome, pct, toast, toLogin, type Me, type Meta } from './api';

export type Live = {
  name: string; uuid: string; status: string; image: string; ipv4: string | null; uptime_s: number | null;
  procs: number | null; cpus: number | null; cpu_allowance: string | null; cpu_pct: number | null;
  mem_used: number; mem_limit: number; disk_used: number | null; disk_limit: number | null;
  rx_bps: number | null; tx_bps: number | null; pool: string | null;
  owner?: string | null; pending?: boolean; allocated?: Record<string, number | null>;
};
type Job = { id: string; name: string; status: string; error: string | null };
type Card = { root: HTMLElement; set: (c: Live, stale: boolean) => void; status: string };

const grid = document.getElementById('grid')!;
const live = document.getElementById('live')!;
const cards = new Map<string, Card>();
let me: Me;

// ---- cards -------------------------------------------------------------------
function metricRow(label: string) {
  const fillHost: HTMLElement = h('div', { class: 'bar' });
  const val = h('span', { class: 'val' }, '–');
  return { row: h('div', { class: 'metric' }, h('span', {}, label), fillHost, val), fillHost, val };
}

function act(label: string, fn: () => Promise<unknown>, cls = ''): HTMLButtonElement {
  const b = h('button', { class: cls }, label);
  b.onclick = async () => {
    b.disabled = true;
    try { await fn(); } catch (e) { toast((e as Error).message); } finally { b.disabled = false; }
  };
  return b;
}

function buildCard(c: Live): Card {
  const pill = h('span', { class: 'pill' });
  const meta = h('div', { class: 'meta muted small' });
  const rows = { cpu: metricRow('CPU'), mem: metricRow('Memory'), disk: metricRow('Disk'), net: metricRow('Net') };
  const facts = h('dl', { class: 'facts' });
  const actions = h('div', { class: 'actions' });
  const root = h('article', { class: 'card' },
    h('header', {}, h('a', { class: 'name', href: `/container/?name=${encodeURIComponent(c.name)}` }, c.name), pill),
    meta, ...Object.values(rows).map((r) => r.row), facts, actions);

  const card: Card = { root, status: c.status, set: (x, stale) => {
    card.status = x.status;
    pill.textContent = x.pending ? 'creating…' : x.status;
    pill.className = `pill ${x.pending ? 'pending' : x.status}`;
    root.classList.toggle('pending', !!x.pending);
    root.classList.toggle('stale', stale);
    const lim = x.cpus ? `${x.cpus} CPU${x.cpu_allowance ? ` (${x.cpu_allowance})` : ''}` : 'no CPU limit';
    meta.textContent = `${x.image || 'image unknown'} — ${lim}${x.owner ? ` — owner ${x.owner}` : ''}`;
    const cores = x.cpus || navigator.hardwareConcurrency || 1;
    set(rows.cpu, x.cpu_pct == null ? null : x.cpu_pct / cores, pct(x.cpu_pct));
    set(rows.mem, x.mem_limit ? (x.mem_used / x.mem_limit) * 100 : null,
      `${bytes(x.mem_used)} / ${bytes(x.mem_limit)}`);
    set(rows.disk, x.disk_limit && x.disk_used != null ? (x.disk_used / x.disk_limit) * 100 : null,
      x.disk_used == null ? 'n/a (pool has no quota)' : `${bytes(x.disk_used)}${x.disk_limit ? ` / ${bytes(x.disk_limit)}` : ''}`);
    set(rows.net, null, x.rx_bps == null ? '…' : `↓${bytes(x.rx_bps)}/s ↑${bytes(x.tx_bps)}/s`);
    facts.replaceChildren(
      h('dt', {}, 'IPv4'), h('dd', {}, x.ipv4 ?? '–'),
      h('dt', {}, 'Uptime'), h('dd', {}, x.status === 'Running' ? duration(x.uptime_s) : '–'),
      h('dt', {}, 'Processes'), h('dd', {}, x.procs == null ? '–' : String(x.procs)),
      h('dt', {}, 'Pool'), h('dd', {}, x.pool ?? '–'));
    buildActions(actions, x);
  } };
  card.set(c, false);
  return card;
}

function set(r: ReturnType<typeof metricRow>, p: number | null, text: string): void {
  r.fillHost.replaceWith(r.fillHost = bar(p));
  r.val.textContent = text;
}

function buildActions(el: HTMLElement, c: Live): void {
  const items: Node[] = [];
  if (!c.pending) {
    items.push(h('a', { class: 'btn', href: `/container/?name=${encodeURIComponent(c.name)}` }, 'Details'));
    if (c.status === 'Running') items.push(h('a', { class: 'btn', href: `/terminal/?name=${encodeURIComponent(c.name)}` }, '›_ Terminal'));
  }
  if (me.role === 'admin' && !c.pending) {
    const state = (a: string) => async () => { await api(`/api/containers/${c.name}/state`, 'POST', { action: a }); toast(`${c.name}: ${a} done`); await load(); };
    if (c.status === 'Running') items.push(act('Stop', state('stop')), act('Restart', state('restart')), act('Freeze', state('freeze')));
    else if (c.status === 'Frozen') items.push(act('Unfreeze', state('unfreeze')));
    else items.push(act('Start', state('start')));
    items.push(act('Delete', async () => openDelete(c.name), 'danger'));
  }
  el.replaceChildren(...items);
}

function render(list: Live[], meta: Meta, jobs: Job[] = []): void {
  document.getElementById('loading')!.hidden = true;
  const stale = !meta.collector_ok || meta.lxd_ok === false;
  const pending = jobs.filter((j) => !list.some((c) => c.name === j.name))
    .map((j) => ({ name: j.name, status: 'Creating', pending: true } as Live));
  const all = [...list, ...pending];
  const seen = new Set(all.map((c) => c.name));
  for (const [name, card] of cards) if (!seen.has(name)) { card.root.remove(); cards.delete(name); }
  for (const c of all) {
    const card = cards.get(c.name);
    if (card) card.set({ ...c, pending: c.pending }, stale);
    else { const n = buildCard(c); cards.set(c.name, n); }
  }
  grid.replaceChildren(...[...cards.values()].sort((a, b) => a.root.querySelector('.name')!.textContent!
    .localeCompare(b.root.querySelector('.name')!.textContent!)).map((c) => c.root));
  const empty = document.getElementById('empty')!;
  empty.hidden = all.length > 0;
  empty.textContent = me.role === 'admin'
    ? 'No containers yet. Create one with “+ New container”.'
    : 'No containers are assigned to you yet. Ask an administrator for access.';
  banner(meta);
  live.textContent = meta.ts ? `updated ${new Date(meta.ts * 1000).toLocaleTimeString()} · collector every ${meta.interval}s` : 'waiting for first sample…';
}

async function load(): Promise<void> {
  const r = await api<{ meta: Meta; containers: Live[]; jobs: Job[] }>('/api/containers');
  render(r.containers, r.meta, r.jobs);
  if (me.role !== 'admin') renderQuota();
}

// ---- live updates: one EventSource per tab; the server reads one shared file --
function stream(): void {
  const es = new EventSource('/api/live');
  es.addEventListener('snapshot', (ev) => {
    const snap = JSON.parse((ev as MessageEvent).data) as { meta: Meta; containers: Record<string, Live> };
    const known = [...cards.keys()].filter((n) => !cards.get(n)!.root.classList.contains('pending'));
    const names = Object.keys(snap.containers);
    if (names.length !== known.length || names.some((n) => !cards.has(n))) { void load(); return; }
    for (const [name, c] of Object.entries(snap.containers)) cards.get(name)?.set(c, !snap.meta.collector_ok || snap.meta.lxd_ok === false);
    banner(snap.meta);
    if (snap.meta.ts) live.textContent = `updated ${new Date(snap.meta.ts * 1000).toLocaleTimeString()} · collector every ${snap.meta.interval}s`;
  });
  es.addEventListener('expired', () => { es.close(); toLogin(); });
  es.onerror = () => { live.textContent = 'reconnecting…'; };
  addEventListener('pagehide', () => es.close());
}

// ---- my quota (container users) ------------------------------------------------
async function renderQuota(): Promise<void> {
  me = await api<Me>('/api/me');
  const box = document.getElementById('quota-bars')!;
  const rows = ([['cpus', 'CPU cores', (v: number) => `${v}`], ['memory_mib', 'Memory', (v: number) => bytes(v * 2 ** 20)],
    ['disk_gib', 'Disk', (v: number) => `${v} GiB`]] as const).map(([k, label, f]) => {
    const used = me.allocated[k] ?? 0, q = me.quota[k];
    return h('div', {}, h('div', { class: 'small' }, `${label}: ${f(used)} of ${q == null ? 'unlimited' : f(q)}`),
      bar(q ? (used / q) * 100 : null));
  });
  box.replaceChildren(...rows);
  document.getElementById('my-quota')!.hidden = false;
}

// ---- create ------------------------------------------------------------------
type Bounds = Record<string, { min: number; max: number } | null>;
type Options = {
  host: { cpus: number; memory_bytes: number };
  pools: { name: string; driver: string; sizable: boolean; total_bytes: number; used_bytes: number }[];
  networks: { name: string }[]; profiles: string[]; images: { key: string; cached: boolean }[];
  owner: { id: number; email: string; remaining: Record<string, number | null> };
  bounds: Record<string, Bounds>; users: { id: number; email: string }[];
};
const $form = (id: string) => document.getElementById(id) as HTMLFormElement;
const $dlg = (id: string) => document.getElementById(id) as HTMLDialogElement;
let opts: Options | null = null;

function slider(name: string): { input: HTMLInputElement; out: HTMLOutputElement } {
  const input = $form('create-form').elements.namedItem(name) as HTMLInputElement;
  const out = input.parentElement!.querySelector('output')!;
  input.oninput = () => (out.value = input.value);
  return { input, out };
}

function applyBounds(): void {
  if (!opts) return;
  const f = $form('create-form');
  const pool = (f.elements.namedItem('pool') as HTMLSelectElement).value;
  const b = opts.bounds[pool];
  for (const k of ['cpus', 'memory_mib', 'disk_gib']) {
    const s = slider(k);
    const range = b[k];
    if (!range) continue;
    s.input.min = String(range.min);
    s.input.max = String(Math.max(range.min, range.max));
    s.input.disabled = range.max < range.min;
    if (Number(s.input.value) > range.max) s.input.value = String(range.max);
    if (Number(s.input.value) < range.min) s.input.value = String(range.min);
    s.out.value = s.input.value;
  }
  slider('cpu_allowance').out.value = slider('cpu_allowance').input.value;
  const sizable = !!b.disk_gib;
  document.getElementById('disk-field')!.hidden = !sizable;
  const p = opts.pools.find((x) => x.name === pool)!;
  document.getElementById('disk-hint')!.textContent =
    `${bytes(p.total_bytes - p.used_bytes)} free on ${p.name} (${p.driver})`;
  const r = opts.owner.remaining;
  document.getElementById('owner-hint')!.textContent = r.cpus == null && r.memory_mib == null
    ? 'no quota (bounded by host only)'
    : `quota left: ${r.cpus ?? '∞'} CPU, ${r.memory_mib ?? '∞'} MiB, ${r.disk_gib ?? '∞'} GiB`;
}

async function loadOptions(ownerId?: number): Promise<void> {
  opts = await api<Options>(`/api/containers/options${ownerId ? `?owner_id=${ownerId}` : ''}`);
  const sel = (id: string, items: [string, string, boolean?][]) =>
    (document.getElementById(id) as HTMLSelectElement).replaceChildren(
      ...items.map(([v, label, selected]) => h('option', { value: v, selected: !!selected }, label)));
  sel('owner-select', opts.users.map((u) => [String(u.id), u.email, u.id === opts!.owner.id]));
  sel('image-select', opts.images.map((i) => [i.key, `${i.key}${i.cached ? ' (cached)' : ' (download)'}`, i.key === 'ubuntu/24.04']));
  const sizable = opts.pools.find((p) => p.sizable);
  sel('pool-select', opts.pools.map((p) => [p.name, `${p.name} (${p.driver}${p.sizable ? '' : ', no disk quota'})`, p === sizable]));
  sel('network-select', [['', '(profile default)'], ...opts.networks.map((n) => [n.name, n.name] as [string, string])]);
  sel('profile-select', opts.profiles.map((p) => [p, p, p === 'default']));
  document.getElementById('create-loading')!.hidden = true;
  document.getElementById('create-fields')!.hidden = false;
  applyBounds();
}

async function pollJob(id: string, name: string): Promise<void> {
  for (let i = 0; i < 300; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    const j = await api<Job>(`/api/jobs/${id}`).catch(() => null);
    if (j && j.status !== 'running') {
      toast(j.status === 'done' ? `${name} created` : `Creating ${name} failed: ${j.error}`);
      await load();
      return;
    }
  }
}

function setupCreate(): void {
  const f = $form('create-form');
  const err = document.getElementById('create-err')!;
  (f.elements.namedItem('pool') as HTMLSelectElement).onchange = applyBounds;
  (f.elements.namedItem('owner_id') as HTMLSelectElement).onchange = (e) =>
    void loadOptions(Number((e.target as HTMLSelectElement).value)).catch((x) => (err.textContent = x.message));
  const btn = document.getElementById('new')!;
  btn.hidden = false;
  btn.onclick = () => {
    err.textContent = '';
    $dlg('create-dlg').showModal();
    loadOptions().catch((x) => (err.textContent = x.message));
  };
  f.querySelector('[data-close]')!.addEventListener('click', () => $dlg('create-dlg').close());
  f.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData(f);
    const submit = f.querySelector('button[type=submit]') as HTMLButtonElement;
    const sizable = !document.getElementById('disk-field')!.hidden;
    const body = {
      name: fd.get('name'), image: fd.get('image'), pool: fd.get('pool'), owner_id: Number(fd.get('owner_id')),
      cpus: Number(fd.get('cpus')), cpu_allowance: Number(fd.get('cpu_allowance')), memory_mib: Number(fd.get('memory_mib')),
      disk_gib: sizable ? Number(fd.get('disk_gib')) : null, network: fd.get('network') || null,
      profiles: fd.getAll('profiles').length ? fd.getAll('profiles') : ['default'],
      description: fd.get('description') ?? '', autostart: fd.get('autostart') === 'on', ephemeral: fd.get('ephemeral') === 'on',
    };
    submit.disabled = true;
    err.textContent = '';
    try {
      const job = await api<Job>('/api/containers', 'POST', body);
      $dlg('create-dlg').close();
      f.reset();
      toast(`Creating ${body.name}… the first download of an image can take a minute.`);
      await load();
      void pollJob(job.id, String(body.name));
    } catch (e) {
      err.textContent = (e as Error).message;
    } finally {
      submit.disabled = false;
    }
  });
}

// ---- delete ------------------------------------------------------------------
let deleteTarget = '';
function openDelete(name: string): void {
  deleteTarget = name;
  document.getElementById('delete-name')!.textContent = name;
  document.getElementById('delete-err')!.textContent = '';
  $form('delete-form').reset();
  $dlg('delete-dlg').showModal();
}

function setupDelete(): void {
  const f = $form('delete-form');
  f.querySelector('[data-close]')!.addEventListener('click', () => $dlg('delete-dlg').close());
  f.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const err = document.getElementById('delete-err')!;
    if (new FormData(f).get('confirm') !== deleteTarget) { err.textContent = 'Name does not match'; return; }
    try {
      await api(`/api/containers/${deleteTarget}`, 'DELETE');
      $dlg('delete-dlg').close();
      toast(`Deleted ${deleteTarget}`);
      await load();
    } catch (e) {
      err.textContent = (e as Error).message;
    }
  });
}

(async () => {
  me = await initChrome();
  try {
    await load();
  } catch (e) {
    document.getElementById('loading')!.textContent = `Could not load containers: ${(e as Error).message}`;
    return;
  }
  if (me.role === 'admin') { setupCreate(); setupDelete(); }
  stream();
})().catch((e) => toast((e as Error).message));
