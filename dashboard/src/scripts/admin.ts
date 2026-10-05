import { api, h, initChrome, toast, type Me, type Quota } from './api';

type User = { id: number; email: string; name: string; role: string; status: string; last_login_at: number | null;
  containers: string[]; quota: Quota; allocated: Quota };
type Audit = { id: number; ts: number; actor_email: string | null; action: string; target: string | null; detail: string | null; ip: string | null };

let me: Me;
let containerNames: string[] = [];
let oldestAudit: number | undefined;

const run = async (fn: () => Promise<unknown>) => {
  try { await fn(); } catch (e) { toast((e as Error).message); }
};
const when = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : 'never');
const num = (v: FormDataEntryValue | null) => (v === null || v === '' ? null : Number(v));

function quotaCell(u: User): HTMLElement {
  const input = (k: keyof Quota, step: number) => h('input', { type: 'number', min: '0', step: String(step),
    value: u.quota[k] == null ? '' : String(u.quota[k]), placeholder: '∞', 'aria-label': `${k} quota for ${u.email}` });
  const cpus = input('cpus', 1), mem = input('memory_mib', 128), disk = input('disk_gib', 1);
  for (const i of [cpus, mem, disk]) (i as HTMLInputElement).style.width = '6.5em';
  const save = h('button', {}, 'Save');
  save.onclick = () => run(async () => {
    await api(`/api/admin/users/${u.id}`, 'PATCH', { quota: {
      cpus: num((cpus as HTMLInputElement).value), memory_mib: num((mem as HTMLInputElement).value),
      disk_gib: num((disk as HTMLInputElement).value) } });
    toast(`Quota saved for ${u.email}`);
    await loadUsers();
  });
  const a = u.allocated;
  const over = (['cpus', 'memory_mib', 'disk_gib'] as const).some((k) => u.quota[k] != null && (a[k] ?? 0) > u.quota[k]!);
  return h('td', {},
    h('div', { class: 'small muted' }, `${a.cpus ?? 0} CPU · ${a.memory_mib ?? 0} MiB · ${a.disk_gib ?? 0} GiB allocated`,
      over ? h('strong', { class: 'error' }, ' · over quota') : null),
    h('div', { class: 'inline' }, cpus, mem, disk, save));
}

function userRow(u: User): HTMLTableRowElement {
  const self = u.id === me.id;
  const role = h('select', { disabled: self },
    ...['user', 'admin'].map((r) => h('option', { value: r, selected: u.role === r }, r)));
  role.onchange = () => run(async () => {
    await api(`/api/admin/users/${u.id}`, 'PATCH', { role: role.value });
    toast(`${u.email} is now ${role.value}`);
    await loadUsers();
  });
  const chips = h('div', { class: 'chips' }, ...u.containers.map((c) => {
    const x = h('button', { title: `Revoke ${c}`, 'aria-label': `Revoke access to ${c}` }, '×');
    x.onclick = () => run(async () => { await api(`/api/admin/users/${u.id}/containers/${c}`, 'DELETE'); await loadUsers(); });
    return h('span', { class: 'chip' }, c, x);
  }));
  if (!u.containers.length) chips.append(h('span', { class: 'muted small' }, u.role === 'admin' ? 'all (admin)' : 'none'));
  const pick = h('select', {}, h('option', { value: '' }, '—'),
    ...containerNames.filter((c) => !u.containers.includes(c)).map((c) => h('option', { value: c }, c)));
  const add = h('button', {}, 'Grant');
  add.onclick = () => run(async () => {
    if (!pick.value) return;
    await api(`/api/admin/users/${u.id}/containers/${pick.value}`, 'PUT');
    await loadUsers();
  });
  const del = h('button', { class: 'danger', disabled: self }, 'Revoke user');
  let armed = false;  // two-click confirm instead of window.confirm()
  del.onclick = () => run(async () => {
    if (!armed) {
      armed = true; del.textContent = 'Confirm?';
      setTimeout(() => { armed = false; del.textContent = 'Revoke user'; }, 3000);
      return;
    }
    await api(`/api/admin/users/${u.id}`, 'DELETE');
    toast(`${u.email} revoked; their sessions are closed`);
    await loadUsers();
  });
  return h('tr', {},
    h('td', {}, h('div', {}, u.email),
      h('div', { class: 'muted small' }, `${u.status === 'invited' ? 'invited, never signed in' : `last login ${when(u.last_login_at)}`}`)),
    h('td', {}, role), quotaCell(u), h('td', {}, chips),
    h('td', {}, u.role === 'admin' ? h('span', { class: 'muted small' }, '—') : h('div', { class: 'inline' }, pick, add)),
    h('td', {}, del));
}

async function loadUsers(): Promise<void> {
  const [users, list] = await Promise.all([
    api<User[]>('/api/admin/users'),
    api<{ containers: { name: string }[] }>('/api/containers'),
  ]);
  containerNames = list.containers.map((c) => c.name);
  document.getElementById('users')!.replaceChildren(...users.map(userRow));
}

async function loadAudit(more = false): Promise<void> {
  const q = more && oldestAudit ? `&before=${oldestAudit}` : '';
  const rows = await api<Audit[]>(`/api/admin/audit?limit=50${q}`);
  const body = document.getElementById('audit')!;
  const trs = rows.map((r) => h('tr', {},
    h('td', { class: 'small' }, when(r.ts)), h('td', {}, r.actor_email ?? 'system'), h('td', {}, r.action),
    h('td', {}, r.target ?? ''), h('td', { class: 'small muted' }, r.detail ?? ''), h('td', { class: 'small muted' }, r.ip ?? '')));
  if (more) body.append(...trs); else body.replaceChildren(...trs);
  if (rows.length) oldestAudit = rows[rows.length - 1].id;
  (document.getElementById('audit-more') as HTMLButtonElement).hidden = rows.length < 50;
}

(async () => {
  me = await initChrome();
  if (me.role !== 'admin') { location.assign('/'); return; }
  const form = document.getElementById('invite') as HTMLFormElement;
  form.onsubmit = (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    void run(async () => {
      await api('/api/admin/users', 'POST', {
        email: String(fd.get('email')).trim().toLowerCase(), role: fd.get('role'),
        quota: { cpus: num(fd.get('cpus')), memory_mib: num(fd.get('memory_mib')), disk_gib: num(fd.get('disk_gib')) } });
      toast(`Invited ${fd.get('email')}`);
      form.reset();
      await Promise.all([loadUsers(), loadAudit()]);
    });
  };
  document.getElementById('audit-refresh')!.onclick = () => run(() => loadAudit());
  document.getElementById('audit-more')!.onclick = () => run(() => loadAudit(true));
  await Promise.all([loadUsers(), loadAudit()]);
})().catch((e) => toast((e as Error).message));
