import '@xterm/xterm/css/xterm.css';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { initChrome, NAME_RE, toLogin } from './api';

// Close codes defined by the backend (see backend/app/terminal.py).
const REASONS: Record<number, string> = {
  1000: 'Session ended.',
  1006: 'Connection lost.',
  1008: 'Connection refused (origin check).',
  3403: 'Connection refused (origin check).',
  4401: 'Your session ended. Please sign in again.',
  4403: 'You do not have access to this container (or it was revoked).',
  4408: 'Closed after inactivity.',
  4409: 'Container is not running.',
  4429: 'Too many terminals open or too many attempts. Wait a moment.',
  4500: 'Server could not reach LXD.',
};

const name = new URLSearchParams(location.search).get('name') ?? '';
const status = document.getElementById('term-status')!;
const again = document.getElementById('reconnect') as HTMLButtonElement;
document.getElementById('term-name')!.textContent = name;

const term = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 2000, theme: { background: '#0b0e14' } });
const fit = new FitAddon();
term.loadAddon(fit);
term.open(document.getElementById('terminal')!);
fit.fit();

const enc = new TextEncoder();
let ws: WebSocket | null = null;

function connect(): void {
  again.hidden = true;
  status.textContent = 'connecting…';
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/containers/${name}/terminal`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { status.textContent = 'connected'; sendSize(); term.focus(); };
  ws.onmessage = (ev) => term.write(new Uint8Array(ev.data as ArrayBuffer));
  ws.onclose = (ev) => {
    const msg = REASONS[ev.code] ?? `Disconnected (${ev.code}).`;
    status.textContent = msg;
    term.write(`\r\n\x1b[2m[${msg}]\x1b[0m\r\n`);
    if (ev.code === 4401) setTimeout(toLogin, 1500);
    else again.hidden = false;
  };
}

// Every frame is binary with a 1-byte type: 0x00 = keystrokes, 0x01 = resize JSON.
function frame(type: number, payload: Uint8Array): void {
  if (ws?.readyState !== WebSocket.OPEN) return;
  const buf = new Uint8Array(payload.length + 1);
  buf[0] = type;
  buf.set(payload, 1);
  ws.send(buf);
}
term.onData((d) => frame(0, enc.encode(d)));
term.onBinary((d) => frame(0, Uint8Array.from(d, (c) => c.charCodeAt(0))));
const sendSize = () => frame(1, enc.encode(JSON.stringify({ cols: term.cols, rows: term.rows })));
term.onResize(sendSize);
addEventListener('resize', () => fit.fit());
again.onclick = connect;

(async () => {
  await initChrome();
  if (!NAME_RE.test(name)) {
    status.textContent = 'Invalid container name.';
    return;
  }
  connect();
})();
