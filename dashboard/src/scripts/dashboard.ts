import { api, bytes, h, initChrome, toast, toLogin, type Me } from './api';

type Container = { name: string; status: string; image: string; cpus: string; memory: string };
type Metric = {
  status: string; cpu_pct: number | null; mem_used: number; mem_total: number;
  disk_used: number; disk_total: number; rx_bps: number | null; tx_bps: number | null;
};
type Card = { root: HTMLElement; pill: HTMLElement; bars: Record<string, [HTMLElement, HTMLElement]>; data: Container };

const grid = document.getElementById('grid')!;
const live = document.getElementById('live')!;
const cards = new Map<string, Card>();
let me: Me;

const $form = (id: string) => document.getElementById(id) as HTMLFormElement;
const $dlg = (id: string) => document.getElementById(id) as HTMLDialogElement;
const memMiB = (m: string) => {
  const n = parseInt(m, 10);
  return Number.isNaN(n) ? 512 : /GiB|GB/.test(m) ? n * 1024 : n;
};

// ---- rendering --------------------------------------------------------------
function metricRow(label: string): [HTMLElement, HTMLElement, HTMLElement] {
  const fill = h('i');
  const val = h('span', { class: 'val' }, '–');
  return [h('div', { class: 'metric' }, h('span', {}, label), h('div', { class: 'bar' }, fill), val), fill, val];
}

function action(label: string, fn: () => Promise<unknown>, cls = ''): HTMLButtonElement {
  const b = h('button', { class: cls }, label);
  b.onclick = async () => {
    b.disabled = true;
    try { await fn(); } catch (e) { toast((e as Error).message); } finally { b.disabled = false; }
  };
  return b;
}

function buildCard(c: Container): Card {
  const pill = h('span', { class: `pill ${c.status}` }, c.status);
  const bars: Card['bars'] = {};
  const rows = ['CPU', 'Memory', 'Disk', 'Net'].map((k) => {
    const [row, fill, val] = metricRow(k);
    bars[k] = [fill, val];
    return row;
  });
  const limits = `${c.cpus ? `${c.cpus} CPU` : 'no CPU limit'} · ${c.memory || 'no memory limit'}`;
  const actions = h('div', { class: 'actions' });
  const term = h('a', { class: 'btn', href: `/terminal/?name=${encodeURIComponent(c.name)}` }, '›_ Terminal');
  actions.append(term);
  if (me.role === 'admin') {
    const act = (a: string) => async () => { await api(`/api/containers/${c.name}/actions`, 'POST', { action: a }); await load(); };
    actions.append(
      action(c.status === 'Running' ? 'Stop' : 'Start', act(c.status === 'Running' ? 'stop' : 'start')),
      action('Restart', act('restart')),
      action('Limits', async () => openLimits(c)),
      action('Delete', async () => openDelete(c.name), 'danger'),
    );
  }
  term.hidden = c.status !== 'Running';
  const root = h('article', { class: 'card' },
    h('header', {}, h('span', { class: 'name' }, c.name), pill),
    h('div', { class: 'meta muted small' }, `${c.image || 'unknown image'} — ${limits}`),
    ...rows, actions);
  return { root, pill, bars, data: c };
}

function setBar(card: Card, key: string, pct: number | null, text: string): void {
  const [fill, val] = card.bars[key];
  const p = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  fill.style.width = `${p}%`;
  fill.className = p > 90 ? 'crit' : p > 70 ? 'hot' : '';
  val.textContent = text;
}

function applyMetric(card: Card, m: Metric): void {
  card.pill.textContent = m.status;
  card.pill.className = `pill ${m.status}`;
  const cpus = parseInt(card.data.cpus, 10) || navigator.hardwareConcurrency || 1;
  const cpu = m.cpu_pct;
  // cpu_pct is per core (100% = one core); the bar scales by the CPU limit.
  setBar(card, 'CPU', cpu == null ? null : cpu / cpus, cpu == null ? '…' : `${cpu.toFixed(1)}%`);
  setBar(card, 'Memory', m.mem_total ? (m.mem_used / m.mem_total) * 100 : null,
    m.mem_used ? `${bytes(m.mem_used)} / ${bytes(m.mem_total)}` : '–');
  setBar(card, 'Disk', m.disk_total ? (m.disk_used / m.disk_total) * 100 : null,
    m.disk_used ? (m.disk_total ? `${bytes(m.disk_used)} / ${bytes(m.disk_total)}` : bytes(m.disk_used)) : 'n/a');
  setBar(card, 'Net', null, m.rx_bps == null ? '…' : `↓${bytes(m.rx_bps)}/s ↑${bytes(m.tx_bps)}/s`);
}

