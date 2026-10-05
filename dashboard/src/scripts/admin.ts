import { api, h, initChrome, toast, type Me } from './api';

type User = { id: number; email: string; name: string; role: string; last_login_at: number | null; containers: string[] };
type Audit = { id: number; ts: number; actor_email: string | null; action: string; target: string | null; detail: string | null; ip: string | null };

let me: Me;
let containerNames: string[] = [];
let oldestAudit: number | undefined;

const run = async (fn: () => Promise<unknown>) => {
  try { await fn(); } catch (e) { toast((e as Error).message); }
};
const when = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : 'never');

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
    const x = h('button', { title: `Unassign ${c}`, 'aria-label': `Unassign ${c}` }, '×');
    x.onclick = () => run(async () => { await api(`/api/admin/users/${u.id}/containers/${c}`, 'DELETE'); await loadUsers(); });
    return h('span', { class: 'chip' }, c, x);
  }));
  if (!u.containers.length) chips.append(h('span', { class: 'muted small' }, u.role === 'admin' ? 'all (admin)' : 'none'));

  const free = containerNames.filter((c) => !u.containers.includes(c));
  const pick = h('select', {}, h('option', { value: '' }, '—'), ...free.map((c) => h('option', { value: c }, c)));
  const add = h('button', {}, 'Assign');
  add.onclick = () => run(async () => {
    if (!pick.value) return;
    await api(`/api/admin/users/${u.id}/containers/${pick.value}`, 'PUT');
    await loadUsers();
  });

  const del = h('button', { class: 'danger', disabled: self }, 'Remove');
  let armed = false;  // two-click confirm instead of window.confirm()
  del.onclick = () => run(async () => {
    if (!armed) { armed = true; del.textContent = 'Confirm?'; setTimeout(() => { armed = false; del.textContent = 'Remove'; }, 3000); return; }
    await api(`/api/admin/users/${u.id}`, 'DELETE');
    await loadUsers();
  });

  return h('tr', {},
    h('td', {}, h('div', {}, u.email), h('div', { class: 'muted small' }, `${u.name || ''} last login: ${when(u.last_login_at)}`)),
    h('td', {}, role),
    h('td', {}, chips),
    h('td', {}, u.role === 'admin' ? h('span', { class: 'muted small' }, '—') : h('div', { class: 'inline' }, pick, add)),
    h('td', {}, del));
}

async function loadUsers(): Promise<void> {
  const [users, containers] = await Promise.all([
    api<User[]>('/api/admin/users'),
    api<{ name: string }[]>('/api/containers'),
  ]);
  containerNames = containers.map((c) => c.name);
  document.getElementById('users')!.replaceChildren(...users.map(userRow));
}

async function loadAudit(more = false): Promise<void> {
  const q = more && oldestAudit ? `&before=${oldestAudit}` : '';
  const rows = await api<Audit[]>(`/api/admin/audit?limit=50${q}`);
  const body = document.getElementById('audit')!;
  const trs = rows.map((r) => h('tr', {},
    h('td', { class: 'small' }, when(r.ts)), h('td', {}, r.actor_email ?? '—'), h('td', {}, r.action),
    h('td', {}, r.target ?? ''), h('td', { class: 'small muted' }, r.detail ?? ''), h('td', { class: 'small muted' }, r.ip ?? '')));
  if (more) body.append(...trs); else body.replaceChildren(...trs);
  if (rows.length) oldestAudit = rows[rows.length - 1].id;
  (document.getElementById('audit-more') as HTMLButtonElement).hidden = rows.length < 50;
}

(async () => {
  me = await initChrome();
  if (me.role !== 'admin') { location.assign('/'); return; }
  const form = document.getElementById('add-user') as HTMLFormElement;
  form.onsubmit = (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    void run(async () => {
      await api('/api/admin/users', 'POST', { email: String(fd.get('email')).trim().toLowerCase(), role: fd.get('role') });
      form.reset();
      await loadUsers();
    });
  };
  document.getElementById('audit-refresh')!.onclick = () => run(() => loadAudit());
  document.getElementById('audit-more')!.onclick = () => run(() => loadAudit(true));
  await Promise.all([loadUsers(), loadAudit()]);
})().catch((e) => toast((e as Error).message));
