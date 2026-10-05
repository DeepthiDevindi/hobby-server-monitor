// Minimal SVG line chart: one y-axis, thin 2px lines, recessive grid, a
// crosshair + tooltip on hover, legend when there are two series, and a
// visually-hidden summary for screen readers. No chart library: ~4 KB.
import { h } from './api';

export type Series = { key: string; label: string; color: string };
export type Point = { t: number } & Record<string, number>;

const H = 160, PAD = { l: 52, r: 8, t: 8, b: 20 };
const NS = 'http://www.w3.org/2000/svg';
const svg = (tag: string, attrs: Record<string, string | number>) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
};

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v)!;
}

export function lineChart(opts: {
  title: string; points: Point[]; series: Series[]; start: number; end: number; step: number;
  format: (v: number) => string; max?: number;
}): HTMLElement {
  const { points, series, start, end, format } = opts;
  const root = h('figure', { class: 'viz panel' }, h('figcaption', { class: 'small' }, h('strong', {}, opts.title)));
  if (series.length > 1) {
    root.append(h('div', { class: 'legend' }, ...series.map((s) => {
      const key = h('span', { class: 'key' });
      key.style.background = s.color;
      return h('span', {}, key, s.label);
    })));
  }
  const has = points.some((p) => series.some((s) => p[s.key] != null));
  if (!has) {
    root.append(h('div', { class: 'empty' }, 'No samples in this range yet'));
    return root;
  }
  const vmax = opts.max ?? niceMax(Math.max(...points.flatMap((p) => series.map((s) => p[s.key] ?? 0))));
  // Drawn at the real pixel width (not a stretched viewBox), so text stays crisp;
  // a ResizeObserver redraws when the layout changes.
  let W = 600;
  const x = (t: number) => PAD.l + ((t - start) / (end - start)) * (W - PAD.l - PAD.r);
  const y = (v: number) => H - PAD.b - (v / vmax) * (H - PAD.t - PAD.b);
  const el = svg('svg', { role: 'img', 'aria-label': opts.title });
  const cross = svg('line', { class: 'cross', y1: PAD.t, y2: H - PAD.b, visibility: 'hidden' });
  const tip = h('div', { class: 'tip', hidden: true });
  const wrap = h('div', { class: 'wrap' }, el as unknown as Node, tip);

  const draw = (width: number) => {
    W = Math.max(200, Math.round(width));
    el.setAttribute('viewBox', `0 0 ${W} ${H}`);
    el.replaceChildren();
    for (const f of [0, 0.5, 1]) {  // recessive grid + y ticks
      el.append(svg('line', { class: 'gridline', x1: PAD.l, x2: W - PAD.r, y1: y(vmax * f), y2: y(vmax * f) }));
      const t = svg('text', { class: 'tick', x: PAD.l - 6, y: y(vmax * f) + 3, 'text-anchor': 'end' });
      t.textContent = format(vmax * f);
      el.append(t);
    }
    for (const t of [start, (start + end) / 2, end]) {
      const lbl = svg('text', { class: 'tick', x: x(t), y: H - 4, 'text-anchor': t === start ? 'start' : t === end ? 'end' : 'middle' });
      lbl.textContent = timeLabel(t, end - start);
      el.append(lbl);
    }
    for (const s of series) {
      // Break the line on gaps (missing buckets) instead of drawing across them.
      let d = '';
      let prev = -Infinity;
      for (const p of points) {
        const v = p[s.key];
        if (v == null) { prev = -Infinity; continue; }
        d += `${p.t - prev > opts.step * 2.5 ? 'M' : 'L'}${x(p.t).toFixed(1)},${y(v).toFixed(1)}`;
        prev = p.t;
      }
      const path = svg('path', { class: 'line', d });
      (path as SVGElement).style.stroke = s.color;  // style accepts var(--series-n)
      el.append(path);
    }
    el.append(cross);
  };
  draw(600);
  new ResizeObserver((entries) => draw(entries[0].contentRect.width)).observe(wrap);

  const hide = () => { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; };
  el.addEventListener('pointerleave', hide);
  el.addEventListener('pointermove', (ev) => {
    const box = el.getBoundingClientRect();
    const t = start + ((ev.clientX - box.left) - PAD.l) / (W - PAD.l - PAD.r) * (end - start);
    const near = points.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a));
    cross.setAttribute('x1', String(x(near.t)));
    cross.setAttribute('x2', String(x(near.t)));
    cross.setAttribute('visibility', 'visible');
    tip.replaceChildren(h('div', { class: 'muted' }, new Date(near.t * 1000).toLocaleString()),
      ...series.map((s) => h('div', {}, `${s.label}: ${near[s.key] == null ? '–' : format(near[s.key])}`)));
    tip.hidden = false;
    tip.style.left = `${Math.min(x(near.t) + 10, box.width - 170)}px`;
    tip.style.top = '4px';
  });
  root.append(wrap);
  // Text alternative (identity and values are never colour-only).
  const last = points[points.length - 1];
  root.append(h('p', { class: 'sr-only' }, `${opts.title}: latest ${series.map((s) =>
    `${s.label} ${last[s.key] == null ? 'n/a' : format(last[s.key])}`).join(', ')}; peak ${format(vmax)} scale.`));
  return root;
}

function timeLabel(t: number, span: number): string {
  const d = new Date(t * 1000);
  return span > 2 * 86400 ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}