async function load(): Promise<void> {
  const list = await api<Container[]>('/api/containers');
  cards.clear();
  grid.replaceChildren(...list.map((c) => {
    const card = buildCard(c);
    cards.set(c.name, card);
    return card.root;
  }));
  document.getElementById('empty')!.hidden = list.length > 0;
}

// ---- live metrics (one EventSource per tab; server shares one LXD poll) -------
function stream(): void {
  const es = new EventSource('/api/metrics/stream');
  es.addEventListener('metrics', (ev) => {
    const snap = JSON.parse((ev as MessageEvent).data) as { ts: number; error?: string; containers: Record<string, Metric> };
    live.textContent = snap.error ? `⚠ ${snap.error}` : `live · updated ${new Date(snap.ts * 1000).toLocaleTimeString()}`;
    const names = Object.keys(snap.containers);
    // Reload the list when containers appear/disappear or change state.
    const changed = names.length !== cards.size ||
      names.some((n) => !cards.has(n) || cards.get(n)!.data.status !== snap.containers[n].status);
    if (changed && !snap.error) void load();
    for (const n of names) {
      const card = cards.get(n);
      if (card) applyMetric(card, snap.containers[n]);
    }
  });
  es.addEventListener('expired', () => { es.close(); toLogin(); });
  es.onerror = () => { live.textContent = 'reconnecting…'; };
  // Close explicitly so the server stops polling as soon as nobody watches.
  addEventListener('pagehide', () => es.close());
}

// ---- dialogs ----------------------------------------------------------------
function wireDialog(dlgId: string, formId: string, errId: string, submit: (fd: FormData) => Promise<void>): void {
  const dlg = $dlg(dlgId);
  const form = $form(formId);
  const err = document.getElementById(errId)!;
  dlg.querySelector('[data-close]')!.addEventListener('click', () => dlg.close());
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector('button[type=submit]') as HTMLButtonElement;
    btn.disabled = true;
    err.textContent = '';
    try {
      await submit(new FormData(form));
      dlg.close();
      await load();
    } catch (e) {
      err.textContent = (e as Error).message;
    } finally {
      btn.disabled = false;
    }
  });
}

let limitsTarget = '';
function openLimits(c: Container): void {
  limitsTarget = c.name;
  document.getElementById('limits-name')!.textContent = c.name;
  const f = $form('limits-form');
  (f.elements.namedItem('cpus') as HTMLInputElement).value = c.cpus || '1';
  (f.elements.namedItem('memory_mib') as HTMLInputElement).value = String(memMiB(c.memory));
  $dlg('limits-dlg').showModal();
}

let deleteTarget = '';
function openDelete(name: string): void {
  deleteTarget = name;
  document.getElementById('delete-name')!.textContent = name;
  $form('delete-form').reset();
  $dlg('delete-dlg').showModal();
}

async function setupAdmin(): Promise<void> {
  const info = await api<{ images: string[]; max_cpus: number; max_memory_mib: number }>('/api/images');
  const select = document.getElementById('image-select')!;
  // `selected` (not just .value) so form.reset() keeps the sensible default.
  select.replaceChildren(...info.images.map((i) => {
    const o = h('option', { value: i }, i);
    if (i === 'ubuntu/24.04') o.setAttribute('selected', '');
    return o;
  }));
  for (const id of ['create-form', 'limits-form']) {
    const f = $form(id);
    (f.elements.namedItem('cpus') as HTMLInputElement).max = String(info.max_cpus);
    (f.elements.namedItem('memory_mib') as HTMLInputElement).max = String(info.max_memory_mib);
  }
  const btn = document.getElementById('new')!;
  btn.hidden = false;
  btn.onclick = () => { $form('create-form').reset(); $dlg('create-dlg').showModal(); };

  const nums = (fd: FormData) => ({ cpus: Number(fd.get('cpus')), memory_mib: Number(fd.get('memory_mib')) });
  wireDialog('create-dlg', 'create-form', 'create-err', async (fd) => {
    toast('Creating… the first pull of an image can take a minute.');
    await api('/api/containers', 'POST', { name: fd.get('name'), image: fd.get('image'), ...nums(fd) });
    toast(`Created ${fd.get('name')}`);
  });
  wireDialog('limits-dlg', 'limits-form', 'limits-err', async (fd) => {
    await api(`/api/containers/${limitsTarget}`, 'PATCH', nums(fd));
  });
  wireDialog('delete-dlg', 'delete-form', 'delete-err', async (fd) => {
    if (fd.get('confirm') !== deleteTarget) throw new Error('Name does not match');
    await api(`/api/containers/${deleteTarget}`, 'DELETE');
    toast(`Deleted ${deleteTarget}`);
  });
}

(async () => {
  me = await initChrome();
  await load();
  if (me.role === 'admin') await setupAdmin();
  stream();
})().catch((e) => toast((e as Error).message));
